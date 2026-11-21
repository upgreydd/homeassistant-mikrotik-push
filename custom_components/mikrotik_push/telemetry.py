"""Ports, traffic and system readings pushed by the router, kept apart from presence tracking.

Everything arrives over the watcher's one connection: `listen` for port state, router-side
`monitor-*` streams for traffic and load. The 5 s samples are averaged and only announced to
listeners every `publish_interval` seconds, so Home Assistant is not asked to write a state per
sample. A reading that fails (no permission, a firmware without that command) is dropped on its
own: it must never end the session that presence tracking depends on.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from .router_info import parse_uptime
from .routeros import ConnectionLost, RouterOSConnection, RouterOSError, forward_events

_LOGGER = logging.getLogger(__name__)

INTERFACE_PATH = "/interface"
ROUTE_PATH = "/ip/route"
DEFAULT_ROUTE = "0.0.0.0/0"
TRAFFIC_COMMAND = "/interface/monitor-traffic"
RESOURCE_COMMAND = "/system/resource/monitor"
STREAM_INTERVAL = "5s"  # the longest interval the router accepts
HEALTH_POLL_SECONDS = 60  # /system/health has no stream, one cheap read per minute
UPDATE_POLL_SECONDS = 6 * 3600  # the router itself only learns of new versions now and then
INTERFACE_RESYNC_SECONDS = 300  # a stream that silently stops must not freeze the port states
BOOT_TIME_TOLERANCE = timedelta(seconds=60)  # re-reading the uptime must not make the boot time jitter
PORT_TYPES = frozenset({"ether"})  # includes SFP cages
_RATE_MBPS = {"Mbps": 1, "Gbps": 1000}
_READING_FAILURES = (RouterOSError, KeyError, ValueError, TimeoutError)  # TimeoutError: no answer
_LOAD = "load"


class Channel(StrEnum):
    PORTS = "ports"
    TRAFFIC = "traffic"
    SYSTEM = "system"
    UPDATE = "update"
    UPLINK = "uplink"


@dataclass(frozen=True, slots=True)
class Features:
    """Which optional readings the user asked for."""

    ports: bool = False
    traffic: bool = False
    system: bool = False
    uplink: bool = False

    @property
    def needs_interfaces(self) -> bool:
        return self.ports or self.traffic


@dataclass(frozen=True, slots=True)
class Port:
    name: str  # what the router calls it now; users may rename it
    running: bool
    rate_mbps: int | None  # negotiated speed; None while the link is down


@dataclass(frozen=True, slots=True)
class Traffic:
    rx_bps: int
    tx_bps: int


@dataclass(frozen=True, slots=True)
class Load:
    cpu_percent: int
    free_memory: int  # bytes


@dataclass(frozen=True, slots=True)
class Health:
    value: float
    kind: str  # RouterOS unit: C (temperature), V (voltage)


@dataclass(frozen=True, slots=True)
class DefaultRoute:
    """One IPv4 default route; the active one of the main table is where internet traffic goes."""

    interface: str | None  # None while the router cannot say (e.g. the link of a static gateway is down)
    gateway: str
    distance: int
    table: str
    active: bool
    comment: str | None

    def as_attributes(self) -> dict[str, str | int | bool]:
        return {
            "interface": self.interface, "gateway": self.gateway, "distance": self.distance,
            "table": self.table, "active": self.active, "comment": self.comment,
        }


@dataclass(frozen=True, slots=True)
class RouterOsUpdate:
    installed: str
    latest: str | None  # None until the router has checked for updates
    status: str  # the router's own words, e.g. "System is already up to date"


Listener = Callable[[Channel], None]


def parse_rate_mbps(text: str | None) -> int | None:
    """`2.5Gbps` -> 2500, `100Mbps` -> 100, anything else (no link) -> None."""
    if not text:
        return None
    for suffix, factor in _RATE_MBPS.items():
        if text.endswith(suffix):
            return round(float(text.removesuffix(suffix)) * factor)
    return None


def default_routes(rows: Iterable[dict[str, str]]) -> tuple[DefaultRoute, ...]:
    """Every enabled IPv4 default route: the main table first, then each table by distance."""
    routes = []
    for row in rows:
        if row.get("dst-address") != DEFAULT_ROUTE or row.get("disabled") in ("true", "yes"):
            continue  # a disabled route cannot carry traffic; a failover may switch one off
        try:
            distance = int(row.get("distance", "0"))
        except ValueError:
            continue
        gateway = row.get("gateway", "")
        routes.append(
            DefaultRoute(
                interface=_outgoing_interface(row.get("immediate-gw", ""), gateway),
                gateway=gateway,
                distance=distance,
                table=row.get("routing-table", "main"),
                active=row.get("active") == "true",
                comment=row.get("comment") or None,
            )
        )
    return tuple(sorted(routes, key=lambda r: (r.table != "main", r.table, r.distance, not r.active)))


def _outgoing_interface(immediate_gateway: str, gateway: str) -> str | None:
    """`10.0.0.1%ether1` -> `ether1`; a gateway that is an interface (`wg0`, `pppoe-out1`) is its own."""
    resolved = immediate_gateway or gateway  # no immediate gateway: the link of the route is down
    if "%" in resolved:  # several comma-separated gateways: a route that shares its load
        return ",".join(hop.rpartition("%")[2] for hop in resolved.split(","))
    try:
        ipaddress.ip_address(resolved)
    except ValueError:
        return resolved or None
    return immediate_gateway or None  # a bare address says nothing about the interface


def port_key(row: dict[str, str]) -> str:
    """Stable identity of a port: the factory name survives a rename by the user."""
    return row.get("default-name") or row["name"]


class _Averager:
    """Collects samples per key and hands out their means once per publication."""

    def __init__(self) -> None:
        self._sums: dict[str, list[float]] = {}

    def add(self, key: str, *values: float) -> None:
        entry = self._sums.setdefault(key, [0, *([0.0] * len(values))])
        entry[0] += 1
        for index, value in enumerate(values, start=1):
            entry[index] += value

    def take(self) -> dict[str, tuple[float, ...]]:
        means = {key: tuple(total / entry[0] for total in entry[1:]) for key, entry in self._sums.items()}
        self._sums = {}
        return means


class RouterTelemetry:
    """Holds the latest readings; `run()` keeps them current for one connection."""

    def __init__(self, features: Features, publish_interval: float) -> None:
        self.features = features
        self.total_memory = 0
        self.ports: dict[str, Port] = {}  # by port_key()
        self.traffic: dict[str, Traffic] = {}  # by port_key()
        self.load: Load | None = None
        self.health: dict[str, Health] = {}
        self.boot_time: datetime | None = None
        self.update: RouterOsUpdate | None = None
        self.routes: tuple[DefaultRoute, ...] = ()
        self.routes_known = False  # False until the routes have been read on this connection
        self._publish_interval = publish_interval
        self._interfaces: dict[str, dict[str, str]] = {}
        self._listeners: list[Listener] = []
        self._fast_values_changed = asyncio.Event()
        self._traffic_means = _Averager()
        self._load_means = _Averager()
        self._streamed_names: frozenset[str] = frozenset()
        self._ports_changed = asyncio.Event()

    @property
    def uplink(self) -> DefaultRoute | None:
        """The default route in use: the first active one of the main table; None if there is none."""
        return next((route for route in self.routes if route.active and route.table == "main"), None)

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def run(self, connection: RouterOSConnection) -> None:
        """Follow the router until the connection fails."""
        self._forget_readings()
        events: asyncio.Queue[dict[str, str] | None] = asyncio.Queue()
        try:
            async with asyncio.TaskGroup() as tasks:
                if self.features.needs_interfaces:
                    # Listening starts before the snapshot is read, as for presence: nothing is lost.
                    tasks.create_task(
                        _optional("Interface events", forward_events(connection, INTERFACE_PATH, events.put_nowait))
                    )
                    tasks.create_task(_request_resync_periodically(events))
                tasks.create_task(self._follow(connection, events, tasks))
                tasks.create_task(self._publish_fast_values())
        except ExceptionGroup as group:
            raise group.exceptions[0] from None

    def _forget_readings(self) -> None:
        """A new connection may face a rebooted router: nothing seen before is current."""
        self.ports = {}
        self.traffic = {}
        self.load = None
        self.health = {}
        self.routes = ()
        self.routes_known = False
        self._traffic_means = _Averager()
        self._load_means = _Averager()
        self._streamed_names = frozenset()

    async def _follow(
        self,
        connection: RouterOSConnection,
        events: asyncio.Queue[dict[str, str] | None],
        tasks: asyncio.TaskGroup,
    ) -> None:
        features = self.features
        if features.system:
            tasks.create_task(_optional("Uptime", self._load_uptime(connection)))
        if features.needs_interfaces:
            await _optional("Interfaces", self._load_interfaces(connection))
        if features.traffic:
            tasks.create_task(_optional("Traffic", self._follow_traffic(connection)))
        if features.system:
            tasks.create_task(_optional("CPU and memory", self._follow_load(connection)))
            tasks.create_task(
                _poll("Temperatures and voltages", lambda: self._read_health(connection), HEALTH_POLL_SECONDS)
            )
            tasks.create_task(
                _poll("RouterOS version", lambda: self._read_update(connection), UPDATE_POLL_SECONDS)
            )
        if features.uplink:
            tasks.create_task(
                _poll("Active default route", lambda: self._read_uplink(connection), self._publish_interval)
            )
        if features.needs_interfaces:
            await _optional("Interface events", self._apply_interface_events(connection, events))

    # --- interfaces: link state (push) and negotiated speed (asked once per link change)

    async def _load_interfaces(self, connection: RouterOSConnection) -> None:
        self._interfaces = {row[".id"]: row for row in await connection.call("/interface/print")}
        await self._refresh_ports(connection)

    async def _apply_interface_events(
        self, connection: RouterOSConnection, events: asyncio.Queue[dict[str, str] | None]
    ) -> None:
        """Serves events and resync requests from one queue, so updates never overlap."""
        while True:
            event = await events.get()
            if event is None:
                await self._resync_interfaces(connection)
                continue
            if event.get(".dead") in ("true", "yes"):
                self._interfaces.pop(event[".id"], None)
            else:
                self._interfaces[event[".id"]] = {**self._interfaces.get(event[".id"], {}), **event}
            await self._refresh_ports(connection)

    async def _resync_interfaces(self, connection: RouterOSConnection) -> None:
        """Re-read the port list; a failed read is retried at the next resync, not fatal."""
        try:
            await self._load_interfaces(connection)
        except ConnectionLost:
            raise
        except _READING_FAILURES as error:
            _LOGGER.warning("Re-reading the ports failed, will retry: %s", error)

    async def _refresh_ports(self, connection: RouterOSConnection) -> None:
        ports: dict[str, Port] = {}
        for row in self._interfaces.values():
            if row.get("type") not in PORT_TYPES:
                continue
            key, name, running = port_key(row), row["name"], row.get("running") == "true"
            known = self.ports.get(key)
            if known is not None and known.running == running and (known.rate_mbps or not running):
                rate = known.rate_mbps  # same link state: the speed has not changed either
            else:
                rate = await _ask_rate(connection, name) if running else None
            ports[key] = Port(name, running, rate)
        changed = ports != self.ports
        self.ports = ports
        self.traffic = {key: value for key, value in self.traffic.items() if key in ports}
        if self._traffic_names() != self._streamed_names:
            self._ports_changed.set()
        if changed and self.features.ports:
            self._notify(Channel.PORTS)

    # --- traffic and load: router-side streams

    def _traffic_names(self) -> frozenset[str]:
        # A comma would split one interface name into several in the command.
        return frozenset(port.name for port in self.ports.values() if "," not in port.name)

    async def _follow_traffic(self, connection: RouterOSConnection) -> None:
        """Stream the ports known now; start over whenever the set of port names changes."""
        while True:
            names = self._traffic_names()
            self._streamed_names = names
            self._ports_changed.clear()
            if names:
                await self._stream_until_ports_change(connection, names)
            else:
                await self._ports_changed.wait()

    async def _stream_until_ports_change(
        self, connection: RouterOSConnection, names: frozenset[str]
    ) -> None:
        stream = asyncio.create_task(self._consume_traffic(connection, names))
        changed = asyncio.create_task(self._ports_changed.wait())
        try:
            done, _ = await asyncio.wait({stream, changed}, return_when=asyncio.FIRST_COMPLETED)
            if stream in done:
                stream.result()  # the stream only ends by failing
        finally:
            for task in (stream, changed):
                task.cancel()
            await asyncio.gather(stream, changed, return_exceptions=True)

    async def _consume_traffic(self, connection: RouterOSConnection, names: frozenset[str]) -> None:
        keys_by_name = {port.name: key for key, port in self.ports.items()}
        async for row in connection.stream(
            TRAFFIC_COMMAND, interface=",".join(sorted(names)), interval=STREAM_INTERVAL
        ):
            key = keys_by_name.get(row["name"])
            if key is not None:
                self._traffic_means.add(
                    key, int(row.get("rx-bits-per-second", 0)), int(row.get("tx-bits-per-second", 0))
                )
                self._fast_values_changed.set()
        raise ConnectionLost("traffic stream ended")

    async def _follow_load(self, connection: RouterOSConnection) -> None:
        async for row in connection.stream(RESOURCE_COMMAND, interval=STREAM_INTERVAL):
            self._load_means.add(_LOAD, int(row["cpu-used"]), int(row["free-memory"]))
            self._fast_values_changed.set()
        raise ConnectionLost("resource stream ended")

    async def _load_uptime(self, connection: RouterOSConnection) -> None:
        resource = (await connection.call("/system/resource/print"))[0]
        self.total_memory = int(resource.get("total-memory", self.total_memory))
        boot_time = datetime.now(UTC) - timedelta(seconds=parse_uptime(resource.get("uptime", "")))
        if self.boot_time is None or abs(boot_time - self.boot_time) > BOOT_TIME_TOLERANCE:
            self.boot_time = boot_time
        self._notify(Channel.SYSTEM)

    async def _read_health(self, connection: RouterOSConnection) -> None:
        rows = await connection.call("/system/health/print")
        health = {
            row["name"]: Health(float(row["value"]), row.get("type", ""))
            for row in rows
            if row.get("type") in ("C", "V") and row.get("value")
        }
        if health != self.health:
            self.health = health
            self._notify(Channel.SYSTEM)

    async def _read_uplink(self, connection: RouterOSConnection) -> None:
        """The routing table has no `listen` worth following (a big table would flood us): poll the
        default routes only, selected by the router, so the cost does not grow with the table."""
        try:
            rows = await connection.call(f"{ROUTE_PATH}/print", queries={"dst-address": DEFAULT_ROUTE})
        except _READING_FAILURES:
            if self.routes_known:  # old routes must not pass for current ones while the reads fail
                self.routes_known = False
                self._notify(Channel.UPLINK)
            raise
        routes = default_routes(rows)
        if routes != self.routes or not self.routes_known:
            self.routes, self.routes_known = routes, True
            self._notify(Channel.UPLINK)

    async def _read_update(self, connection: RouterOSConnection) -> None:
        rows = await connection.call("/system/package/update/print")
        if rows and rows[0].get("installed-version"):
            row = rows[0]
            update = RouterOsUpdate(
                installed=row["installed-version"],
                latest=row.get("latest-version") or None,
                status=row.get("status", ""),
            )
            if update != self.update:
                self.update = update
                self._notify(Channel.UPDATE)

    async def _publish_fast_values(self) -> None:
        """First sample goes out at once; after that at most one announcement per interval."""
        while True:
            await self._fast_values_changed.wait()
            self._fast_values_changed.clear()
            for key, (rx, tx) in self._traffic_means.take().items():
                self.traffic[key] = Traffic(round(rx), round(tx))
            if load := self._load_means.take().get(_LOAD):
                self.load = Load(cpu_percent=round(load[0]), free_memory=round(load[1]) * 1024)  # KiB
            if self.features.traffic:
                self._notify(Channel.TRAFFIC)
            if self.features.system:
                self._notify(Channel.SYSTEM)
            await asyncio.sleep(self._publish_interval)

    def _notify(self, channel: Channel) -> None:
        for listener in list(self._listeners):
            try:
                listener(channel)
            except Exception:
                _LOGGER.exception("A telemetry listener failed")


async def _request_resync_periodically(events: asyncio.Queue[dict[str, str] | None]) -> None:
    while True:
        await asyncio.sleep(INTERFACE_RESYNC_SECONDS)
        events.put_nowait(None)


async def _poll(what: str, read: Callable[[], Awaitable[None]], seconds: float) -> None:
    """Repeat one reading; a failed round is logged (once while it keeps failing) and retried."""
    failing = False
    while True:
        try:
            await read()
        except ConnectionLost:
            raise
        except _READING_FAILURES as error:
            _LOGGER.log(logging.DEBUG if failing else logging.WARNING, "%s unavailable: %s", what, error)
            failing = True
        else:
            failing = False
        await asyncio.sleep(seconds)


async def _optional(what: str, reading: Awaitable[None]) -> None:
    """Run one optional reading; its own failure ends only that reading, a lost link ends all."""
    try:
        await reading
    except ConnectionLost:
        raise
    except _READING_FAILURES as error:
        _LOGGER.warning("%s unavailable: %s", what, error)


async def _ask_rate(connection: RouterOSConnection, port: str) -> int | None:
    try:
        rows = await connection.call("/interface/ethernet/monitor", numbers=port, once="")
    except ConnectionLost:
        raise
    except (RouterOSError, TimeoutError) as error:  # e.g. a port type that has no ethernet monitor
        _LOGGER.debug("No speed for %s: %s", port, error)
        return None
    return parse_rate_mbps(rows[0].get("rate")) if rows else None
