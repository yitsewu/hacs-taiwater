"""讓 HA 自動化讀取已保存的帳單與分攤，不觸發原站查詢。"""
import voluptuous as vol
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN


def async_register_services(hass):
    if hass.services.has_service(DOMAIN, "get_history"):
        return

    async def history(call):
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if not entry or entry.domain != DOMAIN or not getattr(entry, "runtime_data", None):
            raise ServiceValidationError("請選擇已載入的台灣自來水公司整合")
        coordinator = entry.runtime_data
        limit = call.data["limit"]
        months = sorted(coordinator.bills, reverse=True)[:limit]
        return {"bills": [coordinator.bills[month] for month in months],
                "monthly_estimates": coordinator.monthly,
                "available_months": coordinator.available_months,
                "history_complete": coordinator.data["history_complete"],
                "total_saved_bills": len(coordinator.bills)}

    hass.services.async_register(DOMAIN, "get_history", history,
        schema=vol.Schema({vol.Required("config_entry_id"): str,
                           vol.Optional("limit", default=50): vol.All(vol.Coerce(int), vol.Range(min=1, max=200))}),
        supports_response=SupportsResponse.ONLY)
