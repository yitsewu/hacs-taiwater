"""Publish estimated TaiWater history as external long-term statistics."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Final
from weakref import WeakKeyDictionary
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfMass, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_conversion import MassConverter, VolumeConverter

from .const import DOMAIN

STATUS_PUBLISHED: Final = "published"
STATUS_PARTIAL: Final = "partial"
STATUS_CLEARED: Final = "cleared"

_TAIPEI: Final = ZoneInfo("Asia/Taipei")
_HOURS_PER_DAY: Final = Decimal(24)
_DATE_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SLUG_RE: Final = re.compile(r"^(?!_)(?!.+__)[a-z0-9_]+(?<!_)$", re.ASCII | re.IGNORECASE)
_UNKNOWN_VALUES: Final = frozenset({"", "none", "null", "unknown", "unavailable"})

_METRICS: Final = {
    "water": (
        "water",
        "總用水量",
        VolumeConverter.UNIT_CLASS,
        UnitOfVolume.CUBIC_METERS,
    ),
    "cost": ("cost", "台水費用（分攤估算）", None, "TWD"),
    "carbon": (
        "carbon",
        "台水碳排 kgCO2e（分攤估算）",
        MassConverter.UNIT_CLASS,
        UnitOfMass.KILOGRAMS,
    ),
}

_publish_locks: WeakKeyDictionary[HomeAssistant, dict[str, asyncio.Lock]] = WeakKeyDictionary()


def _entry_statistic_ids(entry_id: str) -> dict[str, str]:
    """Return the only statistic IDs this module may clear or publish."""
    if not isinstance(entry_id, str) or not _SLUG_RE.fullmatch(entry_id):
        raise ValueError("entry_id must be a valid ASCII Home Assistant slug")
    normalized_entry_id = entry_id.lower()
    return {
        metric: f"{DOMAIN}:{normalized_entry_id}_{suffix}"
        for metric, (suffix, _label, _unit_class, _unit) in _METRICS.items()
    }


def _publish_lock(hass: HomeAssistant, entry_id: str) -> asyncio.Lock:
    """Serialize destructive replace operations for one config entry."""
    locks = _publish_locks.setdefault(hass, {})
    return locks.setdefault(entry_id, asyncio.Lock())


def _parse_day(value: object) -> date | None:
    """Parse one canonical ISO date, omitting unknown or malformed dates."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _parse_amount(value: object) -> Decimal | None:
    """Parse a finite daily amount, omitting missing and unknown values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().lower() in _UNKNOWN_VALUES:
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _build_series(
    daily: Mapping[str, Mapping[str, object]],
) -> dict[str, list[StatisticData]]:
    """Build deterministic hourly cumulative sums from daily allocations."""
    parsed_days: list[tuple[date, Mapping[str, object]]] = []
    for date_iso, values in daily.items():
        if (day := _parse_day(date_iso)) is None or not isinstance(values, Mapping):
            continue
        parsed_days.append((day, values))
    parsed_days.sort(key=lambda item: item[0])

    result: dict[str, list[StatisticData]] = {}
    for metric in _METRICS:
        cumulative = Decimal(0)
        rows: list[StatisticData] = []
        for day, values in parsed_days:
            if (amount := _parse_amount(values.get(metric))) is None:
                continue
            day_start = datetime.combine(day, time.min, tzinfo=_TAIPEI)
            if not rows:
                rows.append(
                    {
                        "start": day_start - timedelta(hours=1),
                        "state": 0.0,
                        "sum": 0.0,
                    }
                )
            for hour in range(24):
                # Computing each point from the day total makes the final hour exact,
                # while distributing the estimate evenly for hourly Energy charts.
                value = cumulative + amount * Decimal(hour + 1) / _HOURS_PER_DAY
                numeric_value = float(value)
                rows.append(
                    {
                        "start": day_start + timedelta(hours=hour),
                        "state": numeric_value,
                        "sum": numeric_value,
                    }
                )
            cumulative += amount
        if rows:
            result[metric] = rows
    return result


def _metadata(
    statistic_id: str,
    name: str,
    metric: str,
) -> StatisticMetaData:
    """Build HA 2026.3+ metadata for one cumulative external statistic."""
    _suffix, label, unit_class, unit = _METRICS[metric]
    source_name = name.strip()
    return {
        "has_sum": True,
        "mean_type": StatisticMeanType.NONE,
        "name": f"{source_name} {label}" if source_name else label,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_class": unit_class,
        "unit_of_measurement": unit,
    }


async def _async_read_last_statistic(
    recorder: object, hass: HomeAssistant, statistic_id: str
) -> tuple[float, float | None] | None:
    """Read the last persisted timestamp and sum through the recorder executor."""
    result = await recorder.async_add_executor_job(  # type: ignore[attr-defined]
        get_last_statistics,
        hass,
        1,
        statistic_id,
        False,
        {"sum"},
    )
    rows = result.get(statistic_id)
    if not rows:
        return None
    last = rows[-1]
    return float(last["start"]), last.get("sum")


async def _async_verify_reimport(
    recorder: object,
    hass: HomeAssistant,
    statistic_ids: Mapping[str, str],
    series: Mapping[str, list[StatisticData]],
) -> None:
    """Prove clear/import before the caller commits its fingerprint."""
    for metric, statistic_id in statistic_ids.items():
        actual = await _async_read_last_statistic(recorder, hass, statistic_id)
        rows = series.get(metric)
        if not rows:
            if actual is not None:
                raise HomeAssistantError(f"Stale statistics remain after clearing {statistic_id}")
            continue
        expected_start = rows[-1]["start"].timestamp()
        expected_sum = rows[-1]["sum"]
        if actual is None:
            raise HomeAssistantError(f"Statistics readback returned no rows for {statistic_id}")
        actual_start, actual_sum = actual
        if (
            not math.isclose(actual_start, expected_start, rel_tol=0, abs_tol=1e-6)
            or actual_sum is None
            or not math.isclose(actual_sum, expected_sum, rel_tol=1e-12, abs_tol=1e-12)
        ):
            raise HomeAssistantError(
                f"Statistics readback failed for {statistic_id}: "
                f"expected ({expected_start}, {expected_sum}), got ({actual_start}, {actual_sum})"
            )


async def async_publish_statistics(
    hass: HomeAssistant,
    entry_id: str,
    name: str,
    daily: dict[str, dict[str, object]],
) -> str:
    """Replace this entry's external statistics with the current daily history.

    The clear and imports are queued in order, then a recorder synchronization
    barrier is awaited. Replacing all three owned IDs removes stale rows when a
    corrected bill changes an allocation period, while repeated calls remain
    idempotent.
    """
    statistic_ids = _entry_statistic_ids(entry_id)
    series = await hass.async_add_executor_job(_build_series, daily)

    async with _publish_lock(hass, entry_id.lower()):
        recorder = get_instance(hass)
        recorder.async_clear_statistics(list(statistic_ids.values()))

        for metric in _METRICS:
            if rows := series.get(metric):
                async_add_external_statistics(
                    hass,
                    _metadata(statistic_ids[metric], name, metric),
                    rows,
                )

        await recorder.async_block_till_done()
        await _async_verify_reimport(recorder, hass, statistic_ids, series)

    # An empty allocation means the source only has month-level fallback data.
    # Its owned old rows were still cleared above, but there is no honest date
    # on which to place replacement long-term statistics.
    return STATUS_PUBLISHED if series else STATUS_PARTIAL


async def async_clear_statistics(hass: HomeAssistant, entry_id: str) -> str:
    """Explicitly remove only the external statistics owned by one entry."""
    statistic_ids = _entry_statistic_ids(entry_id)
    async with _publish_lock(hass, entry_id.lower()):
        recorder = get_instance(hass)
        recorder.async_clear_statistics(list(statistic_ids.values()))
        await recorder.async_block_till_done()
        await _async_verify_reimport(recorder, hass, statistic_ids, {})
    return STATUS_CLEARED
