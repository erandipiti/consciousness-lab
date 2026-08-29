"""RFC 8785 canonicalization and the record hash procedure — J5, J7 (spec §12.2, D24)."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from consciousness_lab.storage import canonical_json


def test_key_order_is_insertion_independent() -> None:
    """J5: two constructions of the same record produce identical bytes and hash."""
    a = {"b": "2", "a": "1", "c": [1, 2]}
    b = {"c": [1, 2], "a": "1", "b": "2"}
    assert canonical_json.canonicalize(a) == canonical_json.canonicalize(b)
    assert canonical_json.record_hash(a) == canonical_json.record_hash(b)


def test_keys_sort_by_utf16_code_units_not_code_points() -> None:
    """The exact divergence that makes ``json.dumps(sort_keys=True)`` not JCS.

    U+1F600 encodes as the surrogate pair D83D DE00, so by UTF-16 code unit it
    sorts BEFORE U+FFFF (0xD83D < 0xFFFF). By code point the order is the
    opposite (0xFFFF < 0x1F600), which is what Python's ``sort_keys=True``
    would produce. JCS mandates the UTF-16 order.
    """
    emoji = "\U0001f600"
    ffff = "\uffff"
    encoded = canonical_json.canonicalize({ffff: 2, emoji: 1}).decode("utf-8")
    assert encoded.index(emoji) < encoded.index(ffff), "JCS orders by UTF-16 code unit"
    # Code-point order, which a naive implementation would use, is the reverse.
    assert sorted([emoji, ffff]) == [ffff, emoji]


def test_rfc8785_string_and_key_vector() -> None:
    """The RFC's worked example, restricted to the value types this profile allows."""
    record = {
        "\u20ac": "Euro Sign",
        "\r": "Carriage Return",
        "1": "One",
        "\u0080": "Control",
        "\u00f6": "Latin Small Letter O With Diaeresis",
        "": "Empty",
    }
    expected = (
        '{"":"Empty","\\r":"Carriage Return","1":"One",'
        '"\u0080":"Control","\u00f6":"Latin Small Letter O With Diaeresis",'
        '"\u20ac":"Euro Sign"}'
    ).encode("utf-8")
    assert canonical_json.canonicalize(record) == expected


def test_non_ascii_is_literal_not_escaped() -> None:
    assert canonical_json.canonicalize({"k": "\u00f1"}) == '{"k":"\u00f1"}'.encode("utf-8")


def test_control_characters_use_short_escapes() -> None:
    encoded = canonical_json.canonicalize({"k": "a\nb\tc\u0001"}).decode("utf-8")
    assert encoded == '{"k":"a\\nb\\tc\\u0001"}'


@pytest.mark.parametrize("value", [1.0, 0.5, float("nan"), float("inf"), -0.0])
def test_floats_are_refused(value: float) -> None:
    """No float exists anywhere in this design's JSON, so none is serialized."""
    with pytest.raises(canonical_json.CanonicalizationError):
        canonical_json.canonicalize({"v": value})


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-Infinity"])
def test_json_constants_are_refused_on_parse(text: str) -> None:
    with pytest.raises(canonical_json.CanonicalizationError):
        canonical_json.loads('{"v": ' + text + "}")


def test_record_hash_omits_its_own_key() -> None:
    record = {"a": "1"}
    expected = canonical_json.record_hash(record)
    with_hash = dict(record)
    with_hash[canonical_json.RECORD_HASH_KEY] = expected
    assert canonical_json.record_hash(with_hash) == expected
    assert canonical_json.verify_record(with_hash)


def test_changing_a_value_changes_the_hash() -> None:
    assert canonical_json.record_hash({"a": "1"}) != canonical_json.record_hash({"a": "2"})


def test_reformatted_bytes_cannot_defeat_verification() -> None:
    """Verification re-canonicalizes from the parsed object, so layout is irrelevant.

    Reordering keys and adding whitespace on disk either canonicalizes to the
    same bytes, meaning nothing changed, or fails the hash.
    """
    sealed = canonical_json.seal_record({"a": "1", "b": "2"})
    digest = canonical_json.loads(sealed)[canonical_json.RECORD_HASH_KEY]
    reformatted = ('{ "b" : "2",\n  "a" : "1",\n  "record_sha256" : "' + digest + '" }').encode(
        "utf-8"
    )
    assert canonical_json.verify_record(canonical_json.loads(reformatted))

    tampered = canonical_json.loads(sealed)
    tampered["a"] = "9"
    assert not canonical_json.verify_record(tampered)


def test_canonical_reserialization_is_stable() -> None:
    sealed = canonical_json.seal_record({"z": "1", "a": ["x", "y"]})
    assert canonical_json.canonicalize(canonical_json.loads(sealed)) == sealed


@given(
    st.dictionaries(
        st.text(min_size=1, max_size=6),
        st.one_of(st.text(max_size=6), st.integers(-1000, 1000), st.booleans(), st.none()),
        max_size=6,
    )
)
def test_round_trip_property(record: dict[str, object]) -> None:
    """Parse-then-canonicalize is a fixed point for any canonical document."""
    encoded = canonical_json.canonicalize(record)
    assert canonical_json.canonicalize(canonical_json.loads(encoded)) == encoded
