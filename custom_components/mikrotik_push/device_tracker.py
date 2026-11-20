"""Device tracker: one entity per DHCP-leased host the router knows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.components.device_tracker import DOMAIN as DEVICE_TRACKER_DOMAIN
from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MikrotikPushConfigEntry
from .registry import Host
from .watcher import Change, RouterWatcher


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MikrotikPushConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    watcher = entry.runtime_data.watcher
    tracked: set[str] = set()

    @callback
    def track(macs_and_names: Mapping[str, str]) -> None:
        new = {mac: name for mac, name in macs_and_names.items() if mac not in tracked}
        tracked.update(new)
        async_add_entities(
            HostTracker(entry.entry_id, watcher, mac, name) for mac, name in new.items()
        )

    @callback
    def track_new_hosts(change: Change) -> None:
        # After a (re)connect, look at every host: an earlier half-finished snapshot may have
        # put a host into the registry without anybody hearing about it.
        macs = watcher.registry.hosts.keys() if change.link else change.macs
        track({mac: watcher.registry.hosts[mac].name for mac in macs if mac in watcher.registry.hosts})

    # Remember hosts from earlier runs: after a restart an absent host (expired lease) must show
    # as "not home", not as an unavailable leftover.
    prefix = f"{entry.entry_id}_"
    registry = er.async_get(hass)
    track(
        {
            (mac := known.unique_id.removeprefix(prefix)): known.original_name or mac
            for known in er.async_entries_for_config_entry(registry, entry.entry_id)
            if known.domain == DEVICE_TRACKER_DOMAIN
        }
    )
    entry.async_on_unload(watcher.add_listener(track_new_hosts))


class HostTracker(ScannerEntity):
    _attr_should_poll = False

    def __init__(self, entry_id: str, watcher: RouterWatcher, mac: str, fallback_name: str) -> None:
        self._entry_id = entry_id
        self._watcher = watcher
        self._mac = mac
        self._fallback_name = fallback_name

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._watcher.add_listener(self._handle_change))

    @callback
    def _handle_change(self, change: Change) -> None:
        if change.link or self._mac in change.macs:
            self.async_write_ha_state()

    @property
    def unique_id(self) -> str:
        """Scoped per config entry, so two routers that see the same MAC do not collide."""
        return f"{self._entry_id}_{self._mac}"

    @property
    def _host(self) -> Host | None:
        return self._watcher.registry.hosts.get(self._mac)

    @property
    def name(self) -> str:
        return self._host.name if self._host else self._fallback_name

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Enable hosts the admin named or reserved; leave other leases to Home Assistant's rule."""
        host = self._host
        return (host is not None and host.administered) or super().entity_registry_enabled_default

    @property
    def available(self) -> bool:
        return self._watcher.connected

    @property
    def is_connected(self) -> bool:
        return self._host is not None and self._host.connected

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def ip_address(self) -> str | None:
        return self._host.ip_address if self._host else None

    @property
    def hostname(self) -> str | None:
        return self._host.hostname if self._host else None

    @property
    def extra_state_attributes(self) -> Mapping[str, Any]:
        return {"port": self._host.port if self._host else None}
