"""Fixtures for tests that run against a real Home Assistant test instance."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from unittest.mock import AsyncMock, patch

import pytest


WATER_ID = "A0000000001"
CUSTOMER_NAME = "測試帳戶"
ENTRY_NAME = "測試用水"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant discover this repository's custom integration."""
    yield


@pytest.fixture(autouse=True)
def skip_requirements():
    """Keep these tests offline; the integration manifest has no requirements."""
    with patch(
        "homeassistant.requirements.async_get_integration_with_requirements",
        new_callable=AsyncMock,
    ):
        yield


@pytest.fixture
def mock_setup_integration():
    """Prevent config-flow entry creation from starting the full integration."""
    with patch(
        "custom_components.taiwater.async_setup_entry",
        new_callable=AsyncMock,
        return_value=True,
    ) as mocked:
        yield mocked


@pytest.fixture
def mock_statistics():
    """Isolate coordinator behavior from recorder publication."""
    with patch(
        "custom_components.taiwater.coordinator.async_publish_statistics",
        new_callable=AsyncMock,
        return_value="published",
    ) as mocked:
        yield mocked


@pytest.fixture
def bill_result():
    """Return synthetic upstream results with no real account information."""

    def make_result(
        *,
        status="success",
        ocr_status="success",
        verification_status="accepted",
        error_code=None,
        finished_at="2026-09-01T01:02:03+00:00",
    ):
        return {
            "bills": [
                {
                    "month": "2026-08",
                    "fields": {
                        "本期計費用水期間": "1150701 ～ 1150831",
                        "用水度數": "61",
                        "應繳總金額": "610元",
                    },
                    "fetched_at": "2026-09-01T01:00:00+00:00",
                }
            ],
            "available_months": ["2026-08"],
            "ocr_status": ocr_status,
            "verification_status": verification_status,
            "error_code": error_code,
            "status": status,
            "finished_at": finished_at,
        }

    return make_result


@pytest.fixture
def config_entry():
    """Create an unloaded TaiWater config entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.taiwater.const import DEFAULT_OPTIONS, DOMAIN

    return MockConfigEntry(
        domain=DOMAIN,
        title=ENTRY_NAME,
        unique_id=sha256(WATER_ID.encode("ascii")).hexdigest(),
        data={
            "water_id": WATER_ID,
            "customer_name": CUSTOMER_NAME,
            "name": ENTRY_NAME,
        },
        options=deepcopy(DEFAULT_OPTIONS),
    )
