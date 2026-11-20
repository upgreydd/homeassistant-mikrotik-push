"""Link state of the router's ethernet ports."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MikrotikPushConfigEntry
from .entity import PortEntity, add_as_they_appear
from .telemetry import Channel


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MikrotikPushConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    telemetry = entry.runtime_data.telemetry
    if telemetry.features.ports:
        add_as_they_appear(
            entry,
            async_add_entities,
            Channel.PORTS,
            lambda: telemetry.ports,
            lambda name: [PortLink(entry, name)],
        )


class PortLink(PortEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: MikrotikPushConfigEntry, key: str) -> None:
        super().__init__(entry, Channel.PORTS, key, "link", "link")

    def _has_reading(self) -> bool:
        return self._key in self._data.telemetry.ports

    @property
    def is_on(self) -> bool:
        return self._data.telemetry.ports[self._key].running
