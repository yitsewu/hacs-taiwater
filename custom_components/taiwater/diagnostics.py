"""台灣自來水整合診斷；不包含水號、戶名、帳單或原始回應。"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DEFAULT_OPTIONS

_SAFE_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_STATUS_KEYS = (
    "query_status",
    "ocr_status",
    "verification_status",
    "statistics_status",
    "error_code",
)
_BOOLEAN_KEYS = (
    "query_success",
    "ocr_success",
    "verification_success",
    "history_complete",
    "enabled",
)
_COUNT_KEYS = ("available_count", "imported_count", "year_covered_months")


def _safe_code(value: Any) -> str | None:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """回傳足以診斷流程狀態、但不可回推帳戶的摘要。"""
    coordinator = entry.runtime_data
    data = coordinator.data if isinstance(coordinator.data, dict) else {}
    options = {**DEFAULT_OPTIONS, **entry.options}

    state: dict[str, Any] = {
        key: _safe_code(data.get(key)) for key in _STATUS_KEYS
    }
    state.update(
        {
            key: value if isinstance((value := data.get(key)), bool) else None
            for key in _BOOLEAN_KEYS
        }
    )
    state.update(
        {
            key: value
            if isinstance((value := data.get(key)), int)
            and not isinstance(value, bool)
            and value >= 0
            else None
            for key in _COUNT_KEYS
        }
    )

    return {
        "configuration": {
            "schedule": options.get("schedule"),
            "query_time": options.get("query_time"),
            "weekday": options.get("weekday"),
            "monthday": options.get("monthday"),
            "history_limit": options.get("history_limit"),
            "allocation": options.get("allocation"),
            "carbon_factor_configured": options.get("carbon_factor") is not None,
            "carbon_factor_source_configured": bool(
                options.get("carbon_factor_source")
            ),
            "ocr_url_configured": bool(options.get("ocr_url")),
        },
        "state": state,
    }
