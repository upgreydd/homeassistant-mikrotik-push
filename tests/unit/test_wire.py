"""Wire format: length prefixes and sentences."""

import asyncio

import pytest

from mikrotik_push import wire


@pytest.mark.parametrize(
    ("length", "encoded"),
    [(0, b"\x00"), (0x7F, b"\x7f"), (0x80, b"\x80\x80"), (0x3FFF, b"\xbf\xff"),
     (0x4000, b"\xc0\x40\x00"), (0x200000, b"\xe0\x20\x00\x00")],
)
def test_length_encoding_vectors(length, encoded):
    assert wire.encode_length(length) == encoded


@pytest.mark.parametrize("length", [0, 1, 0x7F, 0x80, 0x3FFF, 0x4000, 0x1FFFFF, 0x200000, 0xFFFFFFF])
async def test_length_roundtrip(length):
    reader = asyncio.StreamReader()
    reader.feed_data(wire.encode_length(length))
    assert await wire.read_length(reader) == length


async def test_sentence_roundtrip_with_non_ascii_text():
    reader = asyncio.StreamReader()
    reader.feed_data(wire.encode_sentence(["!re", "=host-name=Żółć", ".tag=7"]))
    kind, tag, attrs = wire.parse_reply(await wire.read_sentence(reader))
    assert (kind, tag, attrs) == ("!re", "7", {"host-name": "Żółć"})


async def test_oversized_word_is_refused_before_it_is_read():
    reader = asyncio.StreamReader()
    reader.feed_data(wire.encode_length(wire.MAX_WORD_LENGTH + 1))
    with pytest.raises(ValueError, match="exceeds"):
        await wire.read_sentence(reader)


def test_empty_sentence_parses_to_nothing():
    assert wire.parse_reply([]) == ("", None, {})


async def test_invalid_utf8_is_replaced_not_fatal():
    reader = asyncio.StreamReader()
    reader.feed_data(wire.encode_length(3) + b"\xff\xfe!" + b"\x00")
    assert await wire.read_sentence(reader) == ["��!"]
