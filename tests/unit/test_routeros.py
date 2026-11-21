"""Protocol tests for the RouterOS API client."""

import asyncio

import pytest
from fake_router import FakeRouter, until

from mikrotik_push import routeros


@pytest.fixture
async def router():
    fake = FakeRouter()
    port = await fake.start()
    yield fake, port
    fake.stop()


async def open_connection(port, password="secret"):
    return await routeros.RouterOSConnection.open("127.0.0.1", port, "user", password)


async def test_call_returns_rows(router):
    fake, port = router
    fake.tables["/ip/arp"] = [{"address": "10.0.0.5"}, {"address": "10.0.0.6"}]
    connection = await open_connection(port)
    rows = await connection.call("/ip/arp/print")
    assert [row["address"] for row in rows] == ["10.0.0.5", "10.0.0.6"]
    await connection.close()


async def test_a_query_selects_rows_on_the_router_next_to_the_attributes(router):
    fake, port = router
    fake.tables["/ip/route"] = [
        {"dst-address": "0.0.0.0/0", "distance": "1"}, {"dst-address": "10.0.0.0/8", "distance": "0"},
    ]
    connection = await open_connection(port)

    rows = await connection.call("/ip/route/print", queries={"dst-address": "0.0.0.0/0"}, detail="")

    assert rows == [{"dst-address": "0.0.0.0/0", "distance": "1"}]
    assert fake.requested_queries[-1] == ("/ip/route/print", {"dst-address": "0.0.0.0/0"})
    await connection.close()


async def test_empty_result_is_an_empty_list(router):
    fake, port = router
    fake.tables["/interface/wifi"] = []
    connection = await open_connection(port)
    assert await connection.call("/interface/wifi/print") == []
    await connection.close()


async def test_a_command_the_router_never_answers_times_out_and_the_link_stays_usable(
    router, monkeypatch
):
    monkeypatch.setattr("mikrotik_push.routeros.REQUEST_TIMEOUT", 0.1)
    fake, port = router
    fake.ignored_commands.add("/system/identity/print")
    connection = await open_connection(port)

    with pytest.raises(TimeoutError):
        await connection.call("/system/identity/print")

    fake.ignored_commands.clear()
    assert await connection.call("/system/identity/print") == [{"name": "fake"}]
    await connection.close()


async def test_trap_raises(router):
    _, port = router
    connection = await open_connection(port)
    with pytest.raises(routeros.RouterOSError, match="no such command"):
        await connection.call("/nope/print")
    await connection.close()


async def test_wrong_password_raises_login_rejected(router):
    _, port = router
    with pytest.raises(routeros.LoginRejected, match="invalid user"):
        await open_connection(port, password="wrong")


async def test_other_login_traps_are_not_reported_as_a_rejected_password(router):
    fake, port = router
    fake.login_failure = "too many sessions"
    with pytest.raises(routeros.RouterOSError, match="too many sessions") as raised:
        await open_connection(port, password="wrong")
    assert not isinstance(raised.value, routeros.LoginRejected)


async def test_listen_that_ends_with_done_returns(router):
    fake, port = router
    fake.end_listen.add("/ip/arp")
    connection = await open_connection(port)
    assert [event async for event in connection.listen("/ip/arp")] == []
    await connection.close()


async def test_unreachable_router_is_not_a_login_problem():
    with pytest.raises(OSError):
        await routeros.RouterOSConnection.open("127.0.0.1", 1, "user", "secret", timeout=1)


async def test_listen_yields_events_including_dead_and_cancels_on_exit(router):
    fake, port = router
    connection = await open_connection(port)
    received = []

    async def consume():
        async for event in connection.listen("/ip/arp"):
            received.append(event)
            if event.get(".dead") == "yes":
                return

    consumer = asyncio.create_task(consume())
    await fake.wait_for_subscribers(1)
    await fake.push("/ip/arp", {".id": "*1", "address": "10.0.0.7"})
    await fake.push("/ip/arp", {".id": "*1", ".dead": "yes"})
    await asyncio.wait_for(consumer, 2)

    assert [event[".id"] for event in received] == ["*1", "*1"]
    await until(lambda: fake.cancelled_tags)
    await connection.close()


async def test_listen_returns_quietly_when_the_router_interrupts_it(router):
    fake, port = router
    connection = await open_connection(port)

    async def consume():
        return [event async for event in connection.listen("/ip/arp")]

    consumer = asyncio.create_task(consume())
    await fake.wait_for_subscribers(1)
    writer, _, listen_tag = fake._subscriptions[0]
    fake._cancel(writer, "99", listen_tag)  # the router ends the stream on its own
    assert await asyncio.wait_for(consumer, 2) == []
    await connection.close()


async def test_connection_loss_wakes_listener(router):
    fake, port = router
    connection = await open_connection(port)

    async def consume():
        async for _ in connection.listen("/ip/arp"):
            pass

    consumer = asyncio.create_task(consume())
    await fake.wait_for_subscribers(1)
    fake.drop_connections()
    with pytest.raises(routeros.ConnectionLost):
        await asyncio.wait_for(consumer, 2)
    assert connection.closed.is_set()
    await connection.close()
