"""查詢歷程與指定帳期 action 的真實 HA 流程驗證。"""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

pytest.importorskip("homeassistant")

from homeassistant.exceptions import ServiceValidationError
from custom_components.taiwater import client
from custom_components.taiwater.const import DOMAIN


async def _setup_seeded(hass, entry, result):
    hass.data.setdefault(DOMAIN, {}).setdefault("seeds", {})[entry.unique_id] = deepcopy(result)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def call(hass, entry, action, **data):
    return await hass.services.async_call("taiwater", action,
        {"config_entry_id": entry.entry_id, **data}, blocking=True, return_response=True)


async def test_query_history_failure_partial_and_restart(hass, config_entry, bill_result, mock_statistics):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    with patch.object(client, "query", side_effect=client.QueryError("upstream_rejected", ocr_status="success", verification_status="rejected")):
        await coordinator.async_query(raise_errors=False, trigger="schedule")
    with patch.object(client, "query", return_value=bill_result(status="partial", error_code="history_incomplete")):
        await coordinator.async_query(history=True)
    response = await call(hass, config_entry, "get_query_history")
    partial, failure, setup = response["records"]
    assert partial["status"] == "partial" and partial["fetched_count"] == 1
    assert failure["trigger"] == "schedule"
    assert failure["ocr_status"] == "success"
    assert failure["verification_status"] == "rejected"
    assert failure["status"] == "failed" and failure["fetched_count"] == 0
    assert failure["duration_seconds"] >= 0
    assert setup["trigger"] == "setup"
    assert len({row["query_id"] for row in response["records"]}) == 3
    encoded = json.dumps(response)
    for value in (config_entry.data["water_id"], config_entry.data["customer_name"], "cookie", "image", "customer_name"):
        assert value not in encoded
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert (await call(hass, config_entry, "get_query_history")) == response


async def test_running_record_becomes_interrupted_on_restart(hass, config_entry, bill_result, mock_statistics):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    coordinator._begin_record("action", "month", "2026-08", client.now())
    coordinator.data.update(query_status="running", query_success=None)
    await coordinator._save()
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    record = (await call(hass, config_entry, "get_query_history"))["records"][0]
    assert record["status"] == "interrupted"
    assert record["error_code"] == "interrupted"
    assert record["finished_at"] and record["duration_seconds"] is None
    assert config_entry.runtime_data.data["last_failure"] == record["finished_at"]


async def test_history_retention_and_read_does_not_query(hass, config_entry, bill_result, mock_statistics):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    for _ in range(105):
        record = coordinator._begin_record("button", "latest", None, client.now())
        coordinator._finish_record(record, client.now(), 0.1, 1)
    with patch.object(client, "query") as query:
        response = await call(hass, config_entry, "get_query_history", limit=2)
    query.assert_not_called()
    assert response["total"] == response["retention_limit"] == 100
    assert len(response["records"]) == 2
    assert response["records"][0]["query_id"] == record["query_id"]


async def test_targeted_month_updates_only_target_and_keeps_schedule(hass, config_entry, bill_result, mock_statistics):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    newest = deepcopy(coordinator.bills["2026-08"])
    next_query = coordinator.data["next_query"]
    result = bill_result()
    result["bills"][0].update(month="2026-06", fields={"本期計費用水期間": "1150501 ～ 1150630", "用水度數": "20", "應繳總金額": "200元"})
    result["available_months"] = ["2026-08", "2026-06"]
    with patch.object(client, "query", return_value=result) as query:
        response = await call(hass, config_entry, "query_month", month="2026-06")
        query.assert_called_once()
        assert query.call_args.kwargs["requested_month"] == "2026-06"
        await call(hass, config_entry, "query_month", month="2026-06")
        result["bills"][0]["fields"]["應繳總金額"] = "400元"
        await call(hass, config_entry, "query_month", month="2026-06")
        await call(hass, config_entry, "query_month", month="2026-06")
    assert response["bill"]["month"] == "2026-06"
    assert response["query"]["operation"] == "month"
    assert response["query"]["trigger"] == "action"
    assert response["query"]["fetched_count"] == 1
    assert coordinator.bills["2026-08"] == newest
    assert coordinator.bills["2026-06"]["total_twd"] == 400
    # 初始化、加入舊帳期、更正金額；相同資料重抓不重建或重複累加。
    assert mock_statistics.await_count == 3
    assert len(coordinator.bills) == 2
    assert coordinator.data["latest_month"] == "2026-08"
    assert coordinator.data["next_query"] == next_query


async def test_failed_month_action_keeps_bill_and_records_reason(hass, config_entry, bill_result, mock_statistics):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    before = deepcopy(coordinator.bills)
    with patch.object(client, "query", side_effect=client.QueryError("month_unavailable", ocr_status="success", verification_status="accepted")):
        with pytest.raises(ServiceValidationError):
            await call(hass, config_entry, "query_month", month="2026-06")
    assert coordinator.bills == before
    row = (await call(hass, config_entry, "get_query_history"))["records"][0]
    assert row["requested_month"] == "2026-06"
    assert row["error_code"] == "month_unavailable"
    assert row["ocr_status"] == "success" and row["verification_status"] == "accepted"


async def test_wrong_month_response_never_overwrites_saved_bill(hass, config_entry, bill_result, mock_statistics):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    before = deepcopy(coordinator.bills)
    with patch.object(client, "query", return_value=bill_result()):
        with pytest.raises(ServiceValidationError):
            await call(hass, config_entry, "query_month", month="2026-06")
    assert coordinator.bills == before
    assert coordinator.query_history[-1]["error_code"] == "invalid_response"
    assert coordinator.query_history[-1]["ocr_status"] == "success"


async def test_busy_action_does_not_start_second_query(hass, config_entry, bill_result, mock_statistics):
    coordinator = await _setup_seeded(hass, config_entry, bill_result())
    count = len(coordinator.query_history)
    async with coordinator._query_lock:
        with patch.object(client, "query") as query:
            with pytest.raises(ServiceValidationError):
                await call(hass, config_entry, "query_month", month="2026-08")
        query.assert_not_called()
    assert len(coordinator.query_history) == count
