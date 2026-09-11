"""台灣自來水整合的共用 entity 基底。"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME

_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")


def safe_error_code(error: BaseException) -> str:
    """只回傳可翻譯的固定錯誤碼，不洩漏例外或使用者資料。"""
    code = getattr(error, "code", "unknown")
    return code if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code) else "unknown"


class TaiWaterEntity(CoordinatorEntity[Any]):
    """所有台水 entities 共用的 coordinator 與 device identity。"""

    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: Any, key: str) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        # entry_id 是 HA 產生的隨機值，不把水號或戶名放入 entity/device ID。
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=str(entry.data.get("name") or NAME),
            manufacturer=NAME,
            model="水費查詢服務",
        )

    def coordinator_value(self) -> Any:
        """依 flat coordinator contract 讀值。"""
        data = self.coordinator.data
        return data.get(self._key) if isinstance(data, dict) else None
