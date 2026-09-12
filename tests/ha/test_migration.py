"""Upgrades preserve IDs and allocation choices; new defaults never rewrite history."""
from copy import deepcopy

import pytest

from custom_components.taiwater import async_migrate_entry
from custom_components.taiwater.const import DEFAULT_OPTIONS


@pytest.mark.parametrize("allocation", [None, "days", "equal"])
async def test_legacy_allocation_migration(hass, config_entry, allocation):
    config_entry.add_to_hass(hass)
    options = {"enabled": False, "history_limit": 6}
    if allocation is not None:
        options["allocation"] = allocation
    hass.config_entries.async_update_entry(config_entry, options=options, minor_version=1)
    identity = (config_entry.entry_id, config_entry.unique_id, deepcopy(dict(config_entry.data)))
    assert await async_migrate_entry(hass, config_entry)
    assert config_entry.options["allocation"] == (allocation or "days")
    assert config_entry.options["enabled"] is False
    assert config_entry.options["history_limit"] == 6
    assert config_entry.minor_version == 2
    assert (config_entry.entry_id, config_entry.unique_id, dict(config_entry.data)) == identity
    assert DEFAULT_OPTIONS["allocation"] == "equal"
    assert await async_migrate_entry(hass, config_entry)
    assert config_entry.options["allocation"] == (allocation or "days")
