"""台灣自來水排程開關。"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import TaiWaterEntity, safe_error_code

DESCRIPTION = SwitchEntityDescription(key="enabled", translation_key="enabled")


class TaiWaterSwitch(TaiWaterEntity, SwitchEntity):
    """啟用或停用 coordinator 排程。"""

    entity_description = DESCRIPTION

    def __init__(self, entry: ConfigEntry, coordinator: Any) -> None:
        super().__init__(entry, coordinator, DESCRIPTION.key)

    @property
    def is_on(self) -> bool | None:
        value = self.coordinator_value()
        return value if isinstance(value, bool) else None

    async def _async_set_enabled(self, enabled: bool) -> None:
        try:
            await self.coordinator.async_set_enabled(enabled)
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_enabled_failed",
                translation_placeholders={"code": safe_error_code(err)},
            ) from err

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_enabled(False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """設定排程開關。"""
    async_add_entities([TaiWaterSwitch(entry, entry.runtime_data)])
