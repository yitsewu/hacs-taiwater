"""帳單保存、查詢狀態與日曆排程；錯誤不覆蓋最後成功資料。"""
from __future__ import annotations

import asyncio
import hashlib
import json
from calendar import monthrange
from decimal import Decimal
from functools import partial
import logging

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
        self.store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}", private=True)
        self._query_lock = asyncio.Lock()
        self._cancel_timer = None
        self._closing = False
        self._initial_task = None
        self._statistics_fingerprint = None
        self.data = {
            "query_status": "never", "query_success": None, "ocr_status": "not_run",
            "ocr_success": None, "verification_status": "not_run", "verification_success": None,
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
            if self.data.get("query_status") == "running":
                self.data.update(query_status="failed", query_success=False, error_code="interrupted")
        self.data["enabled"] = self.options["enabled"]
        self._rebuild()
        seed = self.hass.data.get(DOMAIN, {}).get("seeds", {}).pop(self.entry.unique_id, None)
        if seed:
            self.data["last_attempt"] = seed.get("started_at", seed["finished_at"])
            await self._accept_result(seed)
        elif self.bills:
            await self._publish_statistics()
        self._schedule()
        self.async_set_updated_data(dict(self.data))
        await self._save()

    def async_start(self):
        # 首次回填優先；沒有新需求時重啟只恢復排程，不立即重抓所有帳單。
        if not self.data["history_complete"]:
            self._initial_task = self.entry.async_create_background_task(
                self.hass, self.async_query(history=True, refresh_history=False, raise_errors=False),
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

    async def _publish_statistics(self):
        try:
            bills = [Bill.from_dict(raw) for raw in self.bills.values()]
            daily = allocate_days(bills, self.options.get("carbon_factor"), self.options.get("carbon_factor_source", ""), mode=self.options["allocation"])
            fingerprint = hashlib.sha256(json.dumps(serializable(daily), sort_keys=True).encode()).hexdigest()
            if fingerprint == self._statistics_fingerprint and self.data.get("statistics_status") == "published":
                return
            result = await async_publish_statistics(self.hass, self.entry.entry_id, self.entry.title, daily)
            self.data["statistics_status"] = result
            if result == "published":
                self._statistics_fingerprint = fingerprint
        except Exception:
            # 查詢成功與統計寫入失敗分開顯示，下次更新／重新載入可從保存帳單重建。
            self.data["statistics_status"] = "failed"
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
        if successful:
            self.data["last_success"] = finished
        else:
            self.data["last_failure"] = finished
        self._diagnostic_flags()
        self._rebuild()
        await self._save()
        await self._publish_statistics()

    def _diagnostic_flags(self):
        ocr = self.data["ocr_status"]
        verification = self.data["verification_status"]
        self.data["ocr_success"] = True if ocr == "success" else False if ocr in {"failed", "unavailable"} else None
        self.data["verification_success"] = True if verification == "accepted" else False if verification == "rejected" else None

    async def async_query(self, history=False, *, refresh_history=True, raise_errors=True):
        url = await async_resolve_ocr_url(self.hass, self.options.get("ocr_url", self.entry.data.get("ocr_url", "")))
        # 普通排程補齊缺期；手動最新一期只抓最新；歷史按鈕可重抓更正資料。
        limit = self.options["history_limit"] if history else None
        job = partial(client.query, self.entry.data["water_id"], self.entry.data["customer_name"],
                      ocr_url=url, history_limit=limit, known_months=tuple(self.bills), refresh_history=refresh_history)
        await self._run(job, raise_errors=raise_errors)

    async def async_submit_manual(self, challenge, code):
        job = partial(client.submit_manual, challenge, self.entry.data["water_id"], self.entry.data["customer_name"],
                      code, history_limit=self.options["history_limit"], refresh_history=True)
        await self._run(job)

    async def _run(self, job, *, raise_errors=True):
        if self._closing or self._query_lock.locked():
            if raise_errors:
                raise client.QueryError("busy")
            return
        async with self._query_lock:
            self.data.update(last_attempt=client.now(), query_status="running", query_success=None,
                             ocr_status="not_run", verification_status="not_run", ocr_success=None,
                             verification_success=None, error_code=None)
            self.async_set_updated_data(dict(self.data))
            await self._save()
            failure = None
            try:
                result = await self.hass.async_add_executor_job(job)
                self.data.update(ocr_status=result["ocr_status"], verification_status=result["verification_status"])
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
            self.async_set_updated_data(dict(self.data))
            await self._save()
            if failure and raise_errors:
                raise client.QueryError(failure, ocr_status=self.data["ocr_status"], verification_status=self.data["verification_status"])

    async def _save(self):
        await self.store.async_save({"bills": self.bills, "available_months": self.available_months,
                                     "status": serializable(self.data)})

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
        await self.async_query(history=True, refresh_history=False, raise_errors=False)

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
        # 不會在 unload 刪帳單或外部統計；重裝之前可從 HA 備份復原。
