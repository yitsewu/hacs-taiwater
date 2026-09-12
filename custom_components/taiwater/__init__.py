"""台灣自來水公司 Home Assistant 整合。"""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .coordinator import TaiWaterCoordinator
from .services import async_register_services


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Keep legacy accounts on their old day-based default, preserving all IDs."""
    if entry.version != 1:
        return False
    if entry.minor_version < 2:
        options = dict(entry.options)
        options.setdefault("allocation", "days")
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = TaiWaterCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_initialize()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    coordinator.async_start()
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = entry.runtime_data
    await coordinator.async_close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
