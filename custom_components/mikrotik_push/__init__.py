"""MikroTik Push: RouterOS 7 integration driven by router-side streaming (`listen`)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady

from .routeros import ConnectionLost, RouterOSConnection, RouterOSError

type MikrotikPushConfigEntry = ConfigEntry[RouterOSConnection]

# Platforms (device_tracker, binary_sensor, sensor, update) are added in the next milestone.
PLATFORMS: list = []


async def async_setup_entry(hass: HomeAssistant, entry: MikrotikPushConfigEntry) -> bool:
    d = entry.data
    try:
        conn = await RouterOSConnection.open(
            d[CONF_HOST],
            d[CONF_PORT],
            d[CONF_USERNAME],
            d[CONF_PASSWORD],
            use_ssl=d[CONF_SSL],
            verify_ssl=d[CONF_VERIFY_SSL],
        )
    except (ConnectionLost, OSError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"Cannot reach {d[CONF_HOST]}: {err}") from err
    except RouterOSError as err:
        # No reauth flow yet: the user fixes credentials by removing and re-adding the entry.
        raise ConfigEntryError(f"Login rejected: {err}") from err
    entry.runtime_data = conn
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MikrotikPushConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.close()
    return ok
