"""UI 與 entity contract tests；沒有 Home Assistant 套件的純 client CI 會跳過。"""

import math

import pytest

pytest.importorskip("homeassistant")

from homeassistant.components.sensor import SensorStateClass

from custom_components.taiwater.config_flow import (
    _normalize_credentials,
    _normalize_options,
)
from custom_components.taiwater.const import DEFAULT_OPTIONS
from custom_components.taiwater.normalize import FEE_LABELS
from custom_components.taiwater.sensor import (
    FEE_SENSORS,
    SENSORS,
    _sanitized_bill_fields,
)


def test_options_normalize_zero_seconds_but_reject_other_seconds() -> None:
    values, errors = _normalize_options(
        {"query_time": "09:00:00"}, DEFAULT_OPTIONS
    )
    assert errors == {}
    assert values["query_time"] == "09:00"

    _, errors = _normalize_options(
        {"query_time": "09:00:01"}, DEFAULT_OPTIONS
    )
    assert errors["query_time"] == "invalid_query_time"


def test_omitted_carbon_factor_clears_previous_estimate() -> None:
    current = {**DEFAULT_OPTIONS, "carbon_factor": 0.15, "carbon_factor_source": "測試來源 2024"}
    values, errors = _normalize_options({key: value for key, value in current.items() if key != "carbon_factor"}, current)
    assert errors == {}
    assert values["carbon_factor"] is None


@pytest.mark.parametrize("factor", [math.nan, math.inf, -math.inf])
def test_options_reject_non_finite_carbon_factor(factor: float) -> None:
    _, errors = _normalize_options(
        {"carbon_factor": factor, "carbon_factor_source": "test"},
        DEFAULT_OPTIONS,
    )
    assert errors["carbon_factor"] == "invalid_carbon_factor"


def test_credentials_normalize_water_id_and_history_limit() -> None:
    values, errors = _normalize_credentials(
        {
            "water_id": "A12-345 67890",
            "customer_name": " 測試戶名 ",
            "name": " 我的台水 ",
            "ocr_url": "",
            "history_limit": 6,
        }
    )
    assert errors == {}
    assert values["water_id"] == "A1234567890"
    assert values["history_limit"] == 6


def test_latest_bill_attributes_remove_identifiers() -> None:
    assert _sanitized_bill_fields(
        {
            "基本費": "50",
            "水號": "12345678901",
            "customer_name": "private",
            "巢狀": {"raw": "response"},
        }
    ) == {"基本費": "50"}


def test_fee_entities_are_fixed_monetary_sensors() -> None:
    assert len(FEE_SENSORS) == len(FEE_LABELS)
    assert len({item.key for item in FEE_SENSORS}) == len(FEE_LABELS)
    assert all(item.key.startswith("fee_") for item in FEE_SENSORS)
    assert all(item.native_unit_of_measurement == "TWD" for item in FEE_SENSORS)


def test_bill_quantities_are_not_total_increasing() -> None:
    by_key = {item.key: item for item in SENSORS}
    entity_metrics = (
        "usage_m3",
        "total_twd",
        "carbon_kg",
        "monthly_water",
        "monthly_cost",
        "monthly_carbon",
        "year_water",
        "year_cost",
        "year_carbon",
    )
    assert all(by_key[key].state_class is None for key in entity_metrics)
    assert not any(
        item.state_class is SensorStateClass.TOTAL_INCREASING for item in SENSORS
    )
