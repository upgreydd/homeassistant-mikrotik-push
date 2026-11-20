"""RouterOS version: installed and latest known to the router. Read-only, no install."""

from __future__ import annotations

from homeassistant.components.update import UpdateEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MikrotikPushConfigEntry
from .entity import MikrotikEntity
from .entity_ids import system_unique_id
from .telemetry import Channel


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MikrotikPushConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    if entry.runtime_data.telemetry.features.system:
        async_add_entities([RouterOsUpdateEntity(entry)])


class RouterOsUpdateEntity(MikrotikEntity, UpdateEntity):
    _attr_name = "RouterOS"
    _attr_title = "RouterOS"
    _attr_release_url = "https://mikrotik.com/download/changelogs"

    def __init__(self, entry: MikrotikPushConfigEntry) -> None:
        super().__init__(entry, Channel.UPDATE, system_unique_id(entry.entry_id, "update"))

    def _has_reading(self) -> bool:
        return self._data.telemetry.update is not None

    @property
    def installed_version(self) -> str | None:
        update = self._data.telemetry.update
        return update.installed if update else None

    @property
    def latest_version(self) -> str | None:
        """Unknown (not "up to date") until the router has checked for updates."""
        update = self._data.telemetry.update
        return update.latest if update else None

    @property
    def release_summary(self) -> str | None:
        update = self._data.telemetry.update
        return update.status if update and update.status else None
