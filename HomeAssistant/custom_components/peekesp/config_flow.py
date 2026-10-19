"""Config flow: pair with the code on the device, or enter a URL and token."""
from __future__ import annotations

import hashlib
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PeekAuthError, PeekConnectionError, PeekRelayClient
from .const import (
    CONF_CODE,
    CONF_MODE,
    CONF_OFFLINE_AFTER,
    CONF_RELAY_BASE,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_OFFLINE_AFTER,
    DEFAULT_RELAY_BASE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MIN_SCAN_INTERVAL,
    MODE_MANUAL,
    MODE_PAIRED,
)
from .pairing import derive, is_valid_code, normalize_code

PAIR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CODE): str,
        vol.Required(CONF_RELAY_BASE, default=DEFAULT_RELAY_BASE): str,
    }
)
MANUAL_SCHEMA = vol.Schema({vol.Required(CONF_URL): str, vol.Required(CONF_TOKEN): str})


class PeekConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["pair", "manual"])

    # --- paired: the code shown on the device's screen -----------------------
    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = normalize_code(user_input[CONF_CODE])
            base = user_input[CONF_RELAY_BASE].strip().rstrip("/")
            if not is_valid_code(code):
                errors[CONF_CODE] = "invalid_code"
            elif not base.startswith("https://"):
                errors[CONF_RELAY_BASE] = "invalid_url"
            else:
                stream, token = derive(code)
                url = f"{base}/telemetry/{stream}"
                errors = await self._probe(url, token)
                if not errors:
                    await self.async_set_unique_id(stream)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"PeekESP {stream[:6]}",
                        data={CONF_MODE: MODE_PAIRED, CONF_URL: url, CONF_TOKEN: token},
                    )
        return self.async_show_form(
            step_id="pair",
            data_schema=self.add_suggested_values_to_schema(PAIR_SCHEMA, user_input),
            errors=errors,
        )

    # --- manual: a named or private stream -----------------------------------
    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = user_input[CONF_URL].strip()
            token = user_input[CONF_TOKEN].strip()
            if not url.startswith("https://"):
                errors[CONF_URL] = "invalid_url"
            else:
                errors = await self._probe(url, token)
                if not errors:
                    await self.async_set_unique_id(hashlib.sha256(url.encode()).hexdigest()[:16])
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="PeekESP (custom relay)",
                        data={CONF_MODE: MODE_MANUAL, CONF_URL: url, CONF_TOKEN: token},
                    )
        return self.async_show_form(
            step_id="manual",
            data_schema=self.add_suggested_values_to_schema(MANUAL_SCHEMA, user_input),
            errors=errors,
        )

    # --- reauth: the relay stopped accepting our token ------------------------
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        paired = entry.data.get(CONF_MODE) == MODE_PAIRED
        if user_input is not None:
            if paired:
                code = normalize_code(user_input[CONF_CODE])
                if not is_valid_code(code):
                    errors[CONF_CODE] = "invalid_code"
                else:
                    stream, token = derive(code)
                    base = entry.data[CONF_URL].split("/telemetry/")[0]
                    url = f"{base}/telemetry/{stream}"
            else:
                url, token = entry.data[CONF_URL], user_input[CONF_TOKEN].strip()
            if not errors:
                errors = await self._probe(url, token)
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_URL: url, CONF_TOKEN: token}
                )
        schema = vol.Schema({vol.Required(CONF_CODE if paired else CONF_TOKEN): str})
        return self.async_show_form(step_id="reauth_confirm", data_schema=schema, errors=errors)

    async def _probe(self, url: str, token: str) -> dict[str, str]:
        client = PeekRelayClient(async_get_clientsession(self.hass), url, token)
        try:
            await client.fetch()
        except PeekAuthError:
            return {"base": "invalid_auth"}
        except PeekConnectionError:
            return {"base": "cannot_connect"}
        return {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return PeekOptionsFlow()


class PeekOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        o = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SCAN_INTERVAL, default=o.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
                ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=600)),
                vol.Required(
                    CONF_OFFLINE_AFTER, default=o.get(CONF_OFFLINE_AFTER, DEFAULT_OFFLINE_AFTER)
                ): vol.All(vol.Coerce(int), vol.Range(min=10, max=3600)),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
