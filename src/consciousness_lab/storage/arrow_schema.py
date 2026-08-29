"""Arrow schemas for raw chunks (spec §9.1, §9.3; D11).

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

#: ``payload_ref`` is null for every stream whose capture level is not
#: ``transport_payload``. A fabricated pointer is never written.
PAYLOAD_REF_TYPE = pa.struct(
    [
        pa.field("file", pa.string(), nullable=False),
        pa.field("offset", pa.uint64(), nullable=False),
        pa.field("length", pa.uint64(), nullable=False),
    ]
)

PACKETS_SCHEMA = pa.schema(
    [
        pa.field("packet_seq", pa.int64(), nullable=False),
        pa.field("host_arrival_monotonic_ns", pa.int64(), nullable=False),
        pa.field("host_arrival_utc_ns", pa.int64(), nullable=False),
        pa.field("host_arrival_monotonic_clock_id", pa.string(), nullable=False),
        pa.field("host_arrival_utc_clock_id", pa.string(), nullable=False),
        pa.field("n_samples", pa.int32(), nullable=False),
        pa.field("payload_ref", PAYLOAD_REF_TYPE, nullable=True),
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
