"""Minimal asyncio RouterOS API client (RouterOS 7, plain login, tags, `listen`).

Why our own: librouteros is blocking (executor threads + a global lock) and has no streaming
`listen`. The wire protocol is small: length-prefixed words, a sentence ends with an empty word.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import ssl
from collections.abc import AsyncIterator
from typing import Any


class RouterOSError(Exception):
    """The router answered !trap / !fatal (e.g. wrong password, no permission)."""


class ConnectionLost(RouterOSError):
    """The TCP connection died; the caller must reconnect and resync."""


def encode_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return (n | 0x8000).to_bytes(2, "big")
    if n < 0x200000:
        return (n | 0xC00000).to_bytes(3, "big")
    if n < 0x10000000:
        return (n | 0xE0000000).to_bytes(4, "big")
    return b"\xf0" + n.to_bytes(4, "big")


async def read_length(reader: asyncio.StreamReader) -> int:
    b = (await reader.readexactly(1))[0]
    if b < 0x80:
        return b
    if b < 0xC0:
        return ((b & 0x3F) << 8) | (await reader.readexactly(1))[0]
    if b < 0xE0:
        return ((b & 0x1F) << 16) | int.from_bytes(await reader.readexactly(2), "big")
    if b < 0xF0:
        return ((b & 0x0F) << 24) | int.from_bytes(await reader.readexactly(3), "big")
    return int.from_bytes(await reader.readexactly(4), "big")


def encode_sentence(words: list[str]) -> bytes:
    out = bytearray()
    for w in words:
        raw = w.encode("utf-8")
        out += encode_length(len(raw)) + raw
    return bytes(out) + b"\x00"


async def read_sentence(reader: asyncio.StreamReader) -> list[str]:
    words = []
    while (n := await read_length(reader)) != 0:
        # errors=replace: DHCP host names are free text and may not be valid UTF-8
        words.append((await reader.readexactly(n)).decode("utf-8", errors="replace"))
    return words


def parse_reply(words: list[str]) -> tuple[str, str | None, dict[str, str]]:
    """Return (type like '!re', tag, attributes)."""
    tag = None
    attrs: dict[str, str] = {}
    for w in words[1:]:
        if w.startswith(".tag="):
            tag = w[5:]
        elif w.startswith("="):
            k, _, v = w[1:].partition("=")
            attrs[k] = v
    return words[0], tag, attrs


def ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        # api-ssl needs a certificate on the router: certificate-less (anonymous DH) mode fails
        # the handshake with modern OpenSSL (seen on RouterOS 7.25.1).
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


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
        use_ssl: bool = False,
        verify_ssl: bool = False,
        timeout: float = 10,
    ) -> RouterOSConnection:
        ctx = ssl_context(verify_ssl) if use_ssl else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx), timeout
        )
        conn = cls(reader, writer)
        try:
            await asyncio.wait_for(
                conn.call("/login", name=username, password=password), timeout
            )
        except BaseException:
            await conn.close()
            raise
        return conn

    async def _read_loop(self) -> None:
        try:
            while True:
                kind, tag, attrs = parse_reply(await read_sentence(self._reader))
                if tag in self._queues:
                    self._queues[tag].put_nowait((kind, attrs))
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            pass
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
        self._writer.write(encode_sentence(words))
        await self._writer.drain()
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
        """Yield rows of `<path>/listen`; removed items carry '.dead': 'yes'.

        Note: `listen` first yields nothing for existing items, so call `<path>/print` first.
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
                elif kind == "!trap" and data.get("message") == "interrupted":
                    return
                elif kind in ("!trap", "!fatal"):
                    raise RouterOSError(data.get("message", kind))
        finally:
            if not self.closed.is_set():
                with contextlib.suppress(Exception):
                    self._writer.write(encode_sentence(["/cancel", f"=tag={tag}"]))
                    await self._writer.drain()
            self._queues.pop(tag, None)

    async def close(self) -> None:
        self._task.cancel()
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
        self.closed.set()


def as_bool(value: Any) -> bool:
    return str(value).lower() in ("true", "yes")
