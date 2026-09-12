"""Tests exercising the integration through Home Assistant's real entry pipeline."""

import pytest

pytest.importorskip("homeassistant")

from copy import deepcopy
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import entity_registry as er

from custom_components.taiwater import client
from custom_components.taiwater.const import DOMAIN


def _entry_entities(hass, config_entry):
    return er.async_entries_for_config_entry(
        er.async_get(hass), config_entry.entry_id
    )


def _entity_id(hass, config_entry, platform, key):
    unique_id = f"{config_entry.entry_id}_{key}"
    return next(
        item.entity_id
        for item in _entry_entities(hass, config_entry)
        if item.domain == platform and item.unique_id == unique_id
    )


async def _setup_seeded(hass, config_entry, result):
    hass.data.setdefault(DOMAIN, {}).setdefault("seeds", {})[
        config_entry.unique_id
    ] = deepcopy(result)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return config_entry.runtime_data


async def test_setup_entry_creates_sensor_states_and_keeps_ocr_verification_separate(
    hass, config_entry, bill_result, mock_statistics
):
    coordinator = await _setup_seeded(
        hass,
        config_entry,
        bill_result(ocr_status="failed", verification_status="accepted"),
    )

    sensor_entries = [
        item for item in _entry_entities(hass, config_entry) if item.domain == "sensor"
    ]
    assert len(sensor_entries) > 30
    assert all(hass.states.get(item.entity_id) is not None for item in sensor_entries)
    assert hass.states.get(_entity_id(hass, config_entry, "sensor", "usage_m3")).state == "61"
    assert hass.states.get(_entity_id(hass, config_entry, "sensor", "total_twd")).state == "610"
    assert hass.states.get(_entity_id(hass, config_entry, "sensor", "period_start")).state == "2026-07-01"
    assert hass.states.get(_entity_id(hass, config_entry, "sensor", "period_end")).state == "2026-08-31"
    assert coordinator.data["ocr_success"] is False
    assert coordinator.data["verification_success"] is True
    assert hass.states.get(
        _entity_id(hass, config_entry, "binary_sensor", "ocr_success")
    ).state == "off"
    assert hass.states.get(
        _entity_id(hass, config_entry, "binary_sensor", "verification_success")
    ).state == "on"


async def test_latest_button_dispatches_exactly_history_false(
    hass, config_entry, bill_result, mock_statistics
):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    button_id = _entity_id(hass, config_entry, "button", "query_latest")

    with patch.object(coordinator, "async_query", new_callable=AsyncMock) as query:
        await hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True
        )

    query.assert_awaited_once_with(history=False)


async def test_failed_query_preserves_bill_and_success_timestamps(
    hass, config_entry, bill_result, mock_statistics
):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    bills_before = deepcopy(coordinator.bills)
    latest_before = coordinator.data["latest_month"]
    success_before = coordinator.data["last_success"]
    fetched_before = coordinator.data["bill_fetched_at"]

    with (
        patch.object(
            client,
            "query",
            side_effect=client.QueryError(
                "cannot_connect",
                ocr_status="failed",
                verification_status="not_run",
            ),
        ),
        patch.object(
            client,
            "now",
            side_effect=[
                "2026-09-02T02:00:00+00:00",
                "2026-09-02T02:00:01+00:00",
            ],
        ),
    ):
        with pytest.raises(client.QueryError) as error:
            await coordinator.async_query(history=False)

    assert error.value.code == "cannot_connect"
    assert coordinator.bills == bills_before
    assert coordinator.data["latest_month"] == latest_before
    assert coordinator.data["last_success"] == success_before
    assert coordinator.data["bill_fetched_at"] == fetched_before
    assert coordinator.data["last_attempt"] == "2026-09-02T02:00:00+00:00"
    assert coordinator.data["last_failure"] == "2026-09-02T02:00:01+00:00"
    assert coordinator.data["query_status"] == "failed"


async def test_seeded_storage_restart_does_not_duplicate_bill(
    hass, config_entry, bill_result, mock_statistics
):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    assert list(coordinator.bills) == ["2026-08"]
    assert config_entry.unique_id not in hass.data[DOMAIN]["seeds"]

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    restarted = config_entry.runtime_data
    assert list(restarted.bills) == ["2026-08"]
    assert restarted.data["imported_count"] == 1


async def test_manual_query_does_not_move_next_scheduled_query(
    hass, config_entry, bill_result, mock_statistics
):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    next_query = coordinator.data["next_query"]

    with patch.object(
        client,
        "submit_manual",
        return_value=bill_result(
            ocr_status="not_needed",
            verification_status="accepted",
            finished_at="2026-09-03T03:00:00+00:00",
        ),
    ) as submit:
        await coordinator.async_submit_manual(object(), "AB12")

    assert coordinator.data["next_query"] == next_query
    assert submit.call_count == 1


async def test_disabled_switch_persists_entry_option(
    hass, config_entry, bill_result, mock_statistics
):
    await _setup_seeded(hass, config_entry, bill_result())
    switch_id = _entity_id(hass, config_entry, "switch", "enabled")

    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_id}, blocking=True
    )
    await hass.async_block_till_done(wait_background_tasks=True)

    assert config_entry.options["enabled"] is False
    assert config_entry.runtime_data.data["enabled"] is False
    assert config_entry.runtime_data.data["next_query"] is None
    assert hass.states.get(switch_id).state == "off"


async def test_repeated_history_query_is_idempotent(
    hass, config_entry, bill_result, mock_statistics
):
    result = bill_result()
    coordinator = await _setup_seeded(hass, config_entry, result)
    mock_statistics.reset_mock()

    with patch.object(client, "query", return_value=deepcopy(result)) as query:
        await coordinator.async_query(history=True)
        await coordinator.async_query(history=True)

    assert query.call_count == 2
    assert list(coordinator.bills) == ["2026-08"]
    assert coordinator.data["imported_count"] == 1
    assert coordinator.data["history_complete"] is True
    mock_statistics.assert_not_awaited()
