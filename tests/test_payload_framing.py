"""Transport payload framing (v2 §13).

v2 removed ``payload_ref``, so there is nothing left to resolve a pointer
against. What the framing must now support is the derived index: walking a file
yields ``packet_seq -> bytes`` directly, and the packet relation is a bijection
by identity rather than a stored reference kept in step.
"""

import struct

from hypothesis import given
from hypothesis import strategies as st

from consciousness_lab.storage.payload import (
    CRC,
    HEADER,
    MAGIC,
    crc32c,
    frame,
    iter_frames,
    payloads_by_packet,
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


def test_a_frame_carries_its_own_packet_seq() -> None:
    """This is what made ``payload_ref`` redundant (§5)."""
    frames, error = iter_frames(frame(1, b"aa") + frame(2, b"bbbb"))
    assert error is None
    assert [(f.packet_seq, f.payload) for f in frames] == [(1, b"aa"), (2, b"bbbb")]


def test_payloads_by_packet_builds_the_index_deterministically() -> None:
    data = b"".join(frame(i, bytes([i]) * i) for i in range(1, 5))
    mapping, error = payloads_by_packet(data)
    assert error is None
    assert mapping == {i: bytes([i]) * i for i in range(1, 5)}


def test_a_duplicated_packet_seq_is_reported_not_overwritten() -> None:
    """Last-write-wins would hide two frames claiming one packet."""
    mapping, error = payloads_by_packet(frame(1, b"aa") + frame(1, b"bb"))
    assert error is not None and "more than once" in error
    assert mapping == {1: b"aa"}


def test_iter_frames_stops_at_a_torn_tail() -> None:
    """The shape a crash leaves: complete frames survive, the tail is reported."""
    data = frame(1, b"aa") + frame(2, b"bb")
    frames, error = iter_frames(data[:-2])
    assert [f.packet_seq for f in frames] == [1]
    assert error is not None


def test_a_corrupted_payload_fails_its_checksum() -> None:
    data = bytearray(frame(1, b"abcd"))
    data[16] ^= 0xFF
    frames, error = iter_frames(bytes(data))
    assert frames == []
    assert error is not None and "CRC" in error


def test_bad_magic_terminates_the_walk() -> None:
    frames, error = iter_frames(b"\x00\x00\x00\x00" + frame(1, b"a"))
    assert frames == []
    assert error is not None and "magic" in error


@given(st.lists(st.binary(max_size=64), min_size=1, max_size=8))
def test_round_trip_property(payloads: list[bytes]) -> None:
    data = b"".join(frame(i, p) for i, p in enumerate(payloads))
    mapping, error = payloads_by_packet(data)
    assert error is None
    assert mapping == dict(enumerate(payloads))


def test_empty_payload_is_representable() -> None:
    data = frame(3, b"")
    mapping, error = payloads_by_packet(data)
    assert error is None
    assert mapping == {3: b""}
    assert len(data) == 20


def test_header_is_little_endian() -> None:
    assert HEADER.format.startswith("<")
    assert struct.calcsize(HEADER.format) == 16
