"""Protocol tests against a fake RouterOS server (no router needed)."""

import asyncio
import importlib.util
import pathlib

import pytest

# Load the module directly: importing the package would pull in homeassistant.
_spec = importlib.util.spec_from_file_location(
    "routeros",
    pathlib.Path(__file__).parents[1] / "custom_components/mikrotik_push/routeros.py",
)
ros = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ros)


@pytest.mark.parametrize(
    "n,expected",
    [(0, b"\x00"), (0x7F, b"\x7f"), (0x80, b"\x80\x80"), (0x3FFF, b"\xbf\xff"),
     (0x4000, b"\xc0\x40\x00"), (0x200000, b"\xe0\x20\x00\x00")],
)
def test_length_vectors(n, expected):
    assert ros.encode_length(n) == expected


@pytest.mark.parametrize("n", [0, 1, 0x7F, 0x80, 0x3FFF, 0x4000, 0x1FFFFF, 0x200000, 0xFFFFFFF])
async def test_length_roundtrip(n):
    r = asyncio.StreamReader()
    r.feed_data(ros.encode_length(n))
    assert await ros.read_length(r) == n


class FakeRouter:
    """Speaks just enough RouterOS API. `script` maps a command path to a handler."""

    def __init__(self, password="secret"):
        self.password = password
        self.writers = []
        self.cancelled = []
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer):
        self.writers.append(writer)
        try:
            while True:
                words = await ros.read_sentence(reader)
                cmd, tag, attrs = ros.parse_reply(words)
                send = lambda *w: writer.write(ros.encode_sentence([*w, f".tag={tag}"]))
                if cmd == "/login":
                    if attrs.get("password") == self.password:
                        send("!done")
                    else:
                        send("!trap", "=message=invalid user name or password (6)")
                        send("!done")
                elif cmd == "/ip/arp/print":
                    send("!re", "=address=10.0.0.5", "=mac-address=AA:BB")
                    send("!re", "=address=10.0.0.6", "=mac-address=CC:DD")
                    send("!done")
                elif cmd == "/empty/print":
                    send("!empty")
                    send("!done")
                elif cmd == "/nope/print":
                    send("!trap", "=message=no such command")
                    send("!done")
                elif cmd == "/ip/arp/listen":
                    send("!re", "=.id=*1", "=address=10.0.0.7")
                    send("!re", "=.id=*1", "=.dead=yes")
                elif cmd == "/cancel":
                    self.cancelled.append(attrs["tag"])
                    send("!done")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass


@pytest.fixture
async def router():
    r = FakeRouter()
    port = await r.start()
    yield r, port
    r.server.close()


async def test_login_call_and_trap(router):
    r, port = router
    c = await ros.RouterOSConnection.open("127.0.0.1", port, "u", "secret")
    rows = await c.call("/ip/arp/print")
    assert [x["address"] for x in rows] == ["10.0.0.5", "10.0.0.6"]
    with pytest.raises(ros.RouterOSError, match="no such command"):
        await c.call("/nope/print")
    await c.close()


async def test_empty_result(router):
    _, port = router
    c = await ros.RouterOSConnection.open("127.0.0.1", port, "u", "secret")
    assert await c.call("/empty/print") == []  # RouterOS >= 7.18 sends !empty then !done
    await c.close()


async def test_bad_password(router):
    _, port = router
    with pytest.raises(ros.RouterOSError, match="invalid user"):
        await ros.RouterOSConnection.open("127.0.0.1", port, "u", "wrong")


async def test_listen_dead_and_cancel(router):
    r, port = router
    c = await ros.RouterOSConnection.open("127.0.0.1", port, "u", "secret")
    got = []
    async for row in c.listen("/ip/arp"):
        got.append(row)
        if row.get(".dead") == "yes":
            break
    assert got[0]["address"] == "10.0.0.7" and got[1][".dead"] == "yes"
    await asyncio.sleep(0.05)
    assert r.cancelled  # listen() sent /cancel on exit
    await c.close()


async def test_connection_loss_wakes_listener(router):
    r, port = router
    c = await ros.RouterOSConnection.open("127.0.0.1", port, "u", "secret")

    async def consume():
        async for _ in c.listen("/ip/arp"):
            pass

    t = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    for w in r.writers:
        w.close()
    with pytest.raises(ros.ConnectionLost):
        await asyncio.wait_for(t, 2)
    assert c.closed.is_set()
