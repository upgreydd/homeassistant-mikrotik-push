"""Base for the router-level entities (ports, traffic, system, update)."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MikrotikPushConfigEntry
from .const import DOMAIN
from .entity_ids import port_unique_id
from .telemetry import Channel


class MikrotikEntity(Entity):
    """Writes its state when its channel announces new readings or the link state flips."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: MikrotikPushConfigEntry, channel: Channel, unique_id: str) -> None:
        self._data = entry.runtime_data
        self._channel = channel
        self._attr_unique_id = unique_id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    def _has_reading(self) -> bool:
        return True

    @property
    def available(self) -> bool:
        return self._data.watcher.connected and self._has_reading()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._data.telemetry.add_listener(self._on_channel))
        self.async_on_remove(self._data.watcher.add_listener(self._on_link))

    @callback
    def _on_channel(self, channel: Channel) -> None:
        if channel is self._channel:
            self.async_write_ha_state()

    @callback
    def _on_link(self, change) -> None:
        if change.link:
            self.async_write_ha_state()


class PortEntity(MikrotikEntity):
    """An entity of one port, identified by its factory name and labelled with its current one."""

    def __init__(
        self, entry: MikrotikPushConfigEntry, channel: Channel, key: str, kind: str, label: str
    ) -> None:
        super().__init__(entry, channel, port_unique_id(entry.entry_id, key, kind))
        self._key = key
        self._label = label

    @property
    def name(self) -> str:
        port = self._data.telemetry.ports.get(self._key)
        return f"{port.name if port else self._key} {self._label}"


def add_as_they_appear(
    entry: MikrotikPushConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    channel: Channel,
    names: Callable[[], Iterable[str]],
    create: Callable[[str], Iterable[Entity]],
) -> None:
    """Create entities for every name known now and for each one a later announcement brings."""
    telemetry = entry.runtime_data.telemetry
    known: set[str] = set()

    @callback
    def add_new(announced: Channel) -> None:
        if announced is not channel:
            return
        new = [name for name in names() if name not in known]
        known.update(new)
        async_add_entities(entity for name in new for entity in create(name))

    add_new(channel)
    entry.async_on_unload(telemetry.add_listener(add_new))
