import asyncio
from datetime import UTC, datetime

import pytest
from fake_router import FakeRouter, until
from mikrotik_push.router_info import parse_uptime, read_router_info
from mikrotik_push.routeros import RouterOSConnection
from mikrotik_push.telemetry import Channel, Features, RouterOsUpdate, RouterTelemetry, parse_rate_mbps

INTERFACES = [
    {".id": "*1", "name": "ether1", "type": "ether", "running": "true", "disabled": "false"},
    {".id": "*2", "name": "ether2", "type": "ether", "running": "false", "disabled": "false"},
    {".id": "*3", "name": "bridge", "type": "bridge", "running": "true", "disabled": "false"},
]


@pytest.fixture
async def router():
    fake = FakeRouter()
    fake.tables.update(
        {
            "/interface": INTERFACES,
            "/system/resource": [
                {"uptime": "1d2h", "total-memory": "1073741824", "version": "7.25.1",
                 "board-name": "RB5009UG+S+"}
            ],
            "/system/routerboard": [{"model": "RB5009UG+S+", "serial-number": "SN1"}],
            "/system/package/update": [
                {"installed-version": "7.25.1", "latest-version": "7.26", "channel": "stable",
                 "status": "New version is available"}
            ],
            "/system/health": [
                {".id": "*1", "name": "cpu-temperature", "value": "51", "type": "C"},
                {".id": "*2", "name": "fan-state", "value": "ok", "type": ""},
            ],
        }
    )
    fake.ethernet_monitor["ether1"] = {"name": "ether1", "status": "link-ok", "rate": "2.5Gbps"}
    fake.port = await fake.start()
    yield fake
    fake.stop()


async def connect(router):
    return await RouterOSConnection.open("127.0.0.1", router.port, "u", "secret")


async def start(router, features, publish_interval=0.05):
    telemetry = RouterTelemetry(features, publish_interval)
    connection = await connect(router)
    task = asyncio.create_task(telemetry.run(connection))
    return telemetry, connection, task


async def stop(connection, task):
    task.cancel()
    await connection.close()


def test_parse_rate_mbps():
    assert [parse_rate_mbps(t) for t in ("10Mbps", "100Mbps", "1Gbps", "2.5Gbps", "10Gbps")] == [
        10, 100, 1000, 2500, 10000,
    ]
    assert parse_rate_mbps(None) is None
    assert parse_rate_mbps("") is None


def test_parse_uptime():
    assert parse_uptime("1w5d6h13m16s") == 604800 + 5 * 86400 + 6 * 3600 + 13 * 60 + 16
    assert parse_uptime("59s") == 59


async def test_router_info_reads_identity_model_and_memory(router):
    connection = await connect(router)
    info = await read_router_info(connection)
    await connection.close()
    assert (info.name, info.model, info.serial_number, info.version) == (
        "fake", "RB5009UG+S+", "SN1", "7.25.1",
    )


async def test_only_ethernet_ports_are_reported_with_speed_for_those_with_link(router):
    telemetry, connection, task = await start(router, Features(ports=True))
    await until(lambda: telemetry.ports)

    assert {p.name: (p.running, p.rate_mbps) for p in telemetry.ports.values()} == {
        "ether1": (True, 2500),
        "ether2": (False, None),
    }
    await stop(connection, task)


async def test_a_link_change_event_updates_the_port_and_asks_for_the_new_speed(router):
    telemetry, connection, task = await start(router, Features(ports=True))
    await until(lambda: telemetry.ports)
    changes: list[Channel] = []
    telemetry.add_listener(changes.append)
    router.ethernet_monitor["ether2"] = {"name": "ether2", "status": "link-ok", "rate": "1Gbps"}

    await router.push("/interface", {**INTERFACES[1], "running": "true"})

    await until(lambda: telemetry.ports["ether2"].running)
    assert telemetry.ports["ether2"].rate_mbps == 1000
    assert changes == [Channel.PORTS]
    await stop(connection, task)


async def test_a_missing_speed_does_not_break_the_link_state(router):
    router.ethernet_monitor.clear()  # the router answers "no such item"
    telemetry, connection, task = await start(router, Features(ports=True))
    await until(lambda: telemetry.ports)

    assert telemetry.ports["ether1"].running and telemetry.ports["ether1"].rate_mbps is None
    await stop(connection, task)


async def test_traffic_is_averaged_and_announced_at_most_once_per_interval(router):
    telemetry, connection, task = await start(router, Features(traffic=True), publish_interval=0.3)
    await router.wait_for_subscribers(2)  # /interface listen + monitor-traffic
    announcements: list[Channel] = []
    telemetry.add_listener(announcements.append)
    row = {"name": "ether1", "tx-bits-per-second": "7"}

    await router.push("/interface/monitor-traffic", {**row, "rx-bits-per-second": "1"})
    await until(lambda: announcements)
    assert telemetry.traffic["ether1"].rx_bps == 1  # the first sample is announced at once

    for bps in (2, 4, 6):  # arrive while the interval runs
        await router.push("/interface/monitor-traffic", {**row, "rx-bits-per-second": str(bps)})
    assert announcements == [Channel.TRAFFIC]

    await until(lambda: len(announcements) == 2)
    assert telemetry.traffic["ether1"].rx_bps == 4  # mean of 2, 4, 6, not the last sample
    await stop(connection, task)


async def test_system_load_memory_uptime_and_health(router):
    telemetry, connection, task = await start(router, Features(system=True))
    await until(lambda: telemetry.boot_time and telemetry.health and telemetry.update)
    await router.wait_for_subscribers(1)

    await router.push("/system/resource/monitor", {"cpu-used": "9", "free-memory": "1024"})

    await until(lambda: telemetry.load)
    assert telemetry.load.cpu_percent == 9
    assert telemetry.load.free_memory == 1024 * 1024  # KiB -> bytes
    assert abs((datetime.now(UTC) - telemetry.boot_time).total_seconds() - 93600) < 5
    assert telemetry.update == RouterOsUpdate("7.25.1", "7.26", "New version is available")
    assert telemetry.total_memory == 1073741824
    assert list(telemetry.health) == ["cpu-temperature"]  # the fan has no numeric value
    assert telemetry.health["cpu-temperature"].value == 51.0
    await stop(connection, task)


async def test_nothing_is_requested_when_no_feature_is_enabled(router):
    telemetry, connection, task = await start(router, Features())
    await asyncio.sleep(0.1)

    assert not telemetry.ports and telemetry.load is None
    assert router._subscriptions == []
    await stop(connection, task)


async def test_a_failing_optional_reading_does_not_end_the_session(router):
    del router.tables["/system/health"]  # the router answers "no such command"
    telemetry, connection, task = await start(router, Features(system=True))
    await router.wait_for_subscribers(1)

    await router.push("/system/resource/monitor", {"cpu-used": "9", "free-memory": "1024"})

    await until(lambda: telemetry.load)
    assert not task.done() and telemetry.health == {}
    await stop(connection, task)


async def test_a_new_session_starts_without_the_readings_of_the_old_one(router):
    telemetry, connection, task = await start(router, Features(traffic=True))
    await router.wait_for_subscribers(2)
    await router.push("/interface/monitor-traffic", {"name": "ether1", "rx-bits-per-second": "5"})
    await until(lambda: telemetry.traffic)
    await stop(connection, task)

    connection = await connect(router)
    task = asyncio.create_task(telemetry.run(connection))
    await asyncio.sleep(0)

    assert telemetry.traffic == {}
    await stop(connection, task)


async def test_a_renamed_port_keeps_its_key_and_restarts_the_traffic_stream(router):
    router.tables["/interface"] = [
        {".id": "*1", "name": "ether1", "default-name": "ether1", "type": "ether", "running": "true"},
        {".id": "*2", "name": "ether2", "default-name": "ether2", "type": "ether", "running": "false"},
    ]
    telemetry, connection, task = await start(router, Features(ports=True, traffic=True))
    await router.wait_for_subscribers(2)
    assert router.stream_requests[0][1]["interface"] == "ether1,ether2"

    await router.push(
        "/interface",
        {".id": "*1", "name": "wan", "default-name": "ether1", "type": "ether", "running": "true"},
    )

    await until(lambda: telemetry.ports["ether1"].name == "wan")
    await until(lambda: len(router.stream_requests) == 2)
    assert router.stream_requests[1][1]["interface"] == "ether2,wan"
    await stop(connection, task)


async def test_a_removed_interface_disappears(router):
    telemetry, connection, task = await start(router, Features(ports=True))
    await until(lambda: telemetry.ports)

    await router.push("/interface", {".id": "*2", ".dead": "true"})

    await until(lambda: set(telemetry.ports) == {"ether1"})
    await stop(connection, task)


async def test_the_traffic_of_a_removed_port_does_not_linger(router):
    telemetry, connection, task = await start(router, Features(ports=True, traffic=True), 0.05)
    await router.wait_for_subscribers(2)
    await router.push(
        "/interface/monitor-traffic",
        {"name": "ether2", "rx-bits-per-second": "1", "tx-bits-per-second": "2"},
    )
    await until(lambda: "ether2" in telemetry.traffic)

    await router.push("/interface", {".id": "*2", ".dead": "true"})

    await until(lambda: "ether2" not in telemetry.traffic)
    await stop(connection, task)


async def test_the_update_is_unknown_until_the_router_has_checked(router):
    router.tables["/system/package/update"] = [
        {"installed-version": "7.25.1", "latest-version": "", "status": "ERROR: could not connect"}
    ]
    telemetry, connection, task = await start(router, Features(system=True))

    await until(lambda: telemetry.update)

    assert telemetry.update.latest is None and telemetry.update.installed == "7.25.1"
    await stop(connection, task)


async def test_an_interface_change_that_no_event_announced_is_found_by_the_resync(router, monkeypatch):
    monkeypatch.setattr("mikrotik_push.telemetry.INTERFACE_RESYNC_SECONDS", 0.05)
    telemetry, connection, task = await start(router, Features(ports=True))
    await until(lambda: telemetry.ports)
    router.ethernet_monitor["ether2"] = {"name": "ether2", "status": "link-ok", "rate": "1Gbps"}

    router.tables["/interface"] = [INTERFACES[0], {**INTERFACES[1], "running": "true"}, INTERFACES[2]]

    await until(lambda: telemetry.ports["ether2"].running)
    assert telemetry.ports["ether2"].rate_mbps == 1000
    await stop(connection, task)


async def test_a_failed_resync_does_not_end_the_port_updates(router, monkeypatch):
    monkeypatch.setattr("mikrotik_push.telemetry.INTERFACE_RESYNC_SECONDS", 0.05)
    telemetry, connection, task = await start(router, Features(ports=True))
    await until(lambda: telemetry.ports)
    router.failing_commands.add("/interface/print")
    await asyncio.sleep(0.15)  # several resyncs fail
    router.failing_commands.clear()
    router.ethernet_monitor["ether2"] = {"name": "ether2", "status": "link-ok", "rate": "1Gbps"}

    router.tables["/interface"] = [INTERFACES[0], {**INTERFACES[1], "running": "true"}, INTERFACES[2]]

    await until(lambda: telemetry.ports["ether2"].running)
    assert not task.done()
    await stop(connection, task)


async def test_a_reading_the_router_never_answers_is_dropped_without_ending_the_session(
    router, monkeypatch
):
    monkeypatch.setattr("mikrotik_push.routeros.REQUEST_TIMEOUT", 0.1)
    monkeypatch.setattr("mikrotik_push.telemetry.HEALTH_POLL_SECONDS", 0.05)
    router.ignored_commands.add("/system/health/print")
    telemetry, connection, task = await start(router, Features(system=True))
    await asyncio.sleep(0.4)  # several polls time out
    assert not task.done() and telemetry.health == {}

    router.ignored_commands.clear()

    await until(lambda: telemetry.health)
    await stop(connection, task)


async def test_a_reading_that_failed_is_tried_again_on_the_next_poll(router, monkeypatch):
    monkeypatch.setattr("mikrotik_push.telemetry.HEALTH_POLL_SECONDS", 0.05)
    router.tables["/system/health"] = [{".id": "*1", "name": "cpu-temperature", "value": "N/A", "type": "C"}]
    telemetry, connection, task = await start(router, Features(system=True))
    await router.wait_for_subscribers(1)
    await asyncio.sleep(0.15)
    assert telemetry.health == {}

    router.tables["/system/health"] = [{".id": "*1", "name": "cpu-temperature", "value": "52", "type": "C"}]

    await until(lambda: telemetry.health)
    assert telemetry.health["cpu-temperature"].value == 52.0 and not task.done()
    await stop(connection, task)
