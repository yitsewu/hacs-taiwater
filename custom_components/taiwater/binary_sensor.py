"""台灣自來水診斷 binary sensors。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import TaiWaterEntity


@dataclass(frozen=True, kw_only=True)
class TaiWaterBinarySensorDescription(BinarySensorEntityDescription):
    """台水診斷 binary sensor 描述。"""

    invert: bool = False


BINARY_SENSORS: tuple[TaiWaterBinarySensorDescription, ...] = (
    TaiWaterBinarySensorDescription(
        key="query_success",
        translation_key="last_query_failed",
        entity_category=EntityCategory.DIAGNOSTIC,
        invert=True,
    ),
    TaiWaterBinarySensorDescription(
        key="ocr_success",
        translation_key="ocr_success",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TaiWaterBinarySensorDescription(
        key="verification_success",
        translation_key="verification_accepted",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TaiWaterBinarySensorDescription(
        key="history_complete",
        translation_key="history_complete",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


class TaiWaterBinarySensor(TaiWaterEntity, BinarySensorEntity):
    """由 coordinator 的三態布林值提供診斷結果。"""

    entity_description: TaiWaterBinarySensorDescription

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: Any,
        description: TaiWaterBinarySensorDescription,
    ) -> None:
        super().__init__(entry, coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator_value()
        if value is None:
            return None
        state = bool(value)
        return not state if self.entity_description.invert else state


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """設定台水診斷 binary sensors。"""
    coordinator = entry.runtime_data
    async_add_entities(
        TaiWaterBinarySensor(entry, coordinator, item) for item in BINARY_SENSORS
    )
