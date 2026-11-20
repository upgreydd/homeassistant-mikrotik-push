"""RouterOS API wire format: length-prefixed words, a sentence ends with an empty word."""

from __future__ import annotations

import asyncio


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


MAX_WORD_LENGTH = 1 << 20  # a misbehaving peer must not make us allocate gigabytes


async def read_sentence(reader: asyncio.StreamReader) -> list[str]:
    words = []
    while (n := await read_length(reader)) != 0:
        if n > MAX_WORD_LENGTH:
            raise ValueError(f"word of {n} bytes exceeds the {MAX_WORD_LENGTH} byte limit")
        # errors=replace: DHCP host names are free text and may not be valid UTF-8
        words.append((await reader.readexactly(n)).decode("utf-8", errors="replace"))
    return words


def parse_reply(words: list[str]) -> tuple[str, str | None, dict[str, str]]:
    """Return (type like '!re', tag, attributes)."""
    if not words:
        return "", None, {}
    tag = None
    attrs: dict[str, str] = {}
    for w in words[1:]:
        if w.startswith(".tag="):
            tag = w[5:]
        elif w.startswith("="):
            k, _, v = w[1:].partition("=")
            attrs[k] = v
    return words[0], tag, attrs
