"""MikroTik Push: RouterOS 7 integration driven by router-side streaming (`listen`)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .connection import open_connection
from .const import (
    CONF_TRACK_PORTS,
    CONF_TRACK_SYSTEM,
    CONF_TRACK_TRAFFIC,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .entity_ids import feature_of
from .router_info import RouterInfo, read_router_info
from .routeros import LoginRejected, RouterOSError
from .telemetry import Channel, Features, RouterTelemetry
from .watcher import RouterWatcher


@dataclass(frozen=True, slots=True)
class MikrotikPushData:
    watcher: RouterWatcher
    telemetry: RouterTelemetry
    info: RouterInfo


type MikrotikPushConfigEntry = ConfigEntry[MikrotikPushData]

PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER, Platform.SENSOR, Platform.UPDATE]


async def async_setup_entry(hass: HomeAssistant, entry: MikrotikPushConfigEntry) -> bool:
    connect = partial(open_connection, entry.data)
    try:
        connection = await connect()
    except LoginRejected as error:
        raise ConfigEntryAuthFailed(f"Login rejected: {error}") from error
    except (RouterOSError, OSError, TimeoutError) as error:
        raise ConfigEntryNotReady(f"Cannot reach the router: {error}") from error

    try:
        info = await read_router_info(connection)
        features = Features(
            ports=entry.options.get(CONF_TRACK_PORTS, False),
            traffic=entry.options.get(CONF_TRACK_TRAFFIC, False),
            system=entry.options.get(CONF_TRACK_SYSTEM, False),
        )
        telemetry = RouterTelemetry(
            features,
            publish_interval=int(entry.options.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
        )
        watcher = RouterWatcher(
            connect, on_login_rejected=lambda: entry.async_start_reauth(hass), telemetry=telemetry
        )
        entry.runtime_data = MikrotikPushData(watcher, telemetry, info)
        device = _register_router_device(hass, entry, info)
        entry.async_on_unload(telemetry.add_listener(_keep_version_current(hass, device.id, telemetry)))
        _remove_entities_of_unselected_features(hass, entry, features)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except RouterOSError as error:
        await connection.close()
        raise ConfigEntryNotReady(f"The router answered unexpectedly: {error}") from error
    except BaseException:
        await connection.close()
        raise
    entry.async_create_background_task(
        hass, watcher.run(connection), f"{DOMAIN}_{entry.entry_id}_watcher"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MikrotikPushConfigEntry) -> bool:
    entry.runtime_data.watcher.stop()  # first, so no event reaches a platform being torn down
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


def _register_router_device(
    hass: HomeAssistant, entry: MikrotikPushConfigEntry, info: RouterInfo
) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="MikroTik",
        model=info.model,
        name=info.name,
        serial_number=info.serial_number,
        sw_version=info.version,
    )


def _keep_version_current(
    hass: HomeAssistant, device_id: str, telemetry: RouterTelemetry
) -> Callable[[Channel], None]:
    """A RouterOS upgrade reboots the router, not Home Assistant: refresh the device's version."""

    @callback
    def update_device(channel: Channel) -> None:
        if channel is Channel.UPDATE and telemetry.update:
            dr.async_get(hass).async_update_device(device_id, sw_version=telemetry.update.installed)

    return update_device


def _remove_entities_of_unselected_features(
    hass: HomeAssistant, entry: MikrotikPushConfigEntry, features: Features
) -> None:
    """Switching a feature off in the options removes its entities instead of orphaning them."""
    registry = er.async_get(hass)
    for known in er.async_entries_for_config_entry(registry, entry.entry_id):
        feature = feature_of(entry.entry_id, known.unique_id)
        if feature is not None and not getattr(features, feature):
            registry.async_remove(known.entity_id)
