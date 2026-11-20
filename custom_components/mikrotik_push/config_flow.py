"""Config flow: pick the router, show a ready-to-paste setup script, verify the login."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .connection import open_connection
from .const import DEFAULT_PORT, DOMAIN, IDENTITY_PATH
from .router_script import (
    DEFAULT_USERNAME,
    build_setup_script,
    generate_password,
    is_plain,
    is_safe_host,
)
from .routeros import ConnectionLost, LoginRejected, RouterOSError
from .watcher import TABLE_PATHS

_LOGGER = logging.getLogger(__name__)

type Probe = tuple[str, str]  # (router identity, stable router id)

_PASSWORD_FIELD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
# The script lives in a form field, not in the description: the frontend redraws the description,
# which drops the text selection, while a text area keeps it so the script can be copied.
_SCRIPT_KEY = "setup_script"
_SCRIPT_FIELD = TextSelector(TextSelectorConfig(multiline=True))


class MikrotikPushConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._host = ""
        self._password = generate_password()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            if is_safe_host(host):
                self._host = host
                return await self.async_step_credentials()
            errors[CONF_HOST] = "invalid_host"
        return self.async_show_form(
            step_id="user", data_schema=vol.Schema({vol.Required(CONF_HOST): str}), errors=errors
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {CONF_HOST: self._host, **user_input}
            data.pop(_SCRIPT_KEY, None)
            probe, error = await _probe_router(data)
            if probe is not None:
                identity, router_id = probe
                await self.async_set_unique_id(router_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=identity, data=data)
            errors["base"] = error

        script, ha_address = await self._setup_instructions(DEFAULT_USERNAME)
        return self.async_show_form(
            step_id="credentials",
            errors=errors,
            description_placeholders={"ha_address": ha_address},
            data_schema=vol.Schema(
                {
                    vol.Optional(_SCRIPT_KEY, default=script): _SCRIPT_FIELD,
                    vol.Required(CONF_USERNAME, default=DEFAULT_USERNAME): str,
                    vol.Required(CONF_PASSWORD, default=self._password): _PASSWORD_FIELD,
                    vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
                    vol.Required(CONF_SSL, default=True): bool,
                    vol.Required(CONF_VERIFY_SSL, default=False): bool,
                }
            ),
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        self._host = entry.data[CONF_HOST]
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            probe, error = await _probe_router(data)
            if probe is not None:
                await self.async_set_unique_id(probe[1])
                self._abort_if_unique_id_mismatch()
                return self.async_update_reload_and_abort(entry, data=data)
            errors["base"] = error
        script, ha_address = await self._setup_instructions(entry.data[CONF_USERNAME])
        return self.async_show_form(
            step_id="reauth_confirm",
            errors=errors,
            description_placeholders={"ha_address": ha_address},
            data_schema=vol.Schema(
                {
                    vol.Optional(_SCRIPT_KEY, default=script): _SCRIPT_FIELD,
                    vol.Required(CONF_PASSWORD, default=self._password): _PASSWORD_FIELD,
                }
            ),
        )

    async def _setup_instructions(self, username: str) -> tuple[str, str]:
        """The script to paste on the router and the Home Assistant address it binds to."""
        try:
            ha_address = await async_get_source_ip(self.hass, target_ip=self._host)
        except HomeAssistantError:
            ha_address = None
        if not is_plain(username):
            return (
                "(no script for this user name: set the password on the router)",
                ha_address or "unknown",
            )
        script = build_setup_script(
            host=self._host, ha_address=ha_address, password=self._password, username=username
        )
        return script, ha_address or "unknown"


async def _probe_router(data: Mapping[str, Any]) -> tuple[Probe | None, str]:
    """Log in, read the identity and the tables the watcher needs; return (result, error key)."""
    try:
        connection = await open_connection(data)
        try:
            identity = (await connection.call(IDENTITY_PATH))[0]["name"]
            for path in TABLE_PATHS.values():
                await connection.call(f"{path}/print")
            board = await connection.call("/system/routerboard/print")
        finally:
            await connection.close()
    except LoginRejected:
        return None, "invalid_auth"
    except (ConnectionLost, OSError, TimeoutError):
        return None, "cannot_connect"
    except RouterOSError:
        return None, "router_error"
    except Exception:
        _LOGGER.exception("Unexpected error while probing the router")
        return None, "unknown"
    serial = board[0].get("serial-number") if board else None
    return (identity, serial or data[CONF_HOST]), ""
