"""Exact 64-bit integer logical types for JSON (`DECISIONS.md` D25, spec §12.2.1).

RFC 8785 constrains JSON Numbers to IEEE-754 doubles, so integers are only
interoperably exact within +/-(2^53 - 1). Study 001 UTC timestamps sit near
1.8e18, roughly 200x beyond that: written as a JSON Number,
1787923530123456789 round-trips as 1787923530123456768 in any conforming parser.

So any field whose *declared semantic domain* is int64/uint64 is carried in JSON
as a canonical decimal string. In Python it stays an ``int`` — the string is a
serialization detail, and no value here ever transits a float.
"""

import re
from typing import Annotated, Any

from pydantic import BeforeValidator, PlainSerializer

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
UINT64_MIN = 0
UINT64_MAX = 2**64 - 1

# Canonical decimal grammars. Anchored, ASCII only, no leading '+', no leading
# zeros except the single value "0", no '-0', no decimal point, no exponent, no
# surrounding whitespace. One integer must have exactly one spelling: two
# spellings would give one record two hashes.
_UINT64_RE = re.compile(r"^(0|[1-9][0-9]*)$")
_INT64_RE = re.compile(r"^(0|-?[1-9][0-9]*)$")


class IntegerRepresentationError(ValueError):
    """A value is not a canonical decimal representation of its declared domain."""


def _parse(value: Any, *, signed: bool) -> int:
    """Parse an int64/uint64 logical value from JSON or from Python.

    Accepts a Python ``int`` (already exact) or a canonical decimal string.
    Everything else is rejected — notably ``float`` and ``bool``, because a
    float has already lost precision by the time it reaches us and ``bool`` is
    an ``int`` subclass that would otherwise slip through silently.
    """
    lo, hi = (INT64_MIN, INT64_MAX) if signed else (UINT64_MIN, UINT64_MAX)
    name = "int64_decimal" if signed else "uint64_decimal"

    if isinstance(value, bool):
        raise IntegerRepresentationError(f"{name}: bool is not an integer value")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        pattern = _INT64_RE if signed else _UINT64_RE
        if not pattern.match(value):
            raise IntegerRepresentationError(
                f"{name}: {value!r} is not a canonical decimal integer"
            )
        parsed = int(value)
    else:
        raise IntegerRepresentationError(
            f"{name}: expected int or canonical decimal string, got {type(value).__name__}"
        )

    if not lo <= parsed <= hi:
        raise IntegerRepresentationError(f"{name}: {parsed} out of range [{lo}, {hi}]")
    return parsed


def parse_int64(value: Any) -> int:
    """Parse an ``int64_decimal`` value."""
    return _parse(value, signed=True)


def parse_uint64(value: Any) -> int:
    """Parse a ``uint64_decimal`` value."""
    return _parse(value, signed=False)


def serialize_int(value: int) -> str:
    """Render an integer as its canonical decimal string.

    ``str(int)`` is already canonical for both domains: no '+', no leading
    zeros, no '-0' (Python has no negative zero integer), no exponent.
    """
    return str(value)


Int64Decimal = Annotated[
    int,
    BeforeValidator(parse_int64),
    PlainSerializer(serialize_int, return_type=str, when_used="always"),
]

UInt64Decimal = Annotated[
    int,
    BeforeValidator(parse_uint64),
    PlainSerializer(serialize_int, return_type=str, when_used="always"),
]
