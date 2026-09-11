"""台灣自來水帳單的純 Python 資料模型、分攤與排程工具。"""

from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
from typing import Any, Iterable, Mapping


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_DATE_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_PRIVATE_KEY_RE = re.compile(
    r"(?:^|_)(?:account|address|customer|subscriber|name|phone|mobile|email|"
    r"meter_(?:id|no|number)|identity|id_card|tax_id|credential|password|token|"
    r"cookie|secret|bill_id|invoice_(?:id|no|number))(?:$|_)|"
    r"(?:姓名|戶名|用戶名稱|地址|水號|表號|編號|查核碼|電話|證號|帳號)",
    re.IGNORECASE,
)

_USAGE_KEYS = ("usage_m3", "water_usage_m3", "usage")
_TOTAL_KEYS = ("total_twd", "total_amount", "amount_twd", "amount")
_CARBON_KEYS = ("carbon_kg",)
_START_KEYS = ("period_start", "reading_start")
_END_KEYS = ("period_end", "reading_end")
_FEE_KEYS = ("fee_breakdown", "fees")
_CARBON_SOURCE_KEYS = ("carbon_source",)
_KNOWN_KEYS = frozenset(
    _USAGE_KEYS
    + _TOTAL_KEYS
    + _CARBON_KEYS
    + _START_KEYS
    + _END_KEYS
    + _FEE_KEYS
    + _CARBON_SOURCE_KEYS
)


def _parse_month(value: str) -> str:
    if not isinstance(value, str) or _MONTH_RE.fullmatch(value) is None:
        raise ValueError("帳單月份必須使用 YYYY-MM 格式")
    return value


def _parse_fetched_at(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("fetched_at 必須是 ISO 8601 字串")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("fetched_at 必須是有效的 ISO 8601 時間") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("fetched_at 必須包含時區")
    return parsed


def _parse_decimal(
    value: Any, field_name: str, *, allow_negative: bool = False
) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{field_name} 必須是數值")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 必須是有效數值") from exc
    if not parsed.is_finite() or (parsed < 0 and not allow_negative):
        requirement = "有限數值" if allow_negative else "非負有限數值"
        raise ValueError(f"{field_name} 必須是{requirement}")
    return parsed


def _parse_date(value: Any, field_name: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        raise ValueError(f"{field_name} 必須是 YYYY-MM-DD 日期")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} 必須使用 YYYY-MM-DD 格式")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} 必須是有效日期") from exc


def _alias_value(fields: Mapping[str, Any], keys: tuple[str, ...], field_name: str) -> Any:
    present = [fields[key] for key in keys if key in fields and fields[key] not in (None, "")]
    if not present:
        return None
    first = present[0]
    if any(str(value) != str(first) for value in present[1:]):
        raise ValueError(f"{field_name} 的別名欄位內容互相衝突")
    return first


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _safe_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("額外欄位不得包含非有限數值")
        return _json_number(value)
    if isinstance(value, float):
        parsed = Decimal(str(value))
        if not parsed.is_finite():
            raise ValueError("額外欄位不得包含非有限數值")
        return value
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or _PRIVATE_KEY_RE.search(key):
                continue
            result[key] = _safe_json_value(child)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_json_value(item) for item in value]
    raise ValueError("額外欄位只能包含 JSON 相容資料")


def _parse_fees(value: Any) -> dict[str, Decimal]:
    if value in (None, ""):
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("fee_breakdown 必須是費用名稱到數值的物件")
    result: dict[str, Decimal] = {}
    for key, raw_amount in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("費用名稱必須是非空字串")
        if _PRIVATE_KEY_RE.search(key):
            continue
        amount = _parse_decimal(
            raw_amount, f"fee_breakdown.{key}", allow_negative=True
        )
        if amount is None:
            raise ValueError(f"fee_breakdown.{key} 不得為空")
        result[key] = amount
    return result


@dataclass(frozen=True, slots=True)
class Bill:
    """單張帳單。

    數值保留為 ``Decimal``，日期保留為 ``date``。需要 JSON 時應呼叫
    ``to_dict()`` 或 ``to_json()``，避免呼叫端自行處理 Decimal 與日期。
    ``extra_fields`` 僅保留鍵名未顯示私人性質的 JSON 相容欄位。
    """

    month: str
    fetched_at: str
    usage_m3: Decimal | None = None
    total_twd: Decimal | None = None
    carbon_kg: Decimal | None = None
    period_start: date | None = None
    period_end: date | None = None
    fee_breakdown: dict[str, Decimal] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    extra_fields: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _parse_month(self.month)
        _parse_fetched_at(self.fetched_at)
        if (self.period_start is None) != (self.period_end is None):
            raise ValueError("抄表區間必須同時提供開始日與結束日")
        if self.period_start is not None and self.period_end is not None:
            if self.period_start >= self.period_end:
                raise ValueError("抄表區間結束日必須晚於開始日")

    @classmethod
    def from_fields(cls, month: str, fields: Mapping[str, Any], fetched_at: str) -> Bill:
        """從擷取欄位建立帳單，並排除可辨識的私人欄位。"""

        _parse_month(month)
        _parse_fetched_at(fetched_at)
        if not isinstance(fields, Mapping):
            raise ValueError("fields 必須是欄位物件")

        usage = _parse_decimal(_alias_value(fields, _USAGE_KEYS, "usage_m3"), "usage_m3")
        total = _parse_decimal(_alias_value(fields, _TOTAL_KEYS, "total_twd"), "total_twd")
        carbon = _parse_decimal(_alias_value(fields, _CARBON_KEYS, "carbon_kg"), "carbon_kg")
        period_start = _parse_date(
            _alias_value(fields, _START_KEYS, "period_start"), "period_start"
        )
        period_end = _parse_date(_alias_value(fields, _END_KEYS, "period_end"), "period_end")
        if (period_start is None) != (period_end is None):
            raise ValueError("抄表區間必須同時提供開始日與結束日")
        if period_start is not None and period_end is not None and period_start >= period_end:
            raise ValueError("抄表區間結束日必須晚於開始日")

        fees = _parse_fees(_alias_value(fields, _FEE_KEYS, "fee_breakdown"))
        metadata: dict[str, Any] = {}
        if carbon is not None:
            metadata["carbon_source"] = "original"
            source_detail = _alias_value(fields, _CARBON_SOURCE_KEYS, "carbon_source")
            if source_detail not in (None, ""):
                if not isinstance(source_detail, str):
                    raise ValueError("carbon_source 必須是字串")
                metadata["carbon_source_detail"] = source_detail

        extra_fields: dict[str, Any] = {}
        for key, value in fields.items():
            if not isinstance(key, str):
                continue
            if key in _KNOWN_KEYS or _PRIVATE_KEY_RE.search(key):
                continue
            extra_fields[key] = _safe_json_value(value)

        return cls(
            month=month,
            fetched_at=fetched_at,
            usage_m3=usage,
            total_twd=total,
            carbon_kg=carbon,
            period_start=period_start,
            period_end=period_end,
            fee_breakdown=fees,
            metadata=metadata,
            extra_fields=extra_fields,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Bill:
        """重建 ``to_dict()`` 的輸出，供 coordinator 安全持久化後還原。"""

        if not isinstance(value, Mapping):
            raise ValueError("帳單資料必須是物件")
        try:
            month = value["month"]
            fetched_at = value["fetched_at"]
        except KeyError as exc:
            raise ValueError("帳單資料缺少 month 或 fetched_at") from exc

        fields: dict[str, Any] = {
            "usage_m3": value.get("usage_m3"),
            "total_twd": value.get("total_twd"),
            "carbon_kg": value.get("carbon_kg"),
            "period_start": value.get("period_start"),
            "period_end": value.get("period_end"),
            "fee_breakdown": value.get("fee_breakdown", {}),
        }
        metadata = value.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata 必須是物件")
        source_detail = metadata.get("carbon_source_detail")
        if source_detail not in (None, ""):
            fields["carbon_source"] = source_detail

        extra_fields = value.get("extra_fields", {})
        if not isinstance(extra_fields, Mapping):
            raise ValueError("extra_fields 必須是物件")
        for key, extra_value in extra_fields.items():
            if isinstance(key, str) and key not in _KNOWN_KEYS:
                fields[key] = extra_value
        return cls.from_fields(month, fields, fetched_at)

    def to_dict(self) -> dict[str, Any]:
        """回傳可直接交給 ``json.dumps`` 的資料。"""

        return {
            "month": self.month,
            "fetched_at": self.fetched_at,
            "usage_m3": None if self.usage_m3 is None else _json_number(self.usage_m3),
            "total_twd": None if self.total_twd is None else _json_number(self.total_twd),
            "carbon_kg": None if self.carbon_kg is None else _json_number(self.carbon_kg),
            "period_start": None if self.period_start is None else self.period_start.isoformat(),
            "period_end": None if self.period_end is None else self.period_end.isoformat(),
            "fee_breakdown": {
                key: _json_number(value) for key, value in self.fee_breakdown.items()
            },
            "metadata": _safe_json_value(self.metadata),
            "extra_fields": _safe_json_value(self.extra_fields),
        }

    def to_json(self) -> str:
        """以穩定鍵序輸出 JSON。"""

        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def _previous_month(month: str) -> str:
    year, month_number = (int(part) for part in month.split("-"))
    if month_number == 1:
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month_number - 1:02d}"


def _month_days(month: str) -> int:
    year, month_number = (int(part) for part in month.split("-"))
    return monthrange(year, month_number)[1]


def _month_day_counts(start: date, end: date) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    current = start + timedelta(days=1)
    while current <= end:
        key = current.strftime("%Y-%m")
        counts[key] = counts.get(key, 0) + 1
        current += timedelta(days=1)
    return list(counts.items())


def _split_decimal(value: Decimal | None, weights: list[int]) -> list[Decimal | None]:
    if value is None:
        return [None] * len(weights)
    if not weights or any(weight <= 0 for weight in weights):
        raise ValueError("分攤權重必須是正整數")
    total_weight = sum(weights)
    result: list[Decimal] = []
    with localcontext() as context:
        # 固定為 Python Decimal 預設精度，讓呼叫端直接加總時仍能精確守恆。
        context.prec = 28
        allocated = Decimal(0)
        for weight in weights[:-1]:
            share = value * Decimal(weight) / Decimal(total_weight)
            result.append(share)
            allocated += share
        result.append(value - allocated)
    return result


def _prepare_bills(bills: Iterable[Bill]) -> list[Bill]:
    latest: dict[str, tuple[datetime, int, Bill]] = {}
    for index, bill in enumerate(bills):
        if not isinstance(bill, Bill):
            raise TypeError("bills 只能包含 Bill")
        fetched = _parse_fetched_at(bill.fetched_at).astimezone(timezone.utc)
        current = latest.get(bill.month)
        candidate = (fetched, index, bill)
        if current is None or candidate[:2] >= current[:2]:
            latest[bill.month] = candidate

    selected = [item[2] for item in latest.values()]
    selected.sort(key=lambda bill: (bill.period_start or date.max, bill.month, bill.fetched_at))
    actual = [bill for bill in selected if bill.period_start is not None]
    for previous, current in zip(actual, actual[1:]):
        assert previous.period_end is not None
        assert current.period_start is not None
        if current.period_start < previous.period_end:
            raise ValueError(
                f"實際抄表區間重疊：{previous.month} 與 {current.month}"
            )
    return selected


def _carbon_total(
    bill: Bill, carbon_factor: Decimal | None, carbon_factor_source: str
) -> tuple[Decimal | None, str, str]:
    if bill.carbon_kg is not None:
        return bill.carbon_kg, "original", ""
    if carbon_factor is not None and bill.usage_m3 is not None:
        return bill.usage_m3 * carbon_factor, "configured_estimate", carbon_factor_source
    return None, "", ""


def _validated_carbon_factor(value: Any) -> Decimal | None:
    return _parse_decimal(value, "carbon_factor")


def allocate_months(
    bills: Iterable[Bill],
    mode: str = "days",
    carbon_factor: Decimal | int | float | str | None = None,
    carbon_factor_source: str = "",
) -> dict[str, dict[str, Any]]:
    """按月份分攤帳單。

    回傳值只包含帳單實際涵蓋或估算涵蓋的月份。所有分攤數值都是估算，
    因此 ``estimated`` 固定為 ``True``；``period_estimated`` 才表示日期期間
    是否也是估算。``water``、``cost``、``carbon`` 為 ``Decimal | None``，
    未知值不補零。實際區間採 ``(start, end]``。缺少區間時使用帳單月與
    前一月，``covered_days`` 為 ``None``，明確表示並非真實抄表日期。
    每個帳單的最後一個分攤項吸收 Decimal 餘差。
    """

    if mode not in {"days", "equal"}:
        raise ValueError("mode 只能是 days 或 equal")
    if not isinstance(carbon_factor_source, str):
        raise ValueError("carbon_factor_source 必須是字串")
    factor = _validated_carbon_factor(carbon_factor)
    selected = _prepare_bills(bills)
    result: dict[str, dict[str, Any]] = {}

    for bill in selected:
        actual_period = bill.period_start is not None and bill.period_end is not None
        if actual_period:
            assert bill.period_start is not None and bill.period_end is not None
            month_counts = _month_day_counts(bill.period_start, bill.period_end)
            months = [month for month, _ in month_counts]
            covered = [count for _, count in month_counts]
            weights = covered if mode == "days" else [1] * len(months)
            basis = f"actual_period_{mode}"
            period_days = (bill.period_end - bill.period_start).days
        else:
            months = [_previous_month(bill.month), bill.month]
            covered = [0, 0]
            weights = [_month_days(month) for month in months] if mode == "days" else [1, 1]
            basis = f"estimated_bill_previous_{'calendar_days' if mode == 'days' else 'equal'}"
            period_days = None

        carbon_total, carbon_source, factor_source = _carbon_total(
            bill, factor, carbon_factor_source
        )
        water_parts = _split_decimal(bill.usage_m3, weights)
        cost_parts = _split_decimal(bill.total_twd, weights)
        carbon_parts = _split_decimal(carbon_total, weights)

        for index, month in enumerate(months):
            bucket = result.setdefault(
                month,
                {
                    "water": None,
                    "cost": None,
                    "carbon": None,
                    "estimated": True,
                    "period_estimated": False,
                    "carbon_estimated": False,
                    "allocation_basis": "",
                    "covered_days": 0,
                    "period_days": 0,
                    "source_months": [],
                    "bill_count": 0,
                    "carbon_source": "",
                    "carbon_factor_sources": [],
                    "_bases": set(),
                    "_carbon_sources": set(),
                    "_has_estimated_period": False,
                },
            )
            for key, part in (
                ("water", water_parts[index]),
                ("cost", cost_parts[index]),
                ("carbon", carbon_parts[index]),
            ):
                if part is not None:
                    bucket[key] = part if bucket[key] is None else bucket[key] + part
            bucket["period_estimated"] = bucket["period_estimated"] or not actual_period
            bucket["carbon_estimated"] = (
                bucket["carbon_estimated"] or carbon_source == "configured_estimate"
            )
            bucket["_has_estimated_period"] = bucket["_has_estimated_period"] or not actual_period
            if actual_period:
                bucket["covered_days"] += covered[index]
                bucket["period_days"] += period_days
            bucket["source_months"].append(bill.month)
            bucket["bill_count"] += 1
            bucket["_bases"].add(basis)
            if carbon_source:
                bucket["_carbon_sources"].add(carbon_source)
            if factor_source and factor_source not in bucket["carbon_factor_sources"]:
                bucket["carbon_factor_sources"].append(factor_source)

    for bucket in result.values():
        bucket["allocation_basis"] = "+".join(sorted(bucket.pop("_bases")))
        sources = sorted(bucket.pop("_carbon_sources"))
        bucket["carbon_source"] = sources[0] if len(sources) == 1 else ("mixed" if sources else "")
        if bucket.pop("_has_estimated_period"):
            bucket["covered_days"] = None
            bucket["period_days"] = None
        bucket["source_months"] = sorted(bucket["source_months"])
        bucket["carbon_factor_sources"].sort()
    return dict(sorted(result.items()))


def allocate_days(
    bills: Iterable[Bill],
    carbon_factor: Decimal | int | float | str | None = None,
    carbon_factor_source: str = "",
    mode: str = "days",
) -> dict[str, dict[str, Any]]:
    """建立每日長期統計資料；無實際區間的帳單不建立假日期。

    ``days`` 將整張帳單按日等分。``equal`` 先按涵蓋月份等分，再於各月
    的涵蓋日期內等分，因此按月彙總會與 ``allocate_months(..., "equal")``
    相同。每日數值仍是估算，實際期間只代表日期覆蓋不是估算。
    """

    if mode not in {"days", "equal"}:
        raise ValueError("mode 只能是 days 或 equal")
    if not isinstance(carbon_factor_source, str):
        raise ValueError("carbon_factor_source 必須是字串")
    factor = _validated_carbon_factor(carbon_factor)
    result: dict[str, dict[str, Any]] = {}
    for bill in _prepare_bills(bills):
        if bill.period_start is None or bill.period_end is None:
            continue
        dates: list[date] = []
        current = bill.period_start + timedelta(days=1)
        while current <= bill.period_end:
            dates.append(current)
            current += timedelta(days=1)
        carbon_total, carbon_source, factor_source = _carbon_total(
            bill, factor, carbon_factor_source
        )
        if mode == "days":
            weights = [1] * len(dates)
            water_parts = _split_decimal(bill.usage_m3, weights)
            cost_parts = _split_decimal(bill.total_twd, weights)
            carbon_parts = _split_decimal(carbon_total, weights)
        else:
            month_groups: dict[str, list[int]] = {}
            for index, day in enumerate(dates):
                month_groups.setdefault(day.strftime("%Y-%m"), []).append(index)

            def equal_month_then_day(value: Decimal | None) -> list[Decimal | None]:
                parts: list[Decimal | None] = [None] * len(dates)
                month_parts = _split_decimal(value, [1] * len(month_groups))
                for month_index, indices in enumerate(month_groups.values()):
                    day_parts = _split_decimal(month_parts[month_index], [1] * len(indices))
                    for day_index, part in zip(indices, day_parts):
                        parts[day_index] = part
                return parts

            water_parts = equal_month_then_day(bill.usage_m3)
            cost_parts = equal_month_then_day(bill.total_twd)
            carbon_parts = equal_month_then_day(carbon_total)
        for index, day in enumerate(dates):
            result[day.isoformat()] = {
                "water": water_parts[index],
                "cost": cost_parts[index],
                "carbon": carbon_parts[index],
                "estimated": True,
                "period_estimated": False,
                "carbon_estimated": carbon_source == "configured_estimate",
                "allocation_basis": "actual_period_day",
                "covered_days": 1,
                "source_month": bill.month,
                "carbon_source": carbon_source,
                "carbon_factor_source": factor_source,
            }
    return dict(sorted(result.items()))


def daily_allocation_list(
    bills: Iterable[Bill],
    carbon_factor: Decimal | int | float | str | None = None,
    carbon_factor_source: str = "",
    mode: str = "days",
) -> list[dict[str, Any]]:
    """回傳適合 statistics helper 逐列處理、含 ``date`` 的每日清單。"""

    rows = allocate_days(bills, carbon_factor, carbon_factor_source, mode)
    return [{"date": day, **values} for day, values in rows.items()]


def _wall_candidates(naive: datetime, tzinfo: Any) -> list[datetime]:
    valid: dict[datetime, datetime] = {}
    round_trips: list[datetime] = []
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tzinfo, fold=fold)
        utc_value = aware.astimezone(timezone.utc)
        back = utc_value.astimezone(tzinfo)
        round_trips.append(back)
        if back.replace(tzinfo=None) == naive:
            valid[utc_value] = back
    if valid:
        return [valid[key] for key in sorted(valid)]

    after_gap = [value for value in round_trips if value.replace(tzinfo=None) > naive]
    if after_gap:
        return [min(after_gap, key=lambda value: value.replace(tzinfo=None))]
    return [max(round_trips, key=lambda value: value.astimezone(timezone.utc))]


def _future_wall_candidate(naive: datetime, now_utc: datetime, tzinfo: Any) -> datetime | None:
    candidates = [
        candidate
        for candidate in _wall_candidates(naive, tzinfo)
        if candidate.astimezone(timezone.utc) > now_utc
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda value: value.astimezone(timezone.utc))


def _add_months(year: int, month: int, offset: int) -> tuple[int, int]:
    ordinal = year * 12 + (month - 1) + offset
    return divmod(ordinal, 12)[0], divmod(ordinal, 12)[1] + 1


def next_run(
    now: datetime,
    schedule: str,
    time: str,
    weekday: int = 0,
    monthday: int = 1,
    enabled: bool = True,
) -> datetime | None:
    """計算嚴格晚於 ``now`` 的下一次本地排程時間。

    週次的 ``weekday`` 採 Monday=0。月次日期超出當月時剪裁至月底。
    DST 重複時段選仍在未來的最早實際時間；缺失時段按時區位移到有效時間。
    """

    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now 必須是具時區的 datetime")
    if not enabled:
        return None
    if schedule not in {"daily", "weekly", "monthly"}:
        raise ValueError("schedule 只能是 daily、weekly 或 monthly")
    if not isinstance(time, str):
        raise ValueError("time 必須使用 HH:MM 格式")
    match = _TIME_RE.fullmatch(time)
    if match is None:
        raise ValueError("time 必須使用 HH:MM 格式")
    hour, minute = (int(part) for part in match.groups())
    local_now = now.astimezone(now.tzinfo)
    now_utc = now.astimezone(timezone.utc)
    wall_time = datetime_time(hour, minute)

    if schedule == "daily":
        intended_dates = (local_now.date() + timedelta(days=offset) for offset in range(370))
    elif schedule == "weekly":
        if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise ValueError("weekday 必須是 0 到 6 的整數")
        first_offset = (weekday - local_now.weekday()) % 7
        intended_dates = (
            local_now.date() + timedelta(days=first_offset + 7 * offset)
            for offset in range(60)
        )
    else:
        if isinstance(monthday, bool) or not isinstance(monthday, int) or not 1 <= monthday <= 31:
            raise ValueError("monthday 必須是 1 到 31 的整數")

        def monthly_dates() -> Iterable[date]:
            for offset in range(2400):
                year, month_number = _add_months(local_now.year, local_now.month, offset)
                clipped_day = min(monthday, monthrange(year, month_number)[1])
                yield date(year, month_number, clipped_day)

        intended_dates = monthly_dates()

    for intended_date in intended_dates:
        naive = datetime.combine(intended_date, wall_time)
        candidate = _future_wall_candidate(naive, now_utc, now.tzinfo)
        if candidate is not None:
            return candidate
    raise RuntimeError("找不到下一次有效排程時間")
