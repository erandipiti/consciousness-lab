"""Transport payload framing (spec §9.1).

The exact device bytes, preserved unmodified, for streams whose
``raw_capture_level`` is ``transport_payload``. Framing is fixed so two
implementations cannot disagree about where a record starts or ends:

    offset 0                magic        u32   0x444C5950 ("PYLD" little-endian)
    offset 4                packet_seq   u64
    offset 12               payload_len  u32
    offset 16               payload      payload_len bytes, verbatim
    offset 16+payload_len   crc32c       u32   over the payload bytes only

All integers little-endian and unsigned. Total record size is
``20 + payload_len``; records are byte-adjacent with no file header and no
padding, so a payload file is a pure concatenation.
"""

import struct
from dataclasses import dataclass
from typing import BinaryIO

MAGIC = 0x444C5950
HEADER = struct.Struct("<IQI")
CRC = struct.Struct("<I")
HEADER_SIZE = HEADER.size
CRC_SIZE = CRC.size
FRAME_OVERHEAD = HEADER_SIZE + CRC_SIZE

# CRC-32C (Castagnoli). zlib.crc32 is the wrong polynomial, and the spec names
# CRC-32C, so the table is built here rather than taking a dependency for 20
# lines of arithmetic.
_CRC32C_POLY = 0x82F63B78
_CRC32C_TABLE: list[int] = []
for _byte in range(256):
    _value = _byte
    for _ in range(8):
        _value = (_value >> 1) ^ (_CRC32C_POLY if _value & 1 else 0)
    _CRC32C_TABLE.append(_value)


def crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli) over ``data``."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC32C_TABLE[(crc ^ byte) & 0xFF] ^ (crc >> 8)
    return crc ^ 0xFFFFFFFF


class PayloadFramingError(ValueError):
    """A payload record is malformed, mis-referenced or fails its checksum."""


@dataclass(frozen=True)
class PayloadRef:
    """Where a packet's transport bytes live.

    ``offset`` is the byte offset of the record's ``magic`` field from the start
    of the file; ``length`` is ``payload_len``, the payload alone, excluding
    framing.
    """

    file: str
    offset: int
    length: int


def frame(packet_seq: int, payload: bytes) -> bytes:
    """Encode one payload record."""
    return HEADER.pack(MAGIC, packet_seq, len(payload)) + payload + CRC.pack(crc32c(payload))


def write_record(
    handle: BinaryIO, packet_seq: int, payload: bytes, *, file_name: str
) -> PayloadRef:
    """Append one record and return the reference that points back at it."""
    offset = handle.tell()
    handle.write(frame(packet_seq, payload))
    return PayloadRef(file=file_name, offset=offset, length=len(payload))


def read_at(data: bytes, ref: PayloadRef) -> tuple[int, bytes]:
    """Resolve a reference to ``(packet_seq, payload)``, failing closed.

    A reference whose offset does not land on ``magic`` is broken: the reader
    raises rather than returning whatever bytes happen to be at that position.
    """
    if ref.offset < 0 or ref.offset + HEADER_SIZE > len(data):
        raise PayloadFramingError(f"payload_ref offset {ref.offset} is outside the file")
    magic, packet_seq, payload_len = HEADER.unpack_from(data, ref.offset)
    if magic != MAGIC:
        raise PayloadFramingError(
            f"payload_ref offset {ref.offset} does not point at a record header"
        )
    if payload_len != ref.length:
        raise PayloadFramingError(
            f"payload_ref length {ref.length} disagrees with framed length {payload_len}"
        )
    start = ref.offset + HEADER_SIZE
    end = start + payload_len
    if end + CRC_SIZE > len(data):
        raise PayloadFramingError("payload record is truncated")
    payload = data[start:end]
    (stored_crc,) = CRC.unpack_from(data, end)
    if stored_crc != crc32c(payload):
        raise PayloadFramingError(f"payload CRC mismatch at offset {ref.offset}")
    return packet_seq, payload


def iter_records(data: bytes) -> list[tuple[int, bytes]]:
    """Walk a payload file from the start, stopping at the first bad record.

    Used by verification and recovery. A trailing partial record — the shape a
    crash leaves — terminates the walk rather than raising, because reporting
    "N complete records then a torn tail" is more useful than reporting nothing.
    """
    records: list[tuple[int, bytes]] = []
    offset = 0
    while offset + HEADER_SIZE <= len(data):
        magic, packet_seq, payload_len = HEADER.unpack_from(data, offset)
        if magic != MAGIC:
            break
        start = offset + HEADER_SIZE
        end = start + payload_len
        if end + CRC_SIZE > len(data):
            break
        payload = data[start:end]
        (stored_crc,) = CRC.unpack_from(data, end)
        if stored_crc != crc32c(payload):
            break
        records.append((packet_seq, payload))
        offset = end + CRC_SIZE
    return records
