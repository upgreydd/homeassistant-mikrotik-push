"""A tiny in-process RouterOS API server, shared by the unit and the Home Assistant tests."""

import asyncio
import importlib.util
import pathlib
import ssl
import subprocess

# Loaded by path so this file works with and without the integration package importable.
_WIRE_PATH = (
    pathlib.Path(__file__).parents[1] / "custom_components" / "mikrotik_push" / "wire.py"
)
_spec = importlib.util.spec_from_file_location("fake_router_wire", _WIRE_PATH)
wire = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wire)


# Commands that stream forever like `listen`, but are not `<path>/listen`.
STREAMING_COMMANDS = {"/interface/monitor-traffic", "/system/resource/monitor"}


class FakeRouter:
    def __init__(self, password: str = "secret") -> None:
        self.password = password
        self.tables: dict[str, list[dict[str, str]]] = {"/system/identity": [{"name": "fake"}]}
        self.reject_listen: set[str] = set()  # paths answering `listen` with a permission trap
        self.end_listen: set[str] = set()  # paths answering `listen` with a bare `!done`
        self.ethernet_monitor: dict[str, dict[str, str]] = {}  # port name -> `monitor once` row
        self.login_failure = "invalid user name or password (6)"
        self.muted = False  # swallow every command, like a half-dead link
        self.ignored_commands: set[str] = set()  # commands that never get an answer
        self.failing_commands: set[str] = set()  # commands answered with a trap
        self.login_count = 0
        self.cancelled_tags: list[str] = []
        self.stream_requests: list[tuple[str, dict[str, str]]] = []  # (command, attributes)
        self._writers: list[asyncio.StreamWriter] = []
        self._subscriptions: list[tuple[asyncio.StreamWriter, str, str]] = []
        self._server: asyncio.Server | None = None

    async def start(self, ssl_context: ssl.SSLContext | None = None) -> int:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=ssl_context)
        return self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        self.drop_connections()
        self._server.close()

    def drop_connections(self) -> None:
        for writer in self._writers:
            writer.close()
        self._writers.clear()
        self._subscriptions.clear()

    async def push(self, path: str, event: dict[str, str]) -> None:
        """Send a `listen` event to every subscriber of `path`."""
        for writer, subscribed_path, tag in self._subscriptions:
            if subscribed_path == path:
                _reply(writer, tag, "!re", event)
                await writer.drain()

    async def wait_for_subscribers(self, count: int) -> None:
        while len(self._subscriptions) < count:
            await asyncio.sleep(0.01)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        try:
            while True:
                command, tag, attrs = wire.parse_reply(await wire.read_sentence(reader))
                if not self.muted:
                    self._handle(writer, command, tag, attrs)
                    await writer.drain()
        except (asyncio.IncompleteReadError, OSError):  # includes a TLS shutdown gone wrong
            pass
        finally:
            writer.close()  # like a real server: hang up when the client has gone

    def _handle(self, writer, command: str, tag: str, attrs: dict[str, str]) -> None:
        if command in self.ignored_commands:
            return
        if command in self.failing_commands:
            _reply(writer, tag, "!trap", {"message": "failure: simulated"})
            _reply(writer, tag, "!done")
            return
        if command == "/login":
            self.login_count += 1
            if attrs.get("password") == self.password:
                _reply(writer, tag, "!done")
            else:
                _reply(writer, tag, "!trap", {"message": self.login_failure})
                _reply(writer, tag, "!done")
        elif command.endswith("/listen"):
            path = command.removesuffix("/listen")
            if path in self.reject_listen:
                _reply(writer, tag, "!trap", {"message": "not enough permissions (9)"})
                _reply(writer, tag, "!done")
            elif path in self.end_listen:
                _reply(writer, tag, "!done")
            else:
                self._subscriptions.append((writer, path, tag))
        elif command in STREAMING_COMMANDS:
            self.stream_requests.append((command, attrs))
            self._subscriptions.append((writer, command, tag))
        elif command == "/interface/ethernet/monitor":
            row = self.ethernet_monitor.get(attrs.get("numbers", ""))
            if row is None:
                _reply(writer, tag, "!trap", {"message": "no such item"})
            else:
                _reply(writer, tag, "!re", row)
            _reply(writer, tag, "!done")
        elif command == "/cancel":
            self._cancel(writer, tag, attrs["tag"])
        elif command.endswith("/print") and command.removesuffix("/print") in self.tables:
            rows = self.tables[command.removesuffix("/print")]
            for row in rows:
                _reply(writer, tag, "!re", row)
            if not rows:
                _reply(writer, tag, "!empty")
            _reply(writer, tag, "!done")
        else:
            _reply(writer, tag, "!trap", {"message": "no such command"})
            _reply(writer, tag, "!done")

    def _cancel(self, writer, tag: str, cancelled_tag: str) -> None:
        """Like a real router: the cancelled command ends with `!trap interrupted`."""
        self.cancelled_tags.append(cancelled_tag)
        self._subscriptions = [s for s in self._subscriptions if s[2] != cancelled_tag]
        _reply(writer, cancelled_tag, "!trap", {"message": "interrupted"})
        _reply(writer, cancelled_tag, "!done")
        _reply(writer, tag, "!done")


def _reply(writer, tag: str, kind: str, attrs: dict[str, str] | None = None) -> None:
    words = [kind, *(f"={key}={value}" for key, value in (attrs or {}).items()), f".tag={tag}"]
    writer.write(wire.encode_sentence(words))


async def until(condition, timeout: float = 3) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.01)


def make_self_signed_certificate(directory: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """A throwaway certificate for 127.0.0.1, made with the openssl command line tool."""
    certificate, key = directory / "router.pem", directory / "router.key"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=127.0.0.1", "-keyout", str(key), "-out", str(certificate)],
        check=True, capture_output=True,
    )
    return certificate, key


def certificate_sha256(certificate: pathlib.Path) -> str:
    """The fingerprint as openssl prints it, so the code under test is not checked against itself."""
    out = subprocess.run(
        ["openssl", "x509", "-in", str(certificate), "-noout", "-fingerprint", "-sha256"],
        check=True, capture_output=True, text=True,
    ).stdout
    return out.split("=", 1)[1].strip().replace(":", "").lower()


def server_ssl_context(certificate: pathlib.Path, key: pathlib.Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate, key)
    return context
