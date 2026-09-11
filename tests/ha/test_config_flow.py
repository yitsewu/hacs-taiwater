"""Home Assistant config-flow and options-flow integration tests."""

import pytest

pytest.importorskip("homeassistant")

from datetime import time
from hashlib import sha256
from unittest.mock import MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from custom_components.taiwater import config_flow
from custom_components.taiwater.const import DEFAULT_OPTIONS, DOMAIN

from conftest import CUSTOMER_NAME, ENTRY_NAME, WATER_ID


async def _start_user_flow(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )


def _credentials(water_id=WATER_ID):
    return {
        "water_id": water_id,
        "customer_name": CUSTOMER_NAME,
        "name": ENTRY_NAME,
        "ocr_url": "",
    }


async def test_user_flow_validates_credentials_and_creates_entry(
    hass, bill_result, mock_setup_integration
):
    result_data = bill_result()
    with patch.object(
        config_flow.client, "validate_credentials", return_value=result_data
    ) as validate:
        result = await _start_user_flow(hass)
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _credentials()
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == ENTRY_NAME
    assert result["data"] == {
        "water_id": WATER_ID,
        "customer_name": CUSTOMER_NAME,
        "name": ENTRY_NAME,
    }
    assert result["options"] == {**DEFAULT_OPTIONS, "ocr_url": ""}
    validate.assert_called_once_with(WATER_ID, CUSTOMER_NAME, "")
    await hass.async_block_till_done()
    mock_setup_integration.assert_awaited_once()


async def test_multiple_accounts_allowed_but_duplicate_aborts(
    hass, bill_result, mock_setup_integration
):
    second_id = "B0000000002"
    with patch.object(
        config_flow.client, "validate_credentials", return_value=bill_result()
    ) as validate:
        for water_id in (WATER_ID, second_id):
            result = await _start_user_flow(hass)
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], _credentials(water_id)
            )
            assert result["type"] is FlowResultType.CREATE_ENTRY

        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _credentials(WATER_ID)
        )

    assert len(hass.config_entries.async_entries(DOMAIN)) == 2
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert validate.call_count == 2


async def test_ocr_failure_falls_back_to_manual_and_seeds_accepted_result(
    hass, bill_result, mock_setup_integration
):
    challenge = MagicMock()
    accepted = bill_result(ocr_status="not_needed", verification_status="accepted")
    with (
        patch.object(
            config_flow.client,
            "validate_credentials",
            side_effect=config_flow.client.QueryError(
                "ocr_failed", ocr_status="failed"
            ),
        ),
        patch.object(config_flow.client, "prepare_manual", return_value=challenge),
        patch.object(
            config_flow.client, "submit_manual", return_value=accepted
        ) as submit,
        patch.object(
            config_flow, "register_manual_image", return_value="/captcha/synthetic"
        ),
        patch.object(config_flow, "clear_manual_image"),
    ):
        result = await _start_user_flow(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _credentials()
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "manual"
        assert result["description_placeholders"]["captcha_url"] == "/captcha/synthetic"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "AB12"}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    account_id = sha256(WATER_ID.encode("ascii")).hexdigest()
    assert hass.data[DOMAIN]["seeds"][account_id] == accepted
    submit.assert_called_once_with(
        challenge, WATER_ID, CUSTOMER_NAME, "AB12", history_limit=None
    )


async def test_options_ui_time_value_is_persisted_as_hh_mm(hass, config_entry):
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.MENU

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "settings"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "settings"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            **DEFAULT_OPTIONS,
            "query_time": time(7, 5),
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["query_time"] == "07:05"
    assert config_entry.options["query_time"] == "07:05"
