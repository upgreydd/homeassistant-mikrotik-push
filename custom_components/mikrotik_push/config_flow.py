"""Config flow: ask for the router, prove the login works, store the entry."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)

from .const import DEFAULT_PORT, DOMAIN
from .routeros import ConnectionLost, RouterOSConnection, RouterOSError


class MikrotikPushConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._async_abort_entries_match({CONF_HOST: user_input[CONF_HOST]})
            try:
                conn = await RouterOSConnection.open(
                    user_input[CONF_HOST],
                    user_input[CONF_PORT],
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    use_ssl=user_input[CONF_SSL],
                    verify_ssl=user_input[CONF_VERIFY_SSL],
                )
                try:
                    title = (await conn.call("/system/identity/print"))[0]["name"]
                finally:
                    await conn.close()
            except (ConnectionLost, OSError, TimeoutError):
                errors["base"] = "cannot_connect"
            except RouterOSError:
                errors["base"] = "invalid_auth"
            else:
                return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                    vol.Required(CONF_USERNAME, default="ha"): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Required(CONF_SSL, default=True): bool,
                    vol.Required(CONF_VERIFY_SSL, default=False): bool,
                }
            ),
        )
