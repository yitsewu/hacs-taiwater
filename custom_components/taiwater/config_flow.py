"""台灣自來水整合的 UI 設定與人工驗證流程。"""

from __future__ import annotations

from datetime import time as time_value
from functools import partial
import hashlib
import math
import re
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import client
from .const import DEFAULT_OPTIONS, DOMAIN, NAME
from .entity import safe_error_code
from .http import clear_manual_image, register_manual_image
from .ocr import async_resolve_ocr_url

CONF_WATER_ID = "water_id"
CONF_CUSTOMER_NAME = "customer_name"
CONF_NAME = "name"
CONF_OCR_URL = "ocr_url"

_WATER_ID = re.compile(r"^[A-Z0-9]{11}$")
_QUERY_TIME = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_CAPTCHA_CODE = re.compile(r"^[A-Za-z0-9]{4,8}$")
_SCHEDULES = ("daily", "weekly", "monthly")
_ALLOCATIONS = ("days", "equal")
_MANUAL_ERROR_CODES = {
    "captcha_expired",
    "captcha_required",
    "ocr_failed",
    "ocr_unavailable",
    "upstream_rejected",
}
_FLOW_ERROR_CODES = {
    "cannot_connect",
    "invalid_auth",
    "captcha_required",
    "ocr_unavailable",
    "site_changed",
    "invalid_ocr_url",
}


def _normalize_water_id(value: Any) -> str:
    """移除常見分隔字元並轉大寫，最後仍須為 11 位 ASCII 英數字。"""
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s-]+", "", value).upper()


def _account_id(water_id: str) -> str:
    return hashlib.sha256(water_id.encode("ascii")).hexdigest()


def _valid_ocr_url(value: str) -> bool:
    if not value:
        return True
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def _query_error(error: BaseException) -> str:
    code = safe_error_code(error)
    if code in {"captcha_expired", "captcha_required", "upstream_rejected"}:
        return "captcha_required"
    if code in {"ocr_failed", "ocr_unavailable"}:
        return "ocr_unavailable"
    if code in _FLOW_ERROR_CODES:
        return code
    if code in {"invalid_response", "unknown"}:
        return "site_changed"
    return "cannot_connect"


def _result_error(result: Any) -> str | None:
    if not isinstance(result, dict):
        return "site_changed"
    status = result.get("status")
    if status == "success" or (
        status == "partial"
        and isinstance(result.get("bills"), list)
        and bool(result["bills"])
    ):
        return None
    code = result.get("error_code") or status
    if isinstance(code, str):
        class ResultError(Exception):
            pass

        error = ResultError()
        error.code = code  # type: ignore[attr-defined]
        return _query_error(error)
    return "site_changed"


def _credential_schema(values: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_WATER_ID, default=values.get(CONF_WATER_ID, "")):
                selector.TextSelector(),
            vol.Required(
                CONF_CUSTOMER_NAME, default=values.get(CONF_CUSTOMER_NAME, "")
            ): selector.TextSelector(),
            vol.Required(CONF_NAME, default=values.get(CONF_NAME, NAME)):
                selector.TextSelector(),
            vol.Required(
                "history_limit", default=values.get("history_limit", 0)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, step=1, mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
    )


def _normalize_credentials(
    user_input: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    errors: dict[str, str] = {}
    water_id = _normalize_water_id(user_input.get(CONF_WATER_ID))
    customer_name = str(user_input.get(CONF_CUSTOMER_NAME, "")).strip()
    name = str(user_input.get(CONF_NAME, "")).strip()
    ocr_url = str(user_input.get(CONF_OCR_URL, "")).strip().rstrip("/")
    if not _WATER_ID.fullmatch(water_id):
        errors[CONF_WATER_ID] = "invalid_water_id"
    if not customer_name or len(customer_name) > 100:
        errors[CONF_CUSTOMER_NAME] = "invalid_customer_name"
    if not name or len(name) > 100:
        errors[CONF_NAME] = "invalid_name"
    if not _valid_ocr_url(ocr_url):
        errors[CONF_OCR_URL] = "invalid_ocr_url"
    try:
        history_limit = int(user_input.get("history_limit", 0))
    except (TypeError, ValueError):
        history_limit = -1
    if history_limit < 0:
        errors["history_limit"] = "invalid_history_limit"
    return {
        CONF_WATER_ID: water_id,
        CONF_CUSTOMER_NAME: customer_name,
        CONF_NAME: name,
        CONF_OCR_URL: ocr_url,
        "history_limit": history_limit,
    }, errors


def _options_schema(values: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required("enabled", default=values["enabled"]):
                selector.BooleanSelector(),
            vol.Required("schedule", default=values["schedule"]):
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(_SCHEDULES), translation_key="schedule"
                    )
                ),
            vol.Required("query_time", default=values["query_time"]):
                selector.TimeSelector(),
            vol.Required("weekday", default=values["weekday"]):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=6,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            vol.Required("monthday", default=values["monthday"]):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=31,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
            vol.Required("history_limit", default=values["history_limit"]):
                selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
            vol.Required("allocation", default=values["allocation"]):
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(_ALLOCATIONS), translation_key="allocation"
                    )
                ),
            vol.Optional(
                "carbon_factor",
                description={"suggested_value": values.get("carbon_factor")},
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, step=0.001, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                "carbon_factor_source",
                default=values.get("carbon_factor_source", ""),
            ): selector.TextSelector(),
            vol.Optional(CONF_OCR_URL, default=values.get(CONF_OCR_URL, "")):
                selector.TextSelector(),
        }
    )


def _normalize_options(
    user_input: dict[str, Any], current: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, str]]:
    values = {**DEFAULT_OPTIONS, **current, **user_input}
    errors: dict[str, str] = {}

    query_time_input = values.get("query_time")
    if isinstance(query_time_input, time_value):
        parsed_time = query_time_input
    else:
        try:
            parsed_time = time_value.fromisoformat(str(query_time_input))
        except ValueError:
            parsed_time = None
    if (
        parsed_time is None
        or parsed_time.second != 0
        or parsed_time.microsecond != 0
    ):
        query_time = str(query_time_input)
        errors["query_time"] = "invalid_query_time"
    else:
        query_time = parsed_time.strftime("%H:%M")

    try:
        weekday = int(values.get("weekday"))
    except (TypeError, ValueError):
        weekday = -1
    if not 0 <= weekday <= 6:
        errors["weekday"] = "invalid_weekday"

    try:
        monthday = int(values.get("monthday"))
    except (TypeError, ValueError):
        monthday = 0
    if not 1 <= monthday <= 31:
        errors["monthday"] = "invalid_monthday"

    try:
        history_limit = int(values.get("history_limit"))
    except (TypeError, ValueError):
        history_limit = -1
    if history_limit < 0:
        errors["history_limit"] = "invalid_history_limit"

    schedule = str(values.get("schedule"))
    if schedule not in _SCHEDULES:
        errors["schedule"] = "invalid_schedule"
    allocation = str(values.get("allocation"))
    if allocation not in _ALLOCATIONS:
        errors["allocation"] = "invalid_allocation"

    # 空白 optional 欄位會由前端省略；省略即清除舊係數，不保留舊估算。
    factor_input = user_input.get("carbon_factor")
    carbon_factor: float | None
    if factor_input in (None, ""):
        carbon_factor = None
    else:
        try:
            carbon_factor = float(factor_input)
        except (TypeError, ValueError):
            carbon_factor = None
            errors["carbon_factor"] = "invalid_carbon_factor"
        else:
            if not math.isfinite(carbon_factor) or carbon_factor < 0:
                errors["carbon_factor"] = "invalid_carbon_factor"

    source = str(values.get("carbon_factor_source", "")).strip()
    if carbon_factor is not None and not source:
        errors["carbon_factor_source"] = "carbon_factor_source_required"
    if len(source) > 200:
        errors["carbon_factor_source"] = "invalid_carbon_factor_source"
    if carbon_factor is None:
        source = ""

    ocr_url = str(values.get(CONF_OCR_URL, "")).strip().rstrip("/")
    if not _valid_ocr_url(ocr_url):
        errors[CONF_OCR_URL] = "invalid_ocr_url"

    return {
        "enabled": bool(values.get("enabled")),
        "schedule": schedule,
        "query_time": query_time,
        "weekday": weekday,
        "monthday": monthday,
        "history_limit": history_limit,
        "allocation": allocation,
        "carbon_factor": carbon_factor,
        "carbon_factor_source": source,
        CONF_OCR_URL: ocr_url,
    }, errors


class TaiWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """台水設定流程。"""

    VERSION = 1

    def __init__(self) -> None:
        self._pending_data: dict[str, Any] | None = None
        self._manual_challenge: client.ManualChallenge | None = None
        self._manual_url: str | None = None
        self._credential_step = "user"

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "TaiWaterOptionsFlow":
        return TaiWaterOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """設定新帳戶。"""
        self._credential_step = "user"
        return await self._async_credentials(user_input, DEFAULT_OPTIONS)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """重新驗證水號與戶名，且不允許悄悄切換到另一水號。"""
        self._credential_step = "reconfigure"
        entry = self._get_reconfigure_entry()
        defaults = {
            **entry.data,
            CONF_OCR_URL: entry.options.get(CONF_OCR_URL)
            or entry.data.get(CONF_OCR_URL, DEFAULT_OPTIONS[CONF_OCR_URL]),
            "history_limit": entry.options.get(
                "history_limit", DEFAULT_OPTIONS["history_limit"]
            ),
        }
        return await self._async_credentials(user_input, defaults)

    async def _async_credentials(
        self, user_input: dict[str, Any] | None, defaults: dict[str, Any]
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        values = dict(defaults)
        if user_input is not None:
            values, errors = _normalize_credentials(user_input)
            if not errors:
                account_id = _account_id(values[CONF_WATER_ID])
                await self.async_set_unique_id(account_id)
                if self._credential_step == "reconfigure":
                    self._abort_if_unique_id_mismatch(reason="wrong_account")
                else:
                    self._abort_if_unique_id_configured()

                resolved_ocr_url = await async_resolve_ocr_url(
                    self.hass, values[CONF_OCR_URL]
                )
                try:
                    result = await self.hass.async_add_executor_job(
                        client.validate_credentials,
                        values[CONF_WATER_ID],
                        values[CONF_CUSTOMER_NAME],
                        resolved_ocr_url,
                    )
                except client.QueryError as err:
                    code = safe_error_code(err)
                    if code in _MANUAL_ERROR_CODES:
                        self._pending_data = values
                        return await self._async_prepare_manual()
                    errors["base"] = _query_error(err)
                except (OSError, TimeoutError):
                    errors["base"] = "cannot_connect"
                else:
                    if (code := _result_error(result)) is None:
                        return self._finish_credentials(values, result)
                    if code in _MANUAL_ERROR_CODES:
                        self._pending_data = values
                        return await self._async_prepare_manual()
                    errors["base"] = code

        return self.async_show_form(
            step_id=self._credential_step,
            data_schema=_credential_schema(values),
            errors=errors,
            description_placeholders={"ocr_behavior": ""},
        )

    async def _async_prepare_manual(
        self, error: str | None = None
    ) -> ConfigFlowResult:
        self._clear_manual()
        try:
            challenge = await self.hass.async_add_executor_job(client.prepare_manual)
        except client.QueryError as err:
            return self._show_credentials_error(_query_error(err))
        except (OSError, TimeoutError):
            return self._show_credentials_error("cannot_connect")
        self._manual_challenge = challenge
        self._manual_url = register_manual_image(self.hass, challenge)
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {vol.Required("code"): selector.TextSelector()}
            ),
            errors={"base": error} if error else {},
            description_placeholders={"captcha_url": self._manual_url},
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """以同一個 in-memory session 提交人工辨識結果。"""
        if self._pending_data is None or self._manual_challenge is None:
            return self.async_abort(reason="manual_expired")
        if user_input is None:
            return self.async_show_form(
                step_id="manual",
                data_schema=vol.Schema(
                    {vol.Required("code"): selector.TextSelector()}
                ),
                description_placeholders={"captcha_url": self._manual_url or ""},
            )

        code = str(user_input.get("code", "")).strip()
        if not _CAPTCHA_CODE.fullmatch(code):
            return self.async_show_form(
                step_id="manual",
                data_schema=vol.Schema(
                    {vol.Required("code", default=code): selector.TextSelector()}
                ),
                errors={"code": "invalid_captcha"},
                description_placeholders={"captcha_url": self._manual_url or ""},
            )

        pending = self._pending_data
        challenge = self._manual_challenge
        try:
            result = await self.hass.async_add_executor_job(
                partial(
                    client.submit_manual,
                    challenge,
                    pending[CONF_WATER_ID],
                    pending[CONF_CUSTOMER_NAME],
                    code,
                    history_limit=None,
                )
            )
        except client.QueryError as err:
            error_code = safe_error_code(err)
            self._clear_manual()
            if error_code in _MANUAL_ERROR_CODES:
                return await self._async_prepare_manual(
                    "captcha_required"
                    if error_code in {"upstream_rejected", "captcha_expired"}
                    else _query_error(err)
                )
            return self._show_credentials_error(_query_error(err))
        except (OSError, TimeoutError):
            self._clear_manual()
            return self._show_credentials_error("cannot_connect")

        if (error_code := _result_error(result)) is not None:
            self._clear_manual()
            if error_code in _MANUAL_ERROR_CODES:
                return await self._async_prepare_manual(error_code)
            return self._show_credentials_error(error_code)
        self._clear_manual()
        return self._finish_credentials(pending, result)

    def _show_credentials_error(self, error: str) -> ConfigFlowResult:
        values = self._pending_data or {}
        return self.async_show_form(
            step_id=self._credential_step,
            data_schema=_credential_schema(values),
            errors={"base": error},
            description_placeholders={"ocr_behavior": ""},
        )

    def _clear_manual(self) -> None:
        if self._manual_challenge is not None:
            clear_manual_image(self.hass, self._manual_challenge)
        self._manual_challenge = None
        self._manual_url = None

    def _finish_credentials(
        self, values: dict[str, Any], result: dict[str, Any]
    ) -> ConfigFlowResult:
        account_id = _account_id(values[CONF_WATER_ID])
        self.hass.data.setdefault(DOMAIN, {}).setdefault("seeds", {})[
            account_id
        ] = result
        data = {
            CONF_WATER_ID: values[CONF_WATER_ID],
            CONF_CUSTOMER_NAME: values[CONF_CUSTOMER_NAME],
            CONF_NAME: values[CONF_NAME],
        }
        if self._credential_step == "reconfigure":
            entry = self._get_reconfigure_entry()
            return self.async_update_and_abort(
                entry,
                title=values[CONF_NAME],
                data=data,
                options={
                    **DEFAULT_OPTIONS,
                    **entry.options,
                    CONF_OCR_URL: values[CONF_OCR_URL],
                    "history_limit": values["history_limit"],
                },
            )
        return self.async_create_entry(
            title=values[CONF_NAME],
            data=data,
            options={
                **DEFAULT_OPTIONS,
                CONF_OCR_URL: values[CONF_OCR_URL],
                "history_limit": values["history_limit"],
            },
        )


class TaiWaterOptionsFlow(config_entries.OptionsFlow):
    """排程／計算設定，以及 setup 後的人工查詢。"""

    def __init__(self) -> None:
        self._manual_challenge: client.ManualChallenge | None = None
        self._manual_url: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["settings", "manual"]
        )

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = {**DEFAULT_OPTIONS, **self.config_entry.options}
        current[CONF_OCR_URL] = self.config_entry.options.get(
            CONF_OCR_URL
        ) or self.config_entry.data.get(CONF_OCR_URL, DEFAULT_OPTIONS[CONF_OCR_URL])
        errors: dict[str, str] = {}
        values = current
        if user_input is not None:
            values, errors = _normalize_options(user_input, current)
            if not errors:
                # main integration 的 update listener 統一負責 reload。
                return self.async_create_entry(title="", data=values)
        return self.async_show_form(
            step_id="settings",
            data_schema=_options_schema(values),
            errors=errors,
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """人工查詢並交由 coordinator 寫入正常資料管線。"""
        if self._manual_challenge is None:
            try:
                self._manual_challenge = await self.hass.async_add_executor_job(
                    client.prepare_manual
                )
            except client.QueryError as err:
                return self.async_show_form(
                    step_id="manual",
                    data_schema=vol.Schema({}),
                    errors={"base": _query_error(err)},
                    description_placeholders={"captcha_url": ""},
                )
            self._manual_url = register_manual_image(
                self.hass, self._manual_challenge
            )

        if user_input is not None:
            code = str(user_input.get("code", "")).strip()
            if not _CAPTCHA_CODE.fullmatch(code):
                return self._show_manual_form(
                    {"code": "invalid_captcha"}, code=code
                )
            challenge = self._manual_challenge
            try:
                await self.config_entry.runtime_data.async_submit_manual(
                    challenge, code
                )
            except Exception as err:
                error_code = safe_error_code(err)
                self._clear_manual()
                if error_code in _MANUAL_ERROR_CODES:
                    try:
                        self._manual_challenge = (
                            await self.hass.async_add_executor_job(
                                client.prepare_manual
                            )
                        )
                    except client.QueryError as prepare_error:
                        return self._show_manual_form(
                            {"base": _query_error(prepare_error)}
                        )
                    self._manual_url = register_manual_image(
                        self.hass, self._manual_challenge
                    )
                return self._show_manual_form(
                    {"base": _query_error(err)}
                )
            self._clear_manual()
            return self.async_abort(reason="manual_query_successful")

        return self._show_manual_form({})

    def _show_manual_form(
        self, errors: dict[str, str], *, code: str = ""
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {vol.Required("code", default=code): selector.TextSelector()}
            ),
            errors=errors,
            description_placeholders={"captcha_url": self._manual_url or ""},
        )

    def _clear_manual(self) -> None:
        if self._manual_challenge is not None:
            clear_manual_image(self.hass, self._manual_challenge)
        self._manual_challenge = None
        self._manual_url = None
