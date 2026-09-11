"""讓 HA 自動化讀取已保存的帳單與分攤，不觸發原站查詢。"""
import voluptuous as vol
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from . import client
from .const import DOMAIN


def async_register_services(hass):
    if hass.services.has_service(DOMAIN, "get_history"):
        return

    def coordinator_for(call):
        entry = hass.config_entries.async_get_entry(call.data["config_entry_id"])
        if not entry or entry.domain != DOMAIN or not getattr(entry, "runtime_data", None):
            raise ServiceValidationError("請選擇已載入的台灣自來水公司整合")
        return entry.runtime_data

    async def history(call):
        coordinator = coordinator_for(call)
        limit = call.data["limit"]
        months = sorted(coordinator.bills, reverse=True)[:limit]
        return {"bills": [coordinator.bills[month] for month in months],
                "monthly_estimates": coordinator.monthly,
                "available_months": coordinator.available_months,
                "history_complete": coordinator.data["history_complete"],
                "total_saved_bills": len(coordinator.bills)}

    async def query_history(call):
        coordinator = coordinator_for(call)
        return {"records": [dict(record) for record in reversed(coordinator.query_history[-call.data["limit"]:])],
                "total": len(coordinator.query_history), "retention_limit": 100}

    async def query_month(call):
        coordinator = coordinator_for(call)
        try:
            record = await coordinator.async_query(requested_month=call.data["month"], trigger="action")
        except client.QueryError as error:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="query_failed",
                                         translation_placeholders={"code": error.code}) from None
        return {"query": record, "bill": coordinator.bills[call.data["month"]]}

    hass.services.async_register(DOMAIN, "get_history", history,
        schema=vol.Schema({vol.Required("config_entry_id"): str,
                           vol.Optional("limit", default=50): vol.All(vol.Coerce(int), vol.Range(min=1, max=200))}),
        supports_response=SupportsResponse.ONLY)

    hass.services.async_register(DOMAIN, "get_query_history", query_history,
        schema=vol.Schema({vol.Required("config_entry_id"): str,
                           vol.Optional("limit", default=25): vol.All(vol.Coerce(int), vol.Range(min=1, max=100))}),
        supports_response=SupportsResponse.ONLY)
    hass.services.async_register(DOMAIN, "query_month", query_month,
        schema=vol.Schema({vol.Required("config_entry_id"): str,
                           vol.Required("month"): vol.All(str, vol.Match(r"^[0-9]{4}-(0[1-9]|1[0-2])$"))}),
        supports_response=SupportsResponse.OPTIONAL)
