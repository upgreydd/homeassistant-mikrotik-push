"""Minimal asyncio RouterOS API client (RouterOS 7, plain login, tags, `listen`).

Why our own: librouteros is blocking (executor threads + a global lock) and has no streaming
`listen`. The wire format lives in wire.py; this module adds connection, login and multiplexing.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import ssl
from collections.abc import AsyncIterator

from .wire import encode_sentence, parse_reply, read_sentence

_LOGGER = logging.getLogger(__name__)


class RouterOSError(Exception):
    """The router answered !trap / !fatal (e.g. no permission, unknown command)."""


class LoginRejected(RouterOSError):
    """The router refused the user name or password."""


class ConnectionLost(RouterOSError):
    """The TCP connection died; the caller must reconnect and resync."""


class StreamEnded(ConnectionLost):
    """The router closed a `listen` stream that should run forever."""


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
        timeout: float = 10,
    ) -> RouterOSConnection:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context), timeout
        )
        connection = cls(reader, writer)
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
        """Run a command to completion and return its !re rows."""
        tag, q = await self._send(path, attrs)
        rows: list[dict[str, str]] = []
        try:
            while True:
                kind, data = await q.get()
                if kind == "!re":
                    rows.append(data)
                elif kind == "!empty":  # RouterOS >= 7.18: empty result, !done follows
                    continue
                elif kind == "!done":
                    return rows
                elif kind == "!lost":
                    raise ConnectionLost("connection lost")
                else:  # !trap / !fatal
                    raise RouterOSError(data.get("message", kind))
        finally:
            del self._queues[tag]

    async def listen(self, path: str, **attrs: str) -> AsyncIterator[dict[str, str]]:
        """Yield rows of `<path>/listen`; a removed item arrives as `.id` plus `.dead`.

        Only changes are streamed (no initial dump) and each event carries the full row,
        so take a `<path>/print` snapshot first. Verified on RouterOS 7.25.1.
        """
        tag, q = await self._send(f"{path}/listen", attrs)
        try:
            while True:
                kind, data = await q.get()
                if kind == "!re":
                    yield data
                elif kind == "!empty":
                    continue
                elif kind == "!lost":
                    raise ConnectionLost("connection lost")
                elif kind == "!done" or (kind == "!trap" and data.get("message") == "interrupted"):
                    return
                elif kind in ("!trap", "!fatal"):
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
