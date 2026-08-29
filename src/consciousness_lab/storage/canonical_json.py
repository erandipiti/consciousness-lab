"""RFC 8785 (JCS) canonicalization and the record hash procedure (D24, spec §12.2).

Every hashed record in the package goes through this module and only this
module. Spreading serialization logic would let two call sites drift, and a
drifting canonicalization silently invalidates every hash that depends on it.

`json.dumps(sort_keys=True, ...)` is explicitly NOT JCS and is not used here:
JCS orders keys by UTF-16 code units where Python orders by code point (they
diverge above the BMP), and JCS mandates ECMAScript number formatting.
"""

import hashlib
import json
from typing import Any

RECORD_HASH_KEY = "record_sha256"
ZERO_HASH = "0" * 64

# ECMAScript JSON.stringify escapes: the two mandatory characters plus the
# short forms for the control characters that have them.
_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class CanonicalizationError(ValueError):
    """A value cannot be canonicalized under this project's JCS profile."""


def _escape(text: str) -> str:
    out: list[str] = ['"']
    for char in text:
        short = _SHORT_ESCAPES.get(char)
        if short is not None:
            out.append(short)
        elif char < " ":
            out.append(f"\\u{ord(char):04x}")
        else:
            # Every other character, including non-ASCII, is emitted literally.
            out.append(char)
    out.append('"')
    return "".join(out)


def _sort_key(key: str) -> bytes:
    """JCS orders object keys by UTF-16 code units, not by code point.

    Encoding to UTF-16BE and comparing bytes reproduces that order exactly,
    including for astral-plane characters where code-point order differs.
    """
    return key.encode("utf-16-be")


def _serialize(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        # Only bounded integers reach JSON as Numbers (§12.2.1); int64/uint64
        # domains are decimal strings by then. ECMAScript renders an integral
        # value as plain digits, which is what str() gives.
        return str(value)
    if isinstance(value, float):
        # Deliberate restriction, not an omission. This design places no float
        # anywhere in JSON (D25), so rather than ship a partially-correct
        # ECMAScript double formatter we refuse floats outright. NaN, Infinity
        # and -0.0 are rejected by the same rule.
        raise CanonicalizationError(
            "float is not permitted in a canonical record; "
            "int64/uint64 use decimal strings and no other float fields exist"
        )
    if isinstance(value, str):
        return _escape(value)
    if isinstance(value, list):
        return "[" + ",".join(_serialize(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: _sort_key(kv[0]))
        for key, _ in items:
            if not isinstance(key, str):
                raise CanonicalizationError(f"object keys must be strings, got {type(key)}")
        return "{" + ",".join(f"{_escape(k)}:{_serialize(v)}" for k, v in items) + "}"
    raise CanonicalizationError(f"cannot canonicalize {type(value).__name__}")


def canonicalize(value: Any) -> bytes:
    """Return the RFC 8785 canonical UTF-8 encoding of ``value``."""
    return _serialize(value).encode("utf-8")


def loads(data: str | bytes) -> Any:
    """Parse JSON, refusing the non-standard literals Python's json accepts.

    ``parse_constant`` fires for NaN/Infinity/-Infinity, which are not valid
    JSON and must never enter a hashed record.
    """

    def _reject(constant: str) -> Any:
        raise CanonicalizationError(f"{constant} is not valid JSON in a canonical record")

    return json.loads(data, parse_constant=_reject)


def record_hash(record: dict[str, Any]) -> str:
    """SHA-256 of the record's canonical bytes with ``record_sha256`` omitted.

    Omitted, not nulled and not blanked: the hash is computed over a record
    that does not contain the key at all, so a verifier can reproduce it by
    deletion alone.
    """
    without = {k: v for k, v in record.items() if k != RECORD_HASH_KEY}
    return hashlib.sha256(canonicalize(without)).hexdigest()


def seal_record(record: dict[str, Any]) -> bytes:
    """Compute ``record_sha256``, insert it, and return the canonical bytes."""
    sealed = dict(record)
    sealed[RECORD_HASH_KEY] = record_hash(record)
    return canonicalize(sealed)


def verify_record(record: dict[str, Any]) -> bool:
    """True when the record's stored ``record_sha256`` matches a recomputation.

    Verification re-canonicalizes from the parsed object rather than trusting
    the bytes on disk, so reformatted whitespace or reordered keys cannot pass
    as a different record: either they canonicalize identically, meaning
    nothing changed, or the hash fails.
    """
    stored = record.get(RECORD_HASH_KEY)
    if not isinstance(stored, str):
        return False
    return stored == record_hash(record)


def dump_line(record: dict[str, Any]) -> bytes:
    """A sealed record as one JSONL line. The newline is not part of the hash."""
    return seal_record(record) + b"\n"
