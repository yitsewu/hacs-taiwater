"""帳單保存、查詢狀態與日曆排程；錯誤不覆蓋最後成功資料。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from calendar import monthrange
from decimal import Decimal
from functools import partial
import logging
import time
from uuid import uuid4

from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import client
from .const import DEFAULT_OPTIONS, DOMAIN, STORAGE_VERSION
from .models import Bill, allocate_days, allocate_months, next_run
from .normalize import normalize_bill, roc_date, safe_fields
from .ocr import async_resolve_ocr_url
from .statistics import async_publish_statistics

_LOGGER = logging.getLogger(__name__)


def serializable(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serializable(item) for item in value]
    return value


class TaiWaterCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry)
        self.entry = entry
        self.options = {**DEFAULT_OPTIONS, "ocr_url": entry.data.get("ocr_url", ""), **entry.options}
        self.bills = {}
        self.monthly = {}
        self.available_months = []
        self.query_history = []
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}", private=True)
        self._query_lock = asyncio.Lock()
        self._store_lock = asyncio.Lock()
        self._cancel_timer = None
        self._closing = False
        self._initial_task = None
        self._statistics_fingerprint = None
        self._statistics_task = None
        self._statistics_pending = False
        self._statistics_force = False
        self.data = {
            "query_status": "never", "query_success": None, "ocr_status": "not_run",
            "ocr_success": None, "ocr_backend": "not_run", "verification_status": "not_run", "verification_success": None,
            "error_code": None, "statistics_status": "not_imported", "history_complete": False,
            "available_count": 0, "imported_count": 0, "enabled": self.options["enabled"],
        }

    async def async_initialize(self):
        saved = await self.store.async_load()
        if saved:
            # 所有帳期都保留；後來原站縮減可查月份不會刪除已匯入歷史。
            self.bills = saved.get("bills", {})
            self.available_months = saved.get("available_months", [])
            self.data.update(saved.get("status", {}))
            self.query_history = saved.get("query_history", [])[-100:]
            interrupted_at = None
            for record in self.query_history:
                if record.get("status") == "running":
                    interrupted_at = interrupted_at or client.now()
                    record.update(status="interrupted", error_code="interrupted",
                                  finished_at=interrupted_at, duration_seconds=None)
            if self.data.get("query_status") == "running":
                self.data.update(query_status="failed", query_success=False, error_code="interrupted",
                                 last_failure=interrupted_at or client.now())
        self.data["enabled"] = self.options["enabled"]
        self._rebuild()
        seed = self.hass.data.get(DOMAIN, {}).get("seeds", {}).pop(self.entry.unique_id, None)
        if seed:
            self.data["last_attempt"] = seed.get("started_at", seed["finished_at"])
            record = self._begin_record("setup", "initial", None, self.data["last_attempt"])
            await self._accept_result(seed)
            self._finish_record(record, seed["finished_at"], None, len(seed["bills"]))
        self._schedule()
        self.async_set_updated_data(dict(self.data))
        await self._save()

    def async_start(self):
        if self.bills:
            self.async_rebuild_statistics()
        # 首次回填優先；沒有新需求時重啟只恢復排程，不立即重抓所有帳單。
        if not self.data["history_complete"]:
            self._initial_task = self.entry.async_create_background_task(
                self.hass, self.async_query(history=True, refresh_history=False, raise_errors=False, trigger="initial"),
                "taiwater_initial_history"
            )

    def _rebuild(self):
        bills = [Bill.from_dict(raw) for raw in self.bills.values()]
        factor = self.options.get("carbon_factor")
        source = self.options.get("carbon_factor_source", "")
        self.monthly = serializable(allocate_months(bills, self.options["allocation"], factor, source))
        wanted = self.available_months
        limit = self.options["history_limit"]
        if limit:
            wanted = sorted(wanted, reverse=True)[:limit]
        self.data.update(available_count=len(self.available_months), imported_count=len(self.bills),
                         history_complete=bool(wanted) and set(wanted) <= self.bills.keys())
        if not bills:
            return
        latest = max(bills, key=lambda bill: bill.month)
        raw = latest.to_dict()
        extra = raw["extra_fields"]
        self.data.update(latest_month=latest.month, usage_m3=raw["usage_m3"], total_twd=raw["total_twd"],
                         fees=raw["fee_breakdown"], bill_fields=safe_fields(extra),
                         period_start=extra.get("billing_start", raw["period_start"]),
                         period_end=extra.get("billing_end", raw["period_end"]),
                         carbon_kg=raw["carbon_kg"], carbon_source="original" if raw["carbon_kg"] is not None else "not_configured",
                         bill_fetched_at=latest.fetched_at)
        if latest.carbon_kg is None and factor is not None and latest.usage_m3 is not None:
            self.data.update(carbon_kg=float(latest.usage_m3 * Decimal(str(factor))), carbon_source="configured_estimate")
        self.data.update(carbon_factor=factor, carbon_factor_source=source)
        self.data["dates"] = {}
        for key, value in extra.items():
            if key in {"本期預定扣繳日", "本期繳費起始日", "本期繳費期限", "本期抄表日期", "下期繳費起始日", "下期抄表日"}:
                try:
                    parsed = roc_date(value)
                    self.data["dates"][key] = parsed.isoformat() if parsed else None
                except ValueError:
                    self.data["dates"][key] = None
        self.data["payment_due"] = self.data["dates"].get("本期繳費期限")
        self.data["payment_status"] = next((extra[k] for k in ("繳費狀態", "繳費情形", "繳費狀況") if extra.get(k)), None)
        days = (latest.period_end - latest.period_start).days if latest.period_start and latest.period_end else None
        self.data["billing_days"] = days
        self.data["daily_usage"] = float(latest.usage_m3 / days) if days and latest.usage_m3 is not None else None
        self.data["average_unit_cost"] = float(latest.total_twd / latest.usage_m3) if latest.usage_m3 and latest.total_twd is not None else None
        self.data["latest_allocated_month"] = max(self.monthly, default=None)
        last = self.monthly.get(self.data["latest_allocated_month"], {})
        for metric in ("water", "cost", "carbon"):
            self.data[f"monthly_{metric}"] = last.get(metric)
        year = str(dt_util.now().year)
        months = {key: value for key, value in self.monthly.items() if key.startswith(year + "-")}
        self.data["year_covered_months"] = len(months)
        self.data["year"] = year
        self.data["year_complete"] = len(months) == 12 and all(
            value.get("covered_days") == monthrange(int(key[:4]), int(key[5:]))[1] for key, value in months.items()
        )
        for metric in ("water", "cost", "carbon"):
            values = [value[metric] for value in months.values() if value.get(metric) is not None]
            # 缺項不冒充完整合計；已取得月份以 coverage metadata 呈現。
            self.data[f"year_{metric}"] = sum(values) if values and len(values) == len(months) else None

    def async_rebuild_statistics(self, *, force=False):
        """Coalesce saved bill revisions into one worker, without querying the portal."""
        if self._closing:
            return
        self._statistics_pending = True
        self._statistics_force |= force
        self.data["statistics_status"] = "pending"
        self.async_set_updated_data(dict(self.data))
        if self._statistics_task is None or self._statistics_task.done():
            self._statistics_task = self.hass.async_create_task(
                self._statistics_worker(), "taiwater_statistics"
            )

    async def _statistics_worker(self):
        while self._statistics_pending:
            self._statistics_pending = False
            force, self._statistics_force = self._statistics_force, False
            await self._publish_statistics(force=force)
            self.async_set_updated_data(dict(self.data))
            await self._save()

    async def _publish_statistics(self, *, force=False):
        try:
            # Snapshot immutable Bill values before yielding; newer queries queue another pass.
            bills = [Bill.from_dict(raw) for raw in self.bills.values()]
            daily = await self.hass.async_add_executor_job(partial(
                allocate_days, bills, self.options.get("carbon_factor"),
                self.options.get("carbon_factor_source", ""), mode=self.options["allocation"]
            ))
            fingerprint = hashlib.sha256(json.dumps(serializable(daily), sort_keys=True).encode()).hexdigest()
            if not force and fingerprint == self._statistics_fingerprint:
                self.data["statistics_status"] = "published" if daily else "partial"
                return
            self.data["statistics_status"] = "running"
            self.async_set_updated_data(dict(self.data))
            started = time.monotonic()
            result = await async_publish_statistics(self.hass, self.entry.entry_id, self.entry.title, daily)
            self.data["statistics_status"] = result
            self.data["statistics_duration"] = round(time.monotonic() - started, 3)
            self.data["statistics_updated"] = dt_util.utcnow().isoformat()
            self._statistics_fingerprint = fingerprint
        except Exception:
            # 查詢成功與統計寫入失敗分開顯示，下次更新／重新載入可從保存帳單重建。
            self.data["statistics_status"] = "failed"
            self._statistics_fingerprint = None
            _LOGGER.warning("台水歷史統計未完成；已保留帳單，可重新載入重建")

    async def _accept_result(self, result):
        candidate = dict(self.bills)
        for raw in result["bills"]:
            bill = normalize_bill(raw)
            candidate[bill.month] = bill.to_dict()
        # 在替換最後成功資料前先檢查所有帳期的區間契約。
        allocate_months([Bill.from_dict(raw) for raw in candidate.values()], self.options["allocation"])
        self.bills = candidate
        self.available_months = result["available_months"]
        successful = result["status"] == "success"
        finished = result["finished_at"]
        self.data.update(query_status=result["status"], query_success=successful,
                         ocr_status=result["ocr_status"], verification_status=result["verification_status"],
                         error_code=result.get("error_code"))
        self.data["ocr_backend"] = result.get("ocr_backend", self.data.get("ocr_backend", "not_run"))
        if successful:
            self.data["last_success"] = finished
        else:
            self.data["last_failure"] = finished
        self._diagnostic_flags()
        self._rebuild()
        await self._save()

    def _diagnostic_flags(self):
        ocr = self.data["ocr_status"]
        verification = self.data["verification_status"]
        self.data["ocr_success"] = True if ocr == "success" else False if ocr in {"failed", "unavailable"} else None
        self.data["verification_success"] = True if verification == "accepted" else False if verification == "rejected" else None

    async def async_query(self, history=False, *, refresh_history=True, raise_errors=True, trigger="button", requested_month=None):
        if requested_month is not None:
            client.validate_month(requested_month)
        url = await async_resolve_ocr_url(self.hass, self.options.get("ocr_url", self.entry.data.get("ocr_url", "")))
        # 普通排程補齊缺期；手動最新一期只抓最新；歷史按鈕可重抓更正資料。
        limit = self.options["history_limit"] if history else None
        job = partial(client.query, self.entry.data["water_id"], self.entry.data["customer_name"],
                      ocr_url=url, history_limit=limit, known_months=tuple(self.bills), refresh_history=refresh_history)
        if requested_month is not None:
            job.keywords["requested_month"] = requested_month
        return await self._run(job, raise_errors=raise_errors, trigger=trigger,
                               operation="month" if requested_month else "history" if history else "latest",
                               requested_month=requested_month)

    async def async_submit_manual(self, challenge, code):
        job = partial(client.submit_manual, challenge, self.entry.data["water_id"], self.entry.data["customer_name"],
                      code, history_limit=self.options["history_limit"], refresh_history=True)
        return await self._run(job, trigger="manual", operation="history")

    def _begin_record(self, trigger, operation, requested_month, started_at):
        record = {"query_id": uuid4().hex, "trigger": trigger, "operation": operation,
                  "requested_month": requested_month, "started_at": started_at,
                  "finished_at": None, "duration_seconds": None, "status": "running",
                  "ocr_status": "not_run", "verification_status": "not_run",
                  "error_code": None, "fetched_count": 0}
        self.query_history.append(record)
        self.query_history = self.query_history[-100:]
        return record

    def _finish_record(self, record, finished_at, duration, fetched_count):
        record.update(finished_at=finished_at, duration_seconds=duration,
                      status=self.data["query_status"], ocr_status=self.data["ocr_status"],
                      ocr_backend=self.data.get("ocr_backend", "not_run"),
                      verification_status=self.data["verification_status"],
                      error_code=self.data["error_code"], fetched_count=fetched_count)

    async def _run(self, job, *, raise_errors=True, trigger="button", operation="latest", requested_month=None):
        if self._closing or self._query_lock.locked():
            if raise_errors:
                raise client.QueryError("busy")
            return
        async with self._query_lock:
            self.data["ocr_backend"] = "manual" if trigger == "manual" else "external" if job.keywords.get("ocr_url") else "builtin"
            self.data.update(last_attempt=client.now(), query_status="running", query_success=None,
                             ocr_status="not_run", verification_status="not_run", ocr_success=None,
                             verification_success=None, error_code=None)
            started = time.monotonic()
            record = self._begin_record(trigger, operation, requested_month, self.data["last_attempt"])
            self.async_set_updated_data(dict(self.data))
            await self._save()
            failure = None
            result = None
            fetched_count = 0
            try:
                result = await self.hass.async_add_executor_job(job)
                self.data.update(ocr_status=result["ocr_status"], verification_status=result["verification_status"])
                fetched_count = len(result["bills"])
                if requested_month is not None and [bill["month"] for bill in result["bills"]] != [requested_month]:
                    raise ValueError("Unexpected requested month response")
                await self._accept_result(result)
            except client.QueryError as error:
                failure = error.code
                self.data.update(ocr_status=error.ocr_status, verification_status=error.verification_status)
            except (ValueError, KeyError, TypeError):
                failure = "invalid_response"
            except Exception:
                failure = "internal_error"
            if failure:
                self.data.update(query_status="failed", query_success=False, error_code=failure, last_failure=client.now())
                self._diagnostic_flags()
            finished = self.data["last_failure"] if failure else dt_util.utcnow().isoformat()
            self._finish_record(record, finished, round(time.monotonic() - started, 3),
                                fetched_count)
            self.async_set_updated_data(dict(self.data))
            await self._save()
            if not failure:
                self.async_rebuild_statistics()
            if failure and raise_errors:
                raise client.QueryError(failure, ocr_status=self.data["ocr_status"], verification_status=self.data["verification_status"])
            return dict(record)

    async def _save(self):
        async with self._store_lock:
            await self.store.async_save({"bills": self.bills, "available_months": self.available_months,
                                         "status": serializable(self.data), "query_history": self.query_history})

    def _schedule(self):
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None
        following = next_run(dt_util.now(), self.options["schedule"], self.options["query_time"],
                             self.options["weekday"], self.options["monthday"], self.options["enabled"])
        self.data["next_query"] = following.isoformat() if following else None
        if following:
            self._cancel_timer = async_track_point_in_time(self.hass, self._scheduled, following)

    async def _scheduled(self, _now):
        self._schedule()  # 先排下一次，手動查詢不改動日曆排程。
        self.async_set_updated_data(dict(self.data))
        await self.async_query(history=True, refresh_history=False, raise_errors=False, trigger="schedule")

    async def async_set_enabled(self, enabled):
        self.options["enabled"] = bool(enabled)
        self.data["enabled"] = bool(enabled)
        self._schedule()
        self.async_set_updated_data(dict(self.data))
        await self._save()
        self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, "enabled": bool(enabled)})

    async def async_close(self):
        self._closing = True
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None
        # 等待已開始的查詢保存結果，避免 reload 與舊 executor 同時查同一水號。
        async with self._query_lock:
            pass
        if self._statistics_task is not None:
            await self._statistics_task
        # 不會在 unload 刪帳單或外部統計；重裝之前可從 HA 備份復原。
