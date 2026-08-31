"""Transport payload framing (v2 §13).

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

**v2 removes ``payload_ref``.** A frame carries its own ``packet_seq``, so the
packet -> bytes mapping is produced by walking the file; a persisted pointer was
a second copy of what parsing already yields (§5). The relation the verifier
enforces is therefore a bijection by identity: the multiset of frame
``packet_seq`` values equals the multiset of ``packets.packet_seq`` values.
"""

import struct
from dataclasses import dataclass

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


def frame(packet_seq: int, payload: bytes) -> bytes:
    """Encode one payload record."""
    return HEADER.pack(MAGIC, packet_seq, len(payload)) + payload + CRC.pack(crc32c(payload))


@dataclass(frozen=True)
class Frame:
    """One framed payload record, decomposed into its leaves.

    The frame is not an opaque blob: it carries its own ``packet_seq``, which
    duplicates the packet row's. A valid CRC proves the frame is internally
    intact; it does not prove the frame belongs to the packet that references
    it. Both are checked separately.
    """

    offset: int
    packet_seq: int
    payload_len: int
    total_len: int
    payload: bytes


def iter_frames(data: bytes) -> tuple[list[Frame], str | None]:
    """Walk a payload file, returning every complete frame and any trailing error.

    A torn trailing record — the shape a crash leaves — terminates the walk with
    an error string rather than raising, so callers can report "N complete
    frames then a torn tail".
    """
    frames: list[Frame] = []
    offset = 0
    while offset < len(data):
        if offset + HEADER_SIZE > len(data):
            return frames, f"truncated header at offset {offset}"
        magic, packet_seq, payload_len = HEADER.unpack_from(data, offset)
        if magic != MAGIC:
            return frames, f"bad magic at offset {offset}"
        start = offset + HEADER_SIZE
        end = start + payload_len
        if end + CRC_SIZE > len(data):
            return frames, f"truncated payload at offset {offset}"
        payload = data[start:end]
        (stored_crc,) = CRC.unpack_from(data, end)
        if stored_crc != crc32c(payload):
            return frames, f"CRC mismatch at offset {offset}"
        frames.append(
            Frame(
                offset=offset,
                packet_seq=packet_seq,
                payload_len=payload_len,
                total_len=FRAME_OVERHEAD + payload_len,
                payload=payload,
            )
        )
        offset = end + CRC_SIZE
    return frames, None


def payloads_by_packet(data: bytes) -> tuple[dict[int, bytes], str | None]:
    """Map ``packet_seq -> payload bytes`` by walking the file.

    This replaces v1's persisted ``payload_ref``: the index is derived, so there
    is no stored pointer that can drift out of step with the frames. A duplicate
    ``packet_seq`` is reported rather than silently overwritten, because two
    frames claiming one packet is a real defect and last-write-wins would hide
    it.
    """
    frames, error = iter_frames(data)
    mapping: dict[int, bytes] = {}
    for item in frames:
        if item.packet_seq in mapping:
            return mapping, f"packet_seq {item.packet_seq} is framed more than once"
        mapping[item.packet_seq] = item.payload
    return mapping, error
