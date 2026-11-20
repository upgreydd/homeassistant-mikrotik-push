"""RouterWatcher: snapshot, live changes, resync, grace, reconnect and failure handling."""

import asyncio
import contextlib

import pytest
from fake_router import FakeRouter, until

from mikrotik_push import routeros
from mikrotik_push.watcher import RouterWatcher, Timing

MAC = "AA:BB:CC:DD:EE:01"
LEASE_PATH = "/ip/dhcp-server/lease"
BRIDGE_PATH = "/interface/bridge/host"
LEASE = {".id": "*1", "mac-address": MAC, "address": "10.0.0.5", "status": "bound",
         "host-name": "phone"}
BRIDGE_HOST = {".id": "*B1", "mac-address": MAC, "on-interface": "ether3", "dynamic": "true",
               "local": "false"}
BRIDGE_HOST_GONE = {".id": "*B1", ".dead": "yes"}

FAST = Timing(departure_grace=0, resync_interval=60, keepalive_interval=60,
              first_retry_delay=0.02, max_retry_delay=0.1)


@pytest.fixture
async def router():
    fake = FakeRouter()
    fake.tables.update({LEASE_PATH: [LEASE], BRIDGE_PATH: []})
    fake.port = await fake.start()
    yield fake
    fake.stop()


class Harness:
    """Runs a watcher against the fake router and cleans up after the test."""

    def __init__(self, router, timing):
        self.router = router
        self.login_rejected_calls = 0
        self.watcher = RouterWatcher(self._connect, self._on_login_rejected, timing)
        self.task = None

    async def _connect(self):
        return await routeros.RouterOSConnection.open(
            "127.0.0.1", self.router.port, "user", "secret", timeout=1
        )

    def _on_login_rejected(self):
        self.login_rejected_calls += 1

    async def start(self):
        self.task = asyncio.create_task(self.watcher.run(await self._connect()))
        return self.watcher


@pytest.fixture
async def start_watcher(router):
    harnesses = []

    async def start(timing=FAST):
        harness = Harness(router, timing)
        harnesses.append(harness)
        await harness.start()
        return harness

    yield start
    for harness in harnesses:
        harness.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await harness.task


async def test_snapshot_then_live_events(router, start_watcher):
    harness = await start_watcher()
    watcher = harness.watcher
    await until(lambda: watcher.connected)
    assert not watcher.registry.hosts[MAC].connected

    await router.push(BRIDGE_PATH, BRIDGE_HOST)
    await until(lambda: watcher.registry.hosts[MAC].connected)
    await router.push(BRIDGE_PATH, BRIDGE_HOST_GONE)
    await until(lambda: not watcher.registry.hosts[MAC].connected)


async def test_listeners_are_told_when_the_link_goes_down(router, start_watcher):
    watcher = (await start_watcher()).watcher
    await until(lambda: watcher.connected)
    seen = []
    watcher.add_listener(lambda change: seen.append((change.link, watcher.connected)))

    router.drop_connections()
    await until(lambda: seen)
    assert seen[0] == (True, False)


async def test_reconnect_resyncs_changes_missed_while_offline(router, start_watcher):
    watcher = (await start_watcher()).watcher
    await until(lambda: watcher.connected)

    router.drop_connections()
    router.tables[BRIDGE_PATH] = [BRIDGE_HOST]  # happened while the link was down
    await until(lambda: watcher.connected and watcher.registry.hosts[MAC].connected)


async def test_silent_link_is_detected_by_the_keepalive(router, start_watcher):
    timing = Timing(departure_grace=0, keepalive_interval=0.05, keepalive_timeout=0.1,
                    first_retry_delay=0.02, max_retry_delay=0.1)
    watcher = (await start_watcher(timing)).watcher
    await until(lambda: watcher.connected)

    router.muted = True
    await until(lambda: not watcher.connected)
    router.muted = False
    router.drop_connections()
    await until(lambda: watcher.connected)


async def test_periodic_resync_heals_a_stream_that_missed_an_event(router, start_watcher):
    timing = Timing(departure_grace=0, resync_interval=0.05)
    watcher = (await start_watcher(timing)).watcher
    await until(lambda: watcher.connected)

    router.tables[BRIDGE_PATH] = [BRIDGE_HOST]  # no listen event is ever pushed for this
    await until(lambda: watcher.registry.hosts[MAC].connected)


async def test_departure_is_reported_when_the_grace_period_ends(router, start_watcher):
    router.tables[BRIDGE_PATH] = [BRIDGE_HOST]
    timing = Timing(departure_grace=0.15, resync_interval=60)
    watcher = (await start_watcher(timing)).watcher
    await until(lambda: watcher.connected and watcher.registry.hosts[MAC].connected)
    changes = []
    watcher.add_listener(lambda _change: changes.append(watcher.registry.hosts[MAC].connected))

    await router.push(BRIDGE_PATH, BRIDGE_HOST_GONE)
    await until(lambda: changes)
    assert changes[-1] is True  # port flushed, but still counted as home

    await until(lambda: not watcher.registry.hosts[MAC].connected)  # timer fires, no event needed
    assert changes[-1] is False


async def test_a_failing_listener_does_not_stop_the_watcher(router, start_watcher):
    watcher = (await start_watcher()).watcher
    await until(lambda: watcher.connected)

    def broken(_changed):
        raise RuntimeError("boom")

    watcher.add_listener(broken)
    await router.push(BRIDGE_PATH, BRIDGE_HOST)
    await until(lambda: watcher.registry.hosts[MAC].connected)
    await router.push(BRIDGE_PATH, BRIDGE_HOST_GONE)
    await until(lambda: not watcher.registry.hosts[MAC].connected)


async def test_rejected_login_on_reconnect_stops_retrying_and_asks_for_reauth(router, start_watcher):
    harness = await start_watcher()
    await until(lambda: harness.watcher.connected)

    router.password = "changed-on-the-router"
    router.drop_connections()
    await until(lambda: harness.login_rejected_calls == 1)
    await asyncio.wait_for(harness.task, 2)  # run() returned instead of retrying forever
    assert not harness.watcher.connected


async def test_a_permission_trap_on_listen_backs_off_instead_of_hammering_the_router(
    router, start_watcher
):
    router.reject_listen.add(BRIDGE_PATH)
    started = asyncio.get_running_loop().time()
    await start_watcher(Timing(departure_grace=0, first_retry_delay=0.05, max_retry_delay=0.4))

    await until(lambda: router.login_count >= 4)
    # the sleeps before logins 2, 3 and 4 are 0.05, 0.1 and 0.2 seconds; a tight loop would be ~0
    assert asyncio.get_running_loop().time() - started >= 0.3


async def test_a_listen_stream_that_ends_quietly_leads_to_a_reconnect(router, start_watcher):
    router.end_listen.add(BRIDGE_PATH)
    await start_watcher()
    await until(lambda: router.login_count >= 2)
