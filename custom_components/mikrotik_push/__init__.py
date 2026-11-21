"""MikroTik Push: RouterOS 7 integration driven by router-side streaming (`listen`)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .connection import is_pinnable, open_connection
from .const import (
    CONF_CERT_FINGERPRINT,
    CONF_TRACK_PORTS,
    CONF_TRACK_SYSTEM,
    CONF_TRACK_TRAFFIC,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)
from .entity_ids import feature_of
from .router_info import RouterInfo, read_router_info
from .routeros import AccessRefused, RouterOSConnection, RouterOSError
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
    def connect():  # reads entry.data each time, so a pin stored later is used by reconnects
        return open_connection(entry.data)

    try:
        connection = await connect()
    except AccessRefused as error:
        raise ConfigEntryAuthFailed(f"Access refused: {error}") from error
    except (RouterOSError, OSError, TimeoutError) as error:
        raise ConfigEntryNotReady(f"Cannot reach the router: {error}") from error

    _pin_certificate_on_first_use(hass, entry, connection)
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
            connect, on_access_refused=lambda: entry.async_start_reauth(hass), telemetry=telemetry
        )
        entry.runtime_data = MikrotikPushData(watcher, telemetry, info)
        device = _register_router_device(hass, entry, info)
        entry.async_on_unload(telemetry.add_listener(_keep_version_current(hass, device.id, telemetry)))
        _sync_optional_entities(hass, entry, features)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except (RouterOSError, TimeoutError) as error:
        await connection.close()
        raise ConfigEntryNotReady(f"The router did not answer properly: {error}") from error
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


def _pin_certificate_on_first_use(
    hass: HomeAssistant, entry: MikrotikPushConfigEntry, connection: RouterOSConnection
) -> None:
    """Entries created before pinning existed have no fingerprint: take it at their next start."""
    fingerprint = connection.peer_certificate_sha256()
    if fingerprint and is_pinnable(entry.data) and CONF_CERT_FINGERPRINT not in entry.data:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_CERT_FINGERPRINT: fingerprint}
        )


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


def _sync_optional_entities(
    hass: HomeAssistant, entry: MikrotikPushConfigEntry, features: Features
) -> None:
    """Disable the entities of unselected features and re-enable them when selected again.

    Disabling instead of deleting keeps what the user did to them: names, areas, dashboards.
    CONFIG_ENTRY (not INTEGRATION) is the disabler meant for this: Home Assistant does not
    schedule an extra reload of the entry when such an entity is enabled again.
    """
    registry = er.async_get(hass)
    for known in er.async_entries_for_config_entry(registry, entry.entry_id):
        feature = feature_of(entry.entry_id, known.unique_id)
        if feature is None:
            continue
        if not getattr(features, feature):
            if known.disabled_by is None:
                registry.async_update_entity(
                    known.entity_id, disabled_by=er.RegistryEntryDisabler.CONFIG_ENTRY
                )
                # The reload that brought us here left an "unavailable, restored" state behind.
                hass.states.async_remove(known.entity_id)
        elif known.disabled_by is er.RegistryEntryDisabler.CONFIG_ENTRY:
            registry.async_update_entity(known.entity_id, disabled_by=None)
