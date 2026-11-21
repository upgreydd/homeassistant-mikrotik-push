"""Minimal asyncio RouterOS API client (RouterOS 7, plain login, tags, `listen`).

Why our own: librouteros is blocking (executor threads + a global lock) and has no streaming
`listen`. The wire format lives in wire.py; this module adds connection, login and multiplexing.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import logging
import ssl
from collections.abc import AsyncIterator, Callable

from .const import REQUEST_TIMEOUT
from .wire import encode_sentence, parse_reply, read_sentence

_LOGGER = logging.getLogger(__name__)


class RouterOSError(Exception):
    """The router answered !trap / !fatal (e.g. no permission, unknown command)."""


class AccessRefused(RouterOSError):
    """The router cannot be used with what we hold: the credentials or its certificate changed."""


class LoginRejected(AccessRefused):
    """The router refused the user name or password."""


class CertificateChanged(AccessRefused):
    """The router presents another certificate than the one pinned on first use."""

    def __init__(self, presented_sha256: str | None) -> None:
        super().__init__("the router presents a different certificate than before")
        self.presented_sha256 = presented_sha256


class ConnectionLost(RouterOSError):
    """The TCP connection died; the caller must reconnect and resync."""


_BAD_CREDENTIALS = "invalid user name or password"  # RouterOS login error (6)
_CANCEL_TIMEOUT = 1


class RouterOSConnection:
    """One TCP connection, many concurrent commands (multiplexed by .tag)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._tags = itertools.count(1)
        self._queues: dict[str, asyncio.Queue] = {}
        self._task = asyncio.create_task(self._read_loop())
        self.closed = asyncio.Event()

    @classmethod
    async def open(
        cls,
        host: str,
        port: int,
        username: str,
        password: str,
        *,
        ssl_context: ssl.SSLContext | None = None,
        expected_certificate_sha256: str | None = None,
        timeout: float = 10,
    ) -> RouterOSConnection:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context), timeout
        )
        connection = cls(reader, writer)
        presented = connection.peer_certificate_sha256()
        if expected_certificate_sha256 and presented != expected_certificate_sha256:
            await connection.close()  # before the login: the password must not reach a stranger
            raise CertificateChanged(presented)
        try:
            await asyncio.wait_for(
                connection.call("/login", name=username, password=password), timeout
            )
        except ConnectionLost:
            await connection.close()
            raise
        except RouterOSError as error:
            await connection.close()
            if _BAD_CREDENTIALS in str(error):
                raise LoginRejected(str(error)) from error
            raise
        except BaseException:
            await connection.close()
            raise
        return connection

    def peer_certificate_sha256(self) -> str | None:
        """Fingerprint of the certificate the router presented; None on a plain connection."""
        tls = self._writer.get_extra_info("ssl_object")
        der = tls.getpeercert(binary_form=True) if tls else None
        return hashlib.sha256(der).hexdigest() if der else None

    async def _read_loop(self) -> None:
        try:
            while True:
                kind, tag, attrs = parse_reply(await read_sentence(self._reader))
                if tag in self._queues:
                    self._queues[tag].put_nowait((kind, attrs))
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            pass
        except Exception:
            _LOGGER.exception("Unreadable data from the router, dropping the connection")
        finally:
            self.closed.set()
            for q in self._queues.values():
                q.put_nowait(("!lost", {}))

    async def _send(self, path: str, attrs: dict[str, str]) -> tuple[str, asyncio.Queue]:
        if self.closed.is_set():
            raise ConnectionLost("connection closed")
        tag = str(next(self._tags))
        q: asyncio.Queue = asyncio.Queue()
        self._queues[tag] = q
        words = [path, *(f"={k}={v}" for k, v in attrs.items()), f".tag={tag}"]
        try:
            self._writer.write(encode_sentence(words))
            await self._writer.drain()
        except BaseException:
            del self._queues[tag]
            raise
        return tag, q

    async def call(self, path: str, **attrs: str) -> list[dict[str, str]]:
        """Run a command to completion and return its !re rows.

        A router that never answers raises TimeoutError after REQUEST_TIMEOUT; the connection stays usable.
        """
        async with asyncio.timeout(REQUEST_TIMEOUT):
            tag, q = await self._send(path, attrs)
            rows: list[dict[str, str]] = []
            try:
                while True:
                    kind, data = await _next_reply(q)
                    if kind == "!re":
                        rows.append(data)
                    elif kind == "!done":
                        return rows
                    else:  # !trap / !fatal
                        raise RouterOSError(data.get("message", kind))
            finally:
                del self._queues[tag]

    def listen(self, path: str, **attrs: str) -> AsyncIterator[dict[str, str]]:
        """Yield rows of `<path>/listen`; a removed item arrives as `.id` plus `.dead`.

        Only changes are streamed (no initial dump) and each event carries the full row,
        so take a `<path>/print` snapshot first. Verified on RouterOS 7.25.1.
        """
        return self.stream(f"{path}/listen", **attrs)

    async def stream(self, command: str, **attrs: str) -> AsyncIterator[dict[str, str]]:
        """Yield the rows of a command that keeps running (`listen`, `monitor-traffic`, ...)."""
        tag, q = await self._send(command, attrs)
        try:
            while True:
                kind, data = await _next_reply(q)
                if kind == "!re":
                    yield data
                elif kind == "!done" or (kind == "!trap" and data.get("message") == "interrupted"):
                    return
                else:  # !trap / !fatal
                    raise RouterOSError(data.get("message", kind))
        finally:
            if not self.closed.is_set():
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(_CANCEL_TIMEOUT):
                        self._writer.write(encode_sentence(["/cancel", f"=tag={tag}"]))
                        await self._writer.drain()
            self._queues.pop(tag, None)

    async def close(self) -> None:
        self._task.cancel()
        self._writer.close()
        # A dead TLS link can make wait_closed() hang while it waits for the peer's close_notify.
        with contextlib.suppress(Exception):
            async with asyncio.timeout(2):
                await self._writer.wait_closed()
        self.closed.set()


async def _next_reply(q: asyncio.Queue) -> tuple[str, dict[str, str]]:
    """The next reply that matters: `!empty` (RouterOS >= 7.18) is skipped, a lost link raises."""
    while True:
        kind, data = await q.get()
        if kind == "!empty":
            continue
        if kind == "!lost":
            raise ConnectionLost("connection lost")
        return kind, data


async def forward_events(
    connection: RouterOSConnection, path: str, deliver: Callable[[dict[str, str]], None]
) -> None:
    """Hand every `listen` event of `path` to `deliver`; ending at all counts as a lost link."""
    async for event in connection.listen(path):
        deliver(event)
    raise ConnectionLost(f"{path} stream ended")
