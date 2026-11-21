"""Config flow: pick the router, show a ready-to-paste setup script, verify the login."""

from __future__ import annotations

import logging
import socket
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.components.network import async_get_source_ip
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
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
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .connection import is_pinnable, open_connection
from .const import (
    CONF_CERT_FINGERPRINT,
    CONF_TRACK_PORTS,
    CONF_TRACK_SYSTEM,
    CONF_TRACK_TRAFFIC,
    CONF_TRACK_UPLINK,
    CONF_UPDATE_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)
from .router_script import (
    DEFAULT_USERNAME,
    build_setup_script,
    generate_password,
    is_ip,
    is_plain,
    is_safe_host,
)
from .router_info import read_router_info
from .routeros import CertificateChanged, ConnectionLost, LoginRejected, RouterOSError
from .watcher import TABLE_PATHS

_LOGGER = logging.getLogger(__name__)

type Probe = tuple[str, str, str | None]  # (identity, stable router id, certificate fingerprint)

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
        self._shown_certificate: str | None = None  # a changed certificate the user was shown

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return MikrotikPushOptionsFlow()

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
                identity, router_id, fingerprint = probe
                await self.async_set_unique_id(router_id)
                self._abort_if_unique_id_configured()
                if fingerprint:
                    data[CONF_CERT_FINGERPRINT] = fingerprint
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
            probe, error = await self._probe_confirming_certificate(data)
            if probe is not None:
                return await self._update_same_router(entry, data, probe)
            errors["base"] = error
        script, ha_address = await self._setup_instructions(entry.data[CONF_USERNAME])
        return self.async_show_form(
            step_id="reauth_confirm",
            errors=errors,
            description_placeholders={
                "ha_address": ha_address,
                "fingerprint": self._shown_certificate or "",
            },
            data_schema=vol.Schema(
                {
                    vol.Optional(_SCRIPT_KEY, default=script): _SCRIPT_FIELD,
                    vol.Required(CONF_PASSWORD, default=self._password): _PASSWORD_FIELD,
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Move the entry to a new address or port without losing its entities."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            if is_safe_host(user_input[CONF_HOST].strip()):
                data = {**entry.data, **user_input, CONF_HOST: user_input[CONF_HOST].strip()}
                probe, error = await self._probe_confirming_certificate(data)
                if probe is not None:
                    return await self._update_same_router(entry, data, probe)
                errors["base"] = error
            else:
                errors[CONF_HOST] = "invalid_host"
        current = user_input or entry.data
        return self.async_show_form(
            step_id="reconfigure",
            errors=errors,
            description_placeholders={"fingerprint": self._shown_certificate or ""},
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST, default=current[CONF_HOST]): str,
                    vol.Required(CONF_PORT, default=current[CONF_PORT]): cv.port,
                }
            ),
        )

    async def _probe_confirming_certificate(
        self, data: dict[str, Any]
    ) -> tuple[Probe | None, str]:
        """Probe with the stored pin; a changed certificate is shown first, trusted on a repeat."""
        if self._shown_certificate:
            # The user saw this fingerprint and submitted again: trust exactly that one.
            data[CONF_CERT_FINGERPRINT] = self._shown_certificate
        try:
            return await _probe_router(data)
        except CertificateChanged as changed:
            # Refused before the login, so the password has not left Home Assistant.
            self._shown_certificate = changed.presented_sha256
            return None, "certificate_changed"

    async def _update_same_router(
        self, entry: ConfigEntry, data: dict[str, Any], probe: Probe
    ) -> ConfigFlowResult:
        _, router_id, fingerprint = probe
        await self.async_set_unique_id(router_id)
        if entry.unique_id != entry.data[CONF_HOST]:
            self._abort_if_unique_id_mismatch()  # a serial number tells the router apart
        # else: a router without a serial number is known by its address, which may be what changes
        if fingerprint:
            data[CONF_CERT_FINGERPRINT] = fingerprint
        return self.async_update_reload_and_abort(entry, data=data, unique_id=router_id)

    async def _router_ip(self) -> str:
        """The helper that finds our source address resolves names on the event loop: do it here."""
        if is_ip(self._host):
            return self._host
        addresses = await self.hass.loop.getaddrinfo(self._host, None, family=socket.AF_INET)
        return addresses[0][4][0]

    async def _setup_instructions(self, username: str) -> tuple[str, str]:
        """The script to paste on the router and the Home Assistant address it binds to."""
        try:
            ha_address = await async_get_source_ip(self.hass, target_ip=await self._router_ip())
        except (HomeAssistantError, OSError):
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
            for path in TABLE_PATHS.values():
                await connection.call(f"{path}/print")
            info = await read_router_info(connection)
            fingerprint = connection.peer_certificate_sha256()
        finally:
            await connection.close()
    except LoginRejected:
        return None, "invalid_auth"
    except (ConnectionLost, OSError, TimeoutError):
        return None, "cannot_connect"
    except CertificateChanged:
        raise  # only possible with a stored fingerprint: the caller decides what to ask
    except RouterOSError:
        return None, "router_error"
    except Exception:
        _LOGGER.exception("Unexpected error while probing the router")
        return None, "unknown"
    pinned = fingerprint if is_pinnable(data) else None
    return (info.name, info.serial_number or data[CONF_HOST], pinned), ""


class MikrotikPushOptionsFlow(OptionsFlowWithReload):
    """Choose the optional readings; everything is off until selected."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TRACK_PORTS, default=options.get(CONF_TRACK_PORTS, False)): bool,
                    vol.Required(
                        CONF_TRACK_TRAFFIC, default=options.get(CONF_TRACK_TRAFFIC, False)
                    ): bool,
                    vol.Required(CONF_TRACK_SYSTEM, default=options.get(CONF_TRACK_SYSTEM, False)): bool,
                    vol.Required(CONF_TRACK_UPLINK, default=options.get(CONF_TRACK_UPLINK, False)): bool,
                    vol.Required(
                        CONF_UPDATE_INTERVAL,
                        default=options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_UPDATE_INTERVAL,
                            max=MAX_UPDATE_INTERVAL,
                            step=1,
                            unit_of_measurement="s",
                            mode=NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
        )
