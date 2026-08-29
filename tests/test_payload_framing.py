"""Transport payload framing (spec §9.1)."""

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from consciousness_lab.storage.payload import (
    CRC,
    HEADER,
    MAGIC,
    PayloadFramingError,
    PayloadRef,
    crc32c,
    frame,
    iter_records,
    read_at,
)


def test_frame_layout_is_exactly_as_specified() -> None:
    payload = b"\x01\x02\x03"
    encoded = frame(7, payload)
    assert len(encoded) == 20 + len(payload)
    magic, packet_seq, length = HEADER.unpack_from(encoded, 0)
    assert magic == MAGIC == 0x444C5950
    assert packet_seq == 7
    assert length == 3
    assert encoded[16:19] == payload
    (stored,) = CRC.unpack_from(encoded, 19)
    assert stored == crc32c(payload)


def test_crc32c_is_castagnoli_not_zlib() -> None:
    """The spec names CRC-32C; zlib.crc32 is a different polynomial."""
    import zlib

    assert crc32c(b"123456789") == 0xE3069283
    assert crc32c(b"123456789") != zlib.crc32(b"123456789")


def test_read_at_resolves_a_reference() -> None:
    data = frame(1, b"aa") + frame(2, b"bbbb")
    first = read_at(data, PayloadRef("payloads/000000.bin", 0, 2))
    assert first == (1, b"aa")
    second = read_at(data, PayloadRef("payloads/000000.bin", 22, 4))
    assert second == (2, b"bbbb")


def test_reference_not_pointing_at_magic_fails_closed() -> None:
    """A broken reference raises rather than returning whatever bytes are there."""
    data = frame(1, b"aaaa")
    with pytest.raises(PayloadFramingError):
        read_at(data, PayloadRef("f", 3, 4))


def test_reference_past_the_end_fails_closed() -> None:
    with pytest.raises(PayloadFramingError):
        read_at(frame(1, b"a"), PayloadRef("f", 900, 1))


def test_corrupted_payload_fails_its_checksum() -> None:
    data = bytearray(frame(1, b"abcd"))
    data[16] ^= 0xFF
    with pytest.raises(PayloadFramingError):
        read_at(bytes(data), PayloadRef("f", 0, 4))


def test_length_disagreement_fails_closed() -> None:
    with pytest.raises(PayloadFramingError):
        read_at(frame(1, b"abcd"), PayloadRef("f", 0, 3))


def test_iter_records_stops_at_a_torn_tail() -> None:
    """The shape a crash leaves: complete records are recoverable, the tail is not."""
    data = frame(1, b"aa") + frame(2, b"bb")
    truncated = data[:-2]
    assert iter_records(truncated) == [(1, b"aa")]


def test_iter_records_walks_a_whole_file() -> None:
    data = b"".join(frame(i, bytes([i]) * i) for i in range(1, 5))
    assert iter_records(data) == [(i, bytes([i]) * i) for i in range(1, 5)]


@given(st.lists(st.binary(max_size=64), min_size=1, max_size=8))
def test_round_trip_property(payloads: list[bytes]) -> None:
    data = b"".join(frame(i, p) for i, p in enumerate(payloads))
    assert iter_records(data) == list(enumerate(payloads))
    offset = 0
    for i, p in enumerate(payloads):
        assert read_at(data, PayloadRef("f", offset, len(p))) == (i, p)
        offset += 20 + len(p)


def test_empty_payload_is_representable() -> None:
    data = frame(3, b"")
    assert read_at(data, PayloadRef("f", 0, 0)) == (3, b"")
    assert len(data) == 20


def test_header_is_little_endian() -> None:
    assert HEADER.format.startswith("<")
    assert struct.calcsize(HEADER.format) == 16
