"""Keep a HostRegistry in sync with the router: snapshot, stream changes, resync, reconnect."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .const import IDENTITY_PATH
from .registry import HostRegistry, Row, Table
from .routeros import (
    AccessRefused,
    ConnectionLost,
    RouterOSConnection,
    RouterOSError,
    forward_events,
)
from .telemetry import RouterTelemetry

_LOGGER = logging.getLogger(__name__)

TABLE_PATHS = {
    Table.LEASE: "/ip/dhcp-server/lease",
    Table.BRIDGE_HOST: "/interface/bridge/host",
}
# Only a router with the wifi package (an access point or a CAPsMAN manager) has this; elsewhere it
# is skipped, and the setup does not require it.
OPTIONAL_TABLE_PATHS = {
    Table.WIFI_REGISTRATION: "/interface/wifi/registration-table",
}

ConnectionFactory = Callable[[], Awaitable[RouterOSConnection]]
_EXPECTED_FAILURES = (RouterOSError, OSError, TimeoutError)


@dataclass(frozen=True, slots=True)
class Timing:
    """All durations in seconds."""

    departure_grace: float = 180
    resync_interval: float = 300
    keepalive_interval: float = 30
    keepalive_timeout: float = 10
    first_retry_delay: float = 1
    max_retry_delay: float = 60


@dataclass(frozen=True, slots=True)
class Change:
    """What a listener is told: which hosts changed and whether the link state flipped."""

    macs: frozenset[str] = frozenset()
    link: bool = False


Listener = Callable[[Change], None]


class RouterWatcher:
    """Owns the router connection; tells listeners which hosts changed."""

    def __init__(
        self,
        connect: ConnectionFactory,
        on_access_refused: Callable[[], None],
        timing: Timing = Timing(),
        telemetry: RouterTelemetry | None = None,
    ) -> None:
        self.registry = HostRegistry(timing.departure_grace)
        self.telemetry = telemetry
        self.connected = False
        self._connect = connect
        self._on_access_refused = on_access_refused
        self._timing = timing
        self._retry_delay = timing.first_retry_delay
        self._listeners: list[Listener] = []
        self._grace_timer: asyncio.TimerHandle | None = None
        self._resync_now = asyncio.Event()
        self._run_task: asyncio.Task | None = None

    def stop(self) -> None:
        """Stop watching (idempotent); the connection is closed as the task unwinds."""
        if self._run_task:
            self._run_task.cancel()

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    async def run(self, connection: RouterOSConnection) -> None:
        """Watch the router until cancelled, starting from an already open connection."""
        loop = asyncio.get_running_loop()
        self._run_task = asyncio.current_task()
        try:
            while True:
                started = loop.time()
                await self._run_session(connection)
                self._mark_disconnected()
                if loop.time() - started > self._timing.max_retry_delay:
                    self._retry_delay = self._timing.first_retry_delay  # the link was stable
                connection = await self._reconnect()
        except AccessRefused as error:
            _LOGGER.error("Access to the router was refused, stopping: %s", error)
            self._mark_disconnected()
            self._on_access_refused()
        finally:
            self.connected = False
            if self._grace_timer:
                self._grace_timer.cancel()

    async def _run_session(self, connection: RouterOSConnection) -> None:
        try:
            await self._watch(connection)
        except Exception as error:
            _log_failure("Lost the router connection", error)

    async def _watch(self, connection: RouterOSConnection) -> None:
        events: asyncio.Queue[tuple[Table, Row]] = asyncio.Queue()
        try:
            async with asyncio.TaskGroup() as tasks:
                # The listen commands are written to the socket before the snapshot is requested
                # (tasks start in order), so no change can slip between snapshot and stream.
                for table, path in (TABLE_PATHS | OPTIONAL_TABLE_PATHS).items():
                    stream = forward_events(
                        connection, path, lambda event, table=table: events.put_nowait((table, event))
                    )
                    tasks.create_task(_optional(path, stream) if table in OPTIONAL_TABLE_PATHS else stream)
                tasks.create_task(self._apply_snapshot_then_events(connection, events))
                tasks.create_task(self._keepalive(connection))
                if self.telemetry:
                    tasks.create_task(self.telemetry.run(connection))
                tasks.create_task(self._resync_periodically(connection))
        except ExceptionGroup as group:
            raise group.exceptions[0] from None
        finally:
            await connection.close()

    async def _apply_snapshot_then_events(
        self, connection: RouterOSConnection, events: asyncio.Queue[tuple[Table, Row]]
    ) -> None:
        changed = await self._load_snapshot(connection)
        self.connected = True
        self._publish(Change(frozenset(changed), link=True))

        while True:
            table, event = await events.get()
            self._publish(Change(frozenset(self.registry.apply(table, event))))

    async def _load_snapshot(self, connection: RouterOSConnection) -> set[str]:
        changed: set[str] = set()
        for table, path in TABLE_PATHS.items():
            changed |= self.registry.replace(table, await connection.call(f"{path}/print"))
        for table, path in OPTIONAL_TABLE_PATHS.items():
            changed |= self.registry.replace(table, await _optional(path, connection.call(f"{path}/print")) or [])
        return changed

    async def _resync_periodically(self, connection: RouterOSConnection) -> None:
        """Self-heal: a stream that silently stops delivering would otherwise freeze the state.

        Also runs when a departure grace ends: a host leaves only when the router's table agrees,
        because some routers' streams drop a host whose entry is still there (re-learned entries).
        """
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._resync_now.wait(), self._timing.resync_interval)
            self._resync_now.clear()
            corrected = await self._load_snapshot(connection)
            if corrected:
                _LOGGER.debug("Resync found %d changed host(s)", len(corrected))
            self._publish(Change(frozenset(corrected)))  # also re-arms the timer of a grace still running

    async def _keepalive(self, connection: RouterOSConnection) -> None:
        """Detect a silently dead link, which a quiet `listen` stream would never reveal."""
        while True:
            await asyncio.sleep(self._timing.keepalive_interval)
            await asyncio.wait_for(
                connection.call(IDENTITY_PATH), self._timing.keepalive_timeout
            )

    async def _reconnect(self) -> RouterOSConnection:
        """Wait, then connect; the wait doubles after every attempt until a session proves stable.

        Doubling per attempt (not only per failed connect) also slows down a session that keeps
        dying right away, e.g. one whose `listen` the router refuses.
        """
        while True:
            await asyncio.sleep(self._retry_delay)
            self._retry_delay = min(self._retry_delay * 2, self._timing.max_retry_delay)
            try:
                return await self._connect()
            except AccessRefused:
                raise
            except Exception as error:
                _log_failure("Reconnecting to the router failed", error)

    def _mark_disconnected(self) -> None:
        self.connected = False
        self._notify(Change(link=True))

    def _publish(self, change: Change) -> None:
        self._notify(change)
        self._arm_grace_timer()

    def _arm_grace_timer(self) -> None:
        if self._grace_timer:
            self._grace_timer.cancel()
            self._grace_timer = None
        delay = self.registry.seconds_until_grace_ends()
        if delay is not None:
            self._grace_timer = asyncio.get_running_loop().call_later(delay, self._end_grace)

    def _end_grace(self) -> None:
        self._grace_timer = None
        if self.connected:
            self._resync_now.set()  # confirm the departure with the router's table first
        else:
            self._publish(Change(frozenset(self.registry.refresh())))

    def _notify(self, change: Change) -> None:
        if not change.macs and not change.link:
            return
        for listener in list(self._listeners):
            try:
                listener(change)
            except Exception:
                _LOGGER.exception("A watcher listener failed")


async def _optional[T](path: str, reading: Awaitable[T]) -> T | None:
    """A table the router may not have: its refusal ends only this reading, a lost link ends all."""
    try:
        return await reading
    except ConnectionLost:
        raise
    except RouterOSError as error:
        _LOGGER.debug("%s is not followed: %s", path, error)
        return None


def _log_failure(message: str, error: Exception) -> None:
    """Warn briefly for expected link problems; add the traceback for anything else."""
    _LOGGER.warning("%s: %s", message, error, exc_info=not isinstance(error, _EXPECTED_FAILURES))
