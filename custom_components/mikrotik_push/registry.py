"""Merge router tables (DHCP leases, bridge hosts, wifi registrations) into one view of the known hosts by MAC.

A host is *known* when it holds a DHCP lease and *connected* while its MAC is learned on the
bridge or registered on a wifi interface (wifi package, also as CAPsMAN manager). A host that just
left both stays connected for a grace period, because a flapping port or a rebooting access point
flushes the whole bridge table at once. The wifi registration keeps a sleeping phone connected after
its bridge entry aged out.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

type Row = dict[str, str]


class Table(StrEnum):
    LEASE = "lease"
    BRIDGE_HOST = "bridge_host"
    WIFI_REGISTRATION = "wifi_registration"


@dataclass(frozen=True, slots=True)
class Host:
    mac: str
    name: str
    hostname: str | None
    ip_address: str | None
    port: str | None
    connected: bool
    administered: bool  # named or reserved by the router's admin, so surely a known device


class HostRegistry:
    """Holds raw rows per table and derives hosts; reports which MACs changed."""

    def __init__(
        self, departure_grace: float = 0.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.hosts: dict[str, Host] = {}
        self._departure_grace = departure_grace
        self._clock = clock
        self._rows: dict[Table, dict[str, Row]] = {table: {} for table in Table}
        self._on_bridge: set[str] = set()
        self._departed_at: dict[str, float] = {}
        self._grace_ends_at: float | None = None

    def replace(self, table: Table, rows: Iterable[Row]) -> set[str]:
        """Swap in a full snapshot of `table`."""
        self._rows[table] = {row[".id"]: dict(row) for row in rows}
        return self._rebuild()

    def apply(self, table: Table, event: Row) -> set[str]:
        """Apply one `listen` event: a removal (`.dead`) or the item's new full row."""
        if _is_true(event.get(".dead")):
            self._rows[table].pop(event[".id"], None)
        else:
            self._rows[table][event[".id"]] = dict(event)
        return self._rebuild()

    def refresh(self) -> set[str]:
        """Re-evaluate time-dependent state (departure grace) after time has passed."""
        return self._rebuild()

    def seconds_until_grace_ends(self) -> float | None:
        """When to call `refresh()` next, or None if no host is inside its grace period."""
        if self._grace_ends_at is None:
            return None
        return max(0.0, self._grace_ends_at - self._clock())

    def _track_departures(self, on_bridge: set[str], now: float) -> dict[str, float]:
        """Note who just left the bridge; return {mac: when its grace period ends}."""
        self._departed_at.update(dict.fromkeys(self._on_bridge - on_bridge, now))
        for mac in on_bridge:
            self._departed_at.pop(mac, None)
        self._on_bridge = on_bridge
        in_grace = {
            mac: left_at + self._departure_grace
            for mac, left_at in self._departed_at.items()
            if now < left_at + self._departure_grace
        }
        self._departed_at = {mac: self._departed_at[mac] for mac in in_grace}
        return in_grace

    def _rebuild(self) -> set[str]:
        now = self._clock()
        leases = _leases_by_mac(self._rows[Table.LEASE].values())
        # The wired port wins when a MAC is on both: the bridge sees wifi clients on the AP's port.
        ports = _wifi_interfaces_by_mac(self._rows[Table.WIFI_REGISTRATION].values()) | _bridge_ports_by_mac(
            self._rows[Table.BRIDGE_HOST].values()
        )

        in_grace = self._track_departures(set(ports), now)
        self._grace_ends_at = min(in_grace.values(), default=None)

        previous, self.hosts = self.hosts, {
            mac: _host(mac, lease, ports.get(mac), mac in ports or mac in in_grace)
            for mac, lease in leases.items()
        }
        return {
            mac
            for mac in previous.keys() | self.hosts.keys()
            if previous.get(mac) != self.hosts.get(mac)
        }


def _host(mac: str, lease: Row, port: str | None, connected: bool) -> Host:
    return Host(
        mac=mac,
        name=lease.get("comment") or lease.get("host-name") or mac,
        hostname=lease.get("host-name") or None,
        ip_address=lease.get("active-address") or lease.get("address") or None,
        port=port,
        connected=connected,
        administered=bool(lease.get("comment")) or not _is_true(lease.get("dynamic", "true")),
    )


def _leases_by_mac(rows: Iterable[Row]) -> dict[str, Row]:
    enabled = (row for row in rows if not _is_true(row.get("disabled")))
    # Bound leases sort last so they win when a MAC holds leases on several DHCP servers.
    by_boundness = sorted(enabled, key=lambda row: row.get("status") == "bound")
    return {mac: row for row in by_boundness if (mac := _lease_mac(row))}


def _lease_mac(row: Row) -> str | None:
    mac = row.get("active-mac-address") or row.get("mac-address")
    return mac.upper() if mac else None


def _bridge_ports_by_mac(rows: Iterable[Row]) -> dict[str, str]:
    """Ports where non-local MACs were learned; one MAC can appear once per VLAN."""
    learned = [row for row in rows if _is_learned_host(row)]
    # Lowest row id wins (it is written last), so the port does not flip with other VLAN entries.
    return {
        row["mac-address"].upper(): row.get("on-interface", "")
        for row in sorted(learned, key=_row_number, reverse=True)
    }


def _wifi_interfaces_by_mac(rows: Iterable[Row]) -> dict[str, str]:
    """Clients registered on a wifi interface; one still in its handshake (not authorized) is not in yet."""
    return {
        row["mac-address"].upper(): row.get("interface", "")
        for row in rows
        if row.get("mac-address") and row.get("authorized", "true") != "false"
    }


def _is_learned_host(row: Row) -> bool:
    return bool(row.get("mac-address")) and not (
        _is_true(row.get("local"))
        or _is_true(row.get("invalid"))
        or _is_true(row.get("disabled"))
        or not _is_true(row.get("dynamic", "true"))  # a static entry would mean "home" forever
    )


def _row_number(row: Row) -> int:
    return int(row[".id"].removeprefix("*"), 16)


def _is_true(value: str | None) -> bool:
    return value in ("true", "yes")
