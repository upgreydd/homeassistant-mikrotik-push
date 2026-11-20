"""MikroTik Push: RouterOS 7 integration driven by router-side streaming (`listen`)."""

from __future__ import annotations

from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .connection import open_connection
from .const import DOMAIN
from .routeros import LoginRejected, RouterOSError
from .watcher import RouterWatcher

type MikrotikPushConfigEntry = ConfigEntry[RouterWatcher]

PLATFORMS = [Platform.DEVICE_TRACKER]


async def async_setup_entry(hass: HomeAssistant, entry: MikrotikPushConfigEntry) -> bool:
    connect = partial(open_connection, entry.data)
    try:
        connection = await connect()
    except LoginRejected as error:
        raise ConfigEntryAuthFailed(f"Login rejected: {error}") from error
    except (RouterOSError, OSError, TimeoutError) as error:
        raise ConfigEntryNotReady(f"Cannot reach the router: {error}") from error

    watcher = RouterWatcher(connect, on_login_rejected=lambda: entry.async_start_reauth(hass))
    entry.runtime_data = watcher
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await connection.close()
        raise
    entry.async_create_background_task(
        hass, watcher.run(connection), f"{DOMAIN}_{entry.entry_id}_watcher"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MikrotikPushConfigEntry) -> bool:
    entry.runtime_data.stop()  # first, so no event reaches a platform that is being torn down
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
