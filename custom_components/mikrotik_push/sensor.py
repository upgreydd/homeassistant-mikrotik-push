"""Port speed and traffic, router load, memory, boot time, temperatures, voltages and the uplink.

None of these sensors has a `state_class`: Home Assistant would then keep long-term statistics of
values that change every few seconds. History graphs work without one; statistics can be switched on
per entity with `customize` in configuration.yaml.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import (
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfElectricPotential,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MikrotikPushConfigEntry
from .entity import MikrotikEntity, PortEntity, add_as_they_appear
from .entity_ids import system_unique_id, uplink_unique_id
from .telemetry import Channel

_HEALTH_CLASSES = {
    "C": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    "V": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MikrotikPushConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    telemetry = entry.runtime_data.telemetry
    features = telemetry.features
    if features.ports:
        add_as_they_appear(
            entry, async_add_entities, Channel.PORTS,
            lambda: telemetry.ports, lambda name: [PortSpeed(entry, name)],
        )
    if features.traffic:
        add_as_they_appear(
            entry, async_add_entities, Channel.TRAFFIC,
            lambda: telemetry.traffic,
            lambda name: [PortTraffic(entry, name, "rx"), PortTraffic(entry, name, "tx")],
        )
    if features.uplink:
        async_add_entities([ActiveUplink(entry)])
    if features.system:
        async_add_entities([CpuLoad(entry), MemoryUsage(entry), LastBoot(entry)])
        add_as_they_appear(
            entry, async_add_entities, Channel.SYSTEM,
            lambda: telemetry.health, lambda name: [HealthReading(entry, name)],
        )


class PortSpeed(PortEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND

    def __init__(self, entry: MikrotikPushConfigEntry, key: str) -> None:
        super().__init__(entry, Channel.PORTS, key, "speed", "speed")

    def _has_reading(self) -> bool:
        return self._key in self._data.telemetry.ports

    @property
    def native_value(self) -> int | None:
        return self._data.telemetry.ports[self._key].rate_mbps  # None while the link is down


class PortTraffic(PortEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_native_unit_of_measurement = UnitOfDataRate.BITS_PER_SECOND
    _attr_suggested_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_suggested_display_precision = 2

    def __init__(self, entry: MikrotikPushConfigEntry, key: str, direction: str) -> None:
        super().__init__(entry, Channel.TRAFFIC, key, direction, direction.upper())
        self._direction = direction

    def _has_reading(self) -> bool:
        return self._key in self._data.telemetry.traffic

    @property
    def native_value(self) -> int:
        traffic = self._data.telemetry.traffic[self._key]
        return traffic.rx_bps if self._direction == "rx" else traffic.tx_bps


class _SystemSensor(MikrotikEntity, SensorEntity):
    def __init__(self, entry: MikrotikPushConfigEntry, kind: str, name: str) -> None:
        super().__init__(entry, Channel.SYSTEM, system_unique_id(entry.entry_id, kind))
        self._attr_name = name


class CpuLoad(_SystemSensor):
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, entry: MikrotikPushConfigEntry) -> None:
        super().__init__(entry, "cpu", "CPU load")

    def _has_reading(self) -> bool:
        return self._data.telemetry.load is not None

    @property
    def native_value(self) -> int:
        return self._data.telemetry.load.cpu_percent


class MemoryUsage(_SystemSensor):
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 1

    def __init__(self, entry: MikrotikPushConfigEntry) -> None:
        super().__init__(entry, "memory", "Memory usage")

    def _has_reading(self) -> bool:
        return self._data.telemetry.load is not None and self._data.telemetry.total_memory > 0

    @property
    def native_value(self) -> float:
        total = self._data.telemetry.total_memory
        return round(100 * (total - self._data.telemetry.load.free_memory) / total, 1)


class LastBoot(_SystemSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: MikrotikPushConfigEntry) -> None:
        super().__init__(entry, "boot", "Last boot")

    def _has_reading(self) -> bool:
        return self._data.telemetry.boot_time is not None

    @property
    def native_value(self) -> datetime:
        return self._data.telemetry.boot_time


class HealthReading(_SystemSensor):
    def __init__(self, entry: MikrotikPushConfigEntry, reading: str) -> None:
        super().__init__(entry, f"health_{reading}", reading.replace("-", " ").capitalize())
        self._reading = reading
        kind = entry.runtime_data.telemetry.health[reading].kind
        self._attr_device_class, self._attr_native_unit_of_measurement = _HEALTH_CLASSES[kind]

    def _has_reading(self) -> bool:
        return self._reading in self._data.telemetry.health

    @property
    def native_value(self) -> float:
        return self._data.telemetry.health[self._reading].value


class ActiveUplink(MikrotikEntity, SensorEntity):
    """The interface the active default route leaves through: the WAN in use, whatever kind it is."""

    _attr_name = "Active uplink"

    def __init__(self, entry: MikrotikPushConfigEntry) -> None:
        super().__init__(entry, Channel.UPLINK, uplink_unique_id(entry.entry_id))

    def _has_reading(self) -> bool:
        return self._data.telemetry.routes_known

    @property
    def native_value(self) -> str | None:
        uplink = self._data.telemetry.uplink
        return uplink.interface if uplink else None  # unknown: no active default route

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """`routes` lists every default route in order, so a template can pick any of them."""
        telemetry = self._data.telemetry
        active = telemetry.uplink.as_attributes() if telemetry.uplink else {}
        return {**active, "routes": [route.as_attributes() for route in telemetry.routes]}
