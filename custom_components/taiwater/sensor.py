"""台灣自來水感測器平台。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .entity import TaiWaterEntity
from .normalize import DATE_LABELS, FEE_LABELS


def _identity(value: Any) -> Any:
    return value


def _number(value: Any) -> int | float | Decimal | None:
    """只接受有限數值；不把任意文字當 entity state。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, Decimal) and not value.is_finite():
            return None
        return value
    if isinstance(value, str):
        try:
            parsed = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    return None


def _timestamp(value: Any) -> datetime | None:
    """安全解析 ISO timestamp，並保證回傳 timezone-aware datetime。"""
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
    else:
        return None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _date(value: Any) -> date | None:
    """安全解析 ISO date；timestamp 只取其日期。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


_PRIVATE_FIELD_MARKERS = (
    "water_id",
    "waterno",
    "customer",
    "address",
    "phone",
    "email",
    "水號",
    "戶名",
    "姓名",
    "地址",
    "電話",
    "證號",
    "表號",
)


def _sanitized_bill_fields(value: Any) -> dict[str, str | int | float | bool | None]:
    """僅保留簡短 scalar 費用欄位，排除可識別資料與巢狀原始內容。"""
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, str | int | float | bool | None] = {}
    for raw_key, raw_value in value.items():
        if len(sanitized) >= 64 or not isinstance(raw_key, str):
            break
        key = " ".join(raw_key.split()).strip()
        folded = key.casefold().replace("_", "")
        if not key or len(key) > 64 or any(
            marker.replace("_", "") in folded for marker in _PRIVATE_FIELD_MARKERS
        ):
            continue
        if raw_value is None or isinstance(raw_value, (bool, int, float)):
            sanitized[key] = raw_value
        elif isinstance(raw_value, str):
            sanitized[key] = " ".join(raw_value.split())[:256]
    return sanitized


@dataclass(frozen=True, kw_only=True)
class TaiWaterSensorDescription(SensorEntityDescription):
    """台水感測器描述。"""

    value_fn: Callable[[Any], Any] = _identity
    enum_options: tuple[str, ...] | None = None
    bill_fields: bool = False
    data_map: str | None = None
    data_label: str | None = None


def _label_key(prefix: str, label: str) -> str:
    """由固定中文 label 產生不含帳戶資料的穩定 entity key。"""
    digest = hashlib.sha1(label.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]
    return f"{prefix}_{digest}"


FEE_SENSORS: tuple[TaiWaterSensorDescription, ...] = tuple(
    TaiWaterSensorDescription(
        key=_label_key("fee", label),
        translation_key=_label_key("fee", label),
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="TWD",
        value_fn=_number,
        data_map="fees",
        data_label=label,
    )
    for label in FEE_LABELS
)

DATE_SENSORS: tuple[TaiWaterSensorDescription, ...] = tuple(
    TaiWaterSensorDescription(
        key=_label_key("date", label),
        translation_key=_label_key("date", label),
        device_class=SensorDeviceClass.DATE,
        value_fn=_date,
        data_map="dates",
        data_label=label,
    )
    for label in DATE_LABELS
)


CORE_SENSORS: tuple[TaiWaterSensorDescription, ...] = (
    TaiWaterSensorDescription(
        key="latest_month", translation_key="latest_bill", bill_fields=True
    ),
    TaiWaterSensorDescription(
        key="usage_m3",
        translation_key="usage_m3",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="total_twd",
        translation_key="total_twd",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="TWD",
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="carbon_kg",
        translation_key="carbon_kg",
        native_unit_of_measurement="kgCO2e",
        value_fn=_number,
    ),
    TaiWaterSensorDescription(key="carbon_source", translation_key="carbon_source"),
    TaiWaterSensorDescription(
        key="period_start",
        translation_key="period_start",
        device_class=SensorDeviceClass.DATE,
        value_fn=_date,
    ),
    TaiWaterSensorDescription(
        key="period_end",
        translation_key="period_end",
        device_class=SensorDeviceClass.DATE,
        value_fn=_date,
    ),
    TaiWaterSensorDescription(
        key="payment_due",
        translation_key="payment_due",
        device_class=SensorDeviceClass.DATE,
        value_fn=_date,
    ),
    TaiWaterSensorDescription(key="payment_status", translation_key="payment_status"),
    TaiWaterSensorDescription(
        key="daily_usage",
        translation_key="daily_usage",
        native_unit_of_measurement="m³/日",
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="average_unit_cost",
        translation_key="average_unit_cost",
        native_unit_of_measurement="TWD/m³",
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="last_attempt",
        translation_key="last_attempt",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_timestamp,
    ),
    TaiWaterSensorDescription(
        key="last_success",
        translation_key="last_success",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_timestamp,
    ),
    TaiWaterSensorDescription(
        key="next_query",
        translation_key="next_query",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_timestamp,
    ),
    TaiWaterSensorDescription(
        key="last_failure",
        translation_key="last_failure",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_timestamp,
    ),
    TaiWaterSensorDescription(
        key="query_status",
        translation_key="query_status",
        device_class=SensorDeviceClass.ENUM,
        enum_options=("never", "running", "success", "partial", "failed"),
    ),
    TaiWaterSensorDescription(
        key="ocr_status",
        translation_key="ocr_status",
        device_class=SensorDeviceClass.ENUM,
        enum_options=("not_run", "success", "failed", "unavailable", "not_needed"),
    ),
    TaiWaterSensorDescription(
        key="ocr_backend", translation_key="ocr_backend", device_class=SensorDeviceClass.ENUM,
        enum_options=("not_run", "builtin", "external", "manual"),
    ),
    TaiWaterSensorDescription(
        key="verification_status",
        translation_key="verification_status",
        device_class=SensorDeviceClass.ENUM,
        enum_options=("not_run", "accepted", "rejected", "unknown"),
    ),
    TaiWaterSensorDescription(key="error_code", translation_key="error_code"),
    TaiWaterSensorDescription(
        key="available_count", translation_key="available_count", value_fn=_number
    ),
    TaiWaterSensorDescription(
        key="imported_count", translation_key="imported_count", value_fn=_number
    ),
    TaiWaterSensorDescription(
        key="statistics_status", translation_key="statistics_status",
        device_class=SensorDeviceClass.ENUM,
        enum_options=("not_imported", "pending", "running", "published", "partial", "failed"),
    ),
    TaiWaterSensorDescription(
        key="statistics_updated", translation_key="statistics_updated",
        device_class=SensorDeviceClass.TIMESTAMP, value_fn=_timestamp,
    ),
    TaiWaterSensorDescription(
        key="statistics_duration", translation_key="statistics_duration",
        device_class=SensorDeviceClass.DURATION, native_unit_of_measurement="s", value_fn=_number,
    ),
    # 以下為 coordinator 已配置好的目前月／年摘要；長期統計由 statistics.py
    # 另行發布，這些會切換月份／年度的顯示值不宣告 state class。
    TaiWaterSensorDescription(
        key="monthly_water",
        translation_key="monthly_water",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="monthly_cost",
        translation_key="monthly_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="TWD",
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="monthly_carbon",
        translation_key="monthly_carbon",
        native_unit_of_measurement="kgCO2e",
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="latest_allocated_month",
        translation_key="latest_allocated_month",
    ),
    TaiWaterSensorDescription(
        key="year_water",
        translation_key="year_water",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="year_cost",
        translation_key="year_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="TWD",
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="year_carbon",
        translation_key="year_carbon",
        native_unit_of_measurement="kgCO2e",
        value_fn=_number,
    ),
    TaiWaterSensorDescription(
        key="year_covered_months",
        translation_key="year_covered_months",
        value_fn=_number,
    ),
)

SENSORS = CORE_SENSORS + FEE_SENSORS + DATE_SENSORS


class TaiWaterSensor(TaiWaterEntity, SensorEntity):
    """由 coordinator 記憶體資料提供狀態的感測器。"""

    entity_description: TaiWaterSensorDescription

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: Any,
        description: TaiWaterSensorDescription,
    ) -> None:
        super().__init__(entry, coordinator, description.key)
        self.entity_description = description
        if description.enum_options is not None:
            self._attr_options = list(description.enum_options)

    @property
    def native_value(self) -> Any:
        description = self.entity_description
        if description.data_map is not None:
            data = self.coordinator.data
            values = data.get(description.data_map) if isinstance(data, dict) else None
            value = (
                values.get(description.data_label)
                if isinstance(values, dict) and description.data_label is not None
                else None
            )
        else:
            value = self.coordinator_value()
        return description.value_fn(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self.entity_description.bill_fields:
            return None
        data = self.coordinator.data
        fields = _sanitized_bill_fields(
            data.get("bill_fields") if isinstance(data, dict) else None
        )
        return fields or None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """設定台水感測器。"""
    coordinator = entry.runtime_data
    async_add_entities(TaiWaterSensor(entry, coordinator, item) for item in SENSORS)
