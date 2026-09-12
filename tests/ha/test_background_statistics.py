"""Slow recorder work must not hold portal queries or lose newer bill revisions."""
import asyncio
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch

import pytest

from homeassistant.helpers import entity_registry as er

from custom_components.taiwater import client
from custom_components.taiwater.const import DOMAIN


async def setup(hass, entry, result):
    hass.data.setdefault(DOMAIN, {}).setdefault("seeds", {})[entry.unique_id] = deepcopy(result)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data


def button_id(hass, entry, key):
    return er.async_get(hass).async_get_entity_id("button", DOMAIN, f"{entry.entry_id}_{key}")


async def test_slow_statistics_release_query_and_coalesce_latest_correction(
    hass, config_entry, bill_result, mock_statistics
):
    result = bill_result()
    coordinator = await setup(hass, config_entry, result)
    started, release = asyncio.Event(), asyncio.Event()
    snapshots = []

    async def slow_publish(_hass, _entry, _name, daily):
        # Revision assertion, independent of Decimal division's sub-cent residue.
        snapshots.append(sum(day["cost"] for day in daily.values()).quantize(Decimal("0.000000001")))
        started.set()
        await release.wait()
        return "published"

    mock_statistics.side_effect = slow_publish
    result["bills"][0]["fields"]["應繳總金額"] = "700元"
    try:
        # Bound the query itself: it must return while publication is still blocked.
        with patch.object(client, "query", return_value=result):
            record = await asyncio.wait_for(coordinator.async_query(), 5)
        await asyncio.wait_for(started.wait(), 5)
        assert record["status"] == "success" and record["finished_at"]
        assert not coordinator._query_lock.locked()
        assert coordinator.data["statistics_status"] == "running"
        assert hass.states.get(button_id(hass, config_entry, "query_latest")).state != "unavailable"
        assert hass.states.get(button_id(hass, config_entry, "rebuild_statistics")).state == "unavailable"
        for amount in (800, 900):
            result["bills"][0]["fields"]["應繳總金額"] = f"{amount}元"
            with patch.object(client, "query", return_value=deepcopy(result)):
                await asyncio.wait_for(coordinator.async_query(), 5)
        assert snapshots == [Decimal(700)]
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert snapshots == [Decimal(700), Decimal(900)]
    assert coordinator.bills["2026-08"]["total_twd"] == 900
    assert coordinator.data["statistics_status"] == "published"
    saved = await coordinator.store.async_load()
    assert saved["bills"]["2026-08"]["total_twd"] == 900
    assert all(row["status"] == "success" for row in saved["query_history"])


async def test_statistics_failure_and_retry_never_query_portal_or_change_query_result(
    hass, config_entry, bill_result, mock_statistics
):
    coordinator = await setup(hass, config_entry, bill_result())
    before = deepcopy(coordinator.query_history)
    mock_statistics.side_effect = RuntimeError("synthetic recorder failure")
    with patch.object(client, "query") as query:
        await hass.services.async_call("button", "press", {
            "entity_id": button_id(hass, config_entry, "rebuild_statistics")
        }, blocking=True)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert coordinator.data["statistics_status"] == "failed"
        assert coordinator.data["query_status"] == "success"
        assert coordinator.data["error_code"] is None
        mock_statistics.side_effect = None
        coordinator.async_rebuild_statistics(force=True)
        await hass.async_block_till_done(wait_background_tasks=True)
        query.assert_not_called()
    assert coordinator.data["statistics_status"] == "published"
    assert coordinator.data["statistics_duration"] >= 0
    assert coordinator.data["statistics_updated"]
    assert coordinator.query_history == before


async def test_unload_drains_pending_statistics(hass, config_entry, bill_result, mock_statistics):
    coordinator = await setup(hass, config_entry, bill_result())
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_publish(*_args):
        started.set()
        await release.wait()
        return "published"

    mock_statistics.side_effect = slow_publish
    coordinator.async_rebuild_statistics(force=True)
    await asyncio.wait_for(started.wait(), 5)
    unload = asyncio.create_task(hass.config_entries.async_unload(config_entry.entry_id))
    try:
        await asyncio.sleep(0)
        assert not unload.done()
    finally:
        release.set()
    assert await asyncio.wait_for(unload, 5)
    assert coordinator._statistics_task.done()
    assert (await coordinator.store.async_load())["status"]["statistics_status"] == "published"


async def test_setup_finishes_before_slow_recorder(hass, config_entry, bill_result, mock_statistics):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_publish(*_args):
        started.set()
        await release.wait()
        return "published"

    mock_statistics.side_effect = slow_publish
    hass.data.setdefault(DOMAIN, {}).setdefault("seeds", {})[config_entry.unique_id] = bill_result()
    config_entry.add_to_hass(hass)
    try:
        assert await asyncio.wait_for(hass.config_entries.async_setup(config_entry.entry_id), 5)
        await asyncio.wait_for(started.wait(), 5)
        assert config_entry.runtime_data.data["query_status"] == "success"
        assert config_entry.runtime_data.data["statistics_status"] == "running"
        # Bootstrap's normal task drain must finish even while recorder is blocked.
        await asyncio.wait_for(hass.async_block_till_done(), 5)
    finally:
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)


async def test_busy_action_uses_wait_message_and_keeps_last_result(
    hass, config_entry, bill_result, mock_statistics
):
    from homeassistant.exceptions import ServiceValidationError

    coordinator = await setup(hass, config_entry, bill_result())
    before = deepcopy(coordinator.query_history)
    async with coordinator._query_lock:
        with pytest.raises(ServiceValidationError) as error:
            await hass.services.async_call(DOMAIN, "query_month", {
                "config_entry_id": config_entry.entry_id, "month": "2026-08"
            }, blocking=True)
    assert error.value.translation_key == "query_busy"
    assert coordinator.query_history == before
    assert coordinator.data["query_status"] == "success"
