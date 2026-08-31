"""Arrow schemas for raw chunks (v2 §10.1; D11, D31).

Three tables per stream, deliberately not one:

* ``packets``      — one row per received packet, host arrival only
* ``samples``      — one row per sample, no timestamps at all
* ``observations`` — device-provided times and counters, as rows

Arrow keeps native exact ``int64``/``uint64`` here. The decimal-string rule of
§12.2.1 governs JSON serialization only; storing these as strings would be a
real loss for no gain.
"""

import pyarrow as pa

from consciousness_lab.session.model import SampleLayout

#: **There is no ``payload_ref`` in v2** (§5). A frame carries its own
#: ``packet_seq``, so parsing ``payloads/NNNNNN.bin`` yields the packet -> bytes
#: mapping deterministically; persisting a pointer to it was a second copy of
#: what parsing produces. The consequence is that this schema is now identical
#: at every capture level, which removed the "nullable iff transport_payload"
#: relation with it.
PACKETS_SCHEMA = pa.schema(
    [
        pa.field("packet_seq", pa.int64(), nullable=False),
        pa.field("host_arrival_monotonic_ns", pa.int64(), nullable=False),
        pa.field("host_arrival_utc_ns", pa.int64(), nullable=False),
        pa.field("host_arrival_monotonic_clock_id", pa.string(), nullable=False),
        pa.field("host_arrival_utc_clock_id", pa.string(), nullable=False),
        pa.field("n_samples", pa.int32(), nullable=False),
        pa.field("decode_status", pa.string(), nullable=False),
    ]
)

#: Exactly one of the four value columns is non-null, selected by ``value_type``.
#: A single int64 column was rejected: library timestamps are frequently floats
#: and device counters are frequently unsigned, so coercion would round or wrap
#: values at acquisition time, into immutable data.
OBSERVATIONS_SCHEMA = pa.schema(
    [
        pa.field("packet_seq", pa.int64(), nullable=False),
        pa.field("sample_index_in_packet", pa.int32(), nullable=True),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("name", pa.string(), nullable=False),
        pa.field("value_type", pa.string(), nullable=False),
        pa.field("value_i64", pa.int64(), nullable=True),
        pa.field("value_u64", pa.uint64(), nullable=True),
        pa.field("value_f64", pa.float64(), nullable=True),
        pa.field("value_str", pa.string(), nullable=True),
        pa.field("unit", pa.string(), nullable=True),
        pa.field("clock_id", pa.string(), nullable=True),
        pa.field("applies_to", pa.string(), nullable=False),
        pa.field("provenance", pa.string(), nullable=False),
        pa.field("status", pa.string(), nullable=False),
    ]
)


def samples_schema(layout: SampleLayout, n_channels: int) -> pa.Schema:
    """Sample schema for a stream, per its declared layout.

    ``dense_fixed_list`` keys the values to the descriptor's ordered channel
    list, so channel identity lives in the descriptor and never in a column
    name. There are no timestamps in ``samples``: per-sample time is derived.
    """
    if layout is SampleLayout.DENSE_FIXED_LIST:
        return pa.schema(
            [
                pa.field("packet_seq", pa.int64(), nullable=False),
                pa.field("sample_index_in_packet", pa.int32(), nullable=False),
                pa.field(
                    "values",
                    pa.list_(pa.field("item", pa.float32(), nullable=False), n_channels),
                    nullable=False,
                ),
            ]
        )
    return pa.schema(
        [
            pa.field("packet_seq", pa.int64(), nullable=False),
            pa.field("sample_index_in_packet", pa.int32(), nullable=False),
            pa.field("channel_id", pa.string(), nullable=False),
            pa.field("value", pa.float64(), nullable=False),
        ]
    )


def schema_for_kind(kind: str, layout: SampleLayout, n_channels: int) -> pa.Schema:
    """The v2 contract schema for one artifact kind.

    Used by the writer and by read-time verification, so there is one definition
    of "the right schema" rather than two that can drift (D31).
    """
    if kind == "packets":
        return PACKETS_SCHEMA
    if kind == "observations":
        return OBSERVATIONS_SCHEMA
    if kind == "samples":
        return samples_schema(layout, n_channels)
    raise ValueError(f"{kind!r} has no Arrow schema")


def schema_conformance_error(actual: pa.Schema, expected: pa.Schema) -> str | None:
    """Why ``actual`` is not ``expected``, or ``None`` when it conforms.

    Names, order, types and nullability are all contractual; Arrow **schema
    metadata** is not, and is ignored here rather than compared (v2 §10.1).
    A matching SHA proves identity, not conformance: it says the bytes are the
    bytes that were sealed, never that they are a valid v2 artifact.
    """
    actual_names = actual.names
    expected_names = expected.names
    if actual_names != expected_names:
        return f"fields are {actual_names}, expected {expected_names}"
    for index, field in enumerate(expected):
        found = actual.field(index)
        if not found.type.equals(field.type):
            return f"field {field.name!r} is {found.type}, expected {field.type}"
        if found.nullable != field.nullable:
            return (
                f"field {field.name!r} is "
                f"{'nullable' if found.nullable else 'non-nullable'}, expected the opposite"
            )
    return None
