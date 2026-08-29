"""Exact int64/uint64 JSON semantics — acceptance tests J1-J4, J7 (spec §12.2.1, D25)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.integer_types import (
    INT64_MAX,
    INT64_MIN,
    UINT64_MAX,
    IntegerRepresentationError,
    parse_int64,
    parse_uint64,
    serialize_int,
)

#: The value that motivated the whole rule: a real UTC nanosecond timestamp.
#: As a JSON Number it rounds to 1787923530123456768 under IEEE-754.
UTC_NS = 1787923530123456789

NON_CANONICAL = ["01", "+1", "-0", "1.0", "1e3", " 1", "1 ", "", "-01", "0x10", "١٢٣"]


def test_j1_utc_nanoseconds_round_trip_exactly() -> None:
    """J1: int64 -> decimal string -> JCS -> parse -> int64, exact."""
    encoded = canonical_json.canonicalize({"utc_ns": serialize_int(UTC_NS)})
    assert encoded == b'{"utc_ns":"1787923530123456789"}'
    decoded = parse_int64(canonical_json.loads(encoded)["utc_ns"])
    assert decoded == UTC_NS
    # The failure this test exists to catch, spelled out.
    assert decoded != int(float(UTC_NS))


@pytest.mark.parametrize("value", [INT64_MIN, INT64_MAX, 0, -1, 1])
def test_j2_int64_limits_round_trip(value: int) -> None:
    """J2: the signed 64-bit extremes survive a full round trip."""
    encoded = canonical_json.canonicalize({"v": serialize_int(value)})
    assert parse_int64(canonical_json.loads(encoded)["v"]) == value


def test_j3_uint64_maximum_round_trips() -> None:
    """J3: 2^64-1 survives, which no double can represent."""
    encoded = canonical_json.canonicalize({"v": serialize_int(UINT64_MAX)})
    assert parse_uint64(canonical_json.loads(encoded)["v"]) == UINT64_MAX


@pytest.mark.parametrize("text", NON_CANONICAL)
def test_j4_non_canonical_representations_rejected(text: str) -> None:
    """J4: one integer must have exactly one spelling, or one record has two hashes."""
    with pytest.raises(IntegerRepresentationError):
        parse_int64(text)
    with pytest.raises(IntegerRepresentationError):
        parse_uint64(text)


@pytest.mark.parametrize(
    "value",
    [INT64_MIN - 1, INT64_MAX + 1, UINT64_MAX + 1],
)
def test_out_of_range_rejected(value: int) -> None:
    with pytest.raises(IntegerRepresentationError):
        parse_int64(value)


def test_negative_rejected_for_unsigned() -> None:
    with pytest.raises(IntegerRepresentationError):
        parse_uint64(-1)


@pytest.mark.parametrize("value", [1.0, 0.5, float(UTC_NS), True, False, None, [], {}])
def test_j6_no_float_or_other_type_transit(value: object) -> None:
    """J6: a float never reaches an int64/uint64 field, not even transiently.

    ``bool`` is included because it is an ``int`` subclass and would otherwise
    slip through as 0/1.
    """
    with pytest.raises(IntegerRepresentationError):
        parse_int64(value)


@given(st.integers(min_value=INT64_MIN, max_value=INT64_MAX))
def test_signed_round_trip_property(value: int) -> None:
    assert parse_int64(serialize_int(value)) == value


@given(st.integers(min_value=0, max_value=UINT64_MAX))
def test_unsigned_round_trip_property(value: int) -> None:
    assert parse_uint64(serialize_int(value)) == value


@given(st.integers(min_value=INT64_MIN, max_value=INT64_MAX))
def test_serialization_is_the_only_spelling(value: int) -> None:
    """Re-serializing a parsed value reproduces the same string, always."""
    text = serialize_int(value)
    assert serialize_int(parse_int64(text)) == text
