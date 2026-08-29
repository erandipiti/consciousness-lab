"""Reader and replay round-trip — category H (spec §16; D26)."""

import pytest

from consciousness_lab.session.model import RawCaptureLevel
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.reader import UnsupportedSchemaVersionError, open_package
from consciousness_lab.storage.verifier import verify_package
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSpec,
)
from tests.conftest import build_session


def test_write_verify_read_round_trip_preserves_everything(data_root: DataRoot) -> None:
    """Row counts, ordering, nulls and provenance all survive the round trip."""
    spec = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD)
    built = build_session(data_root, streams=[spec], chunks=3, packets_per_chunk=4, seed=11)
    assert verify_package(built.allocated.paths).is_completed

    reader = open_package(built.allocated.paths).stream("synthetic.eeg")
    packets = list(reader.packets())
    samples = list(reader.samples())
    observations = list(reader.observations())

    assert len(packets) == 12
    assert len(samples) == 12 * spec.samples_per_packet
    assert len(observations) == 12 * 2  # one device time and one counter per packet

    # Ordering is as-received, and packet_seq is never renumbered.
    assert [p["packet_seq"] for p in packets] == list(range(12))
    assert [(s["packet_seq"], s["sample_index_in_packet"]) for s in samples] == [
        (p, i) for p in range(12) for i in range(spec.samples_per_packet)
    ]


def test_replay_reproduces_the_source_exactly(data_root: DataRoot) -> None:
    """Regenerating from the same seed yields the same values that were stored."""
    spec = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD)
    built = build_session(data_root, streams=[spec], chunks=2, packets_per_chunk=3, seed=42)

    stored = list(open_package(built.allocated.paths).stream("synthetic.eeg").samples())
    regenerated: list[list[float]] = []
    source = SyntheticSource(spec, seed=42)
    for _ in range(2):
        for row in source.next_chunk(3).samples:
            regenerated.append(list(row["values"]))

    assert len(stored) == len(regenerated)
    for row, expected in zip(stored, regenerated, strict=True):
        assert [pytest.approx(v, abs=1e-6) for v in row["values"]] == expected


def test_observations_preserve_type_unit_clock_and_provenance(data_root: DataRoot) -> None:
    """The fields that make a device time interpretable years later."""
    built = build_session(data_root)
    observations = list(open_package(built.allocated.paths).stream("synthetic.eeg").observations())
    times = [o for o in observations if o["kind"] == "time"]
    counters = [o for o in observations if o["kind"] == "counter"]
    assert times and counters

    time_row = times[0]
    assert time_row["value_type"] == "float64"
    assert time_row["value_f64"] is not None
    assert time_row["value_i64"] is None and time_row["value_u64"] is None
    assert time_row["unit"] == "s"
    assert time_row["clock_id"] == "synthetic_device_clock"
    assert time_row["applies_to"] == "unknown", "honest for an unverified device"
    assert time_row["status"] == "assumed"

    counter_row = counters[0]
    assert counter_row["value_type"] == "uint64"
    assert counter_row["value_u64"] is not None
    assert counter_row["unit"] is None and counter_row["clock_id"] is None


def test_no_reconstructed_time_exists_anywhere_under_raw(data_root: DataRoot) -> None:
    """The `samples` table carries no timestamp column at all (spec §10.1)."""
    built = build_session(data_root)
    reader = open_package(built.allocated.paths).stream("synthetic.eeg")
    sample = next(iter(reader.samples()))
    assert set(sample) == {"packet_seq", "sample_index_in_packet", "values"}
    packet = next(iter(reader.packets()))
    forbidden = {"reconstructed_time", "sample_time", "timestamp", "device_timestamp"}
    assert not forbidden & set(packet)


def test_events_round_trip_in_order(data_root: DataRoot) -> None:
    built = build_session(data_root)
    events = open_package(built.allocated.paths).events()
    assert [e.event_seq for e in events] == list(range(len(events)))
    names = [e.event_name for e in events]
    assert "RECORDING_START" in names and "CLOCK_SNAPSHOT" in names
    # Every schema used travels with the package.
    schema_ids = {e.payload_schema for e in events}
    for schema_id in schema_ids:
        assert (built.allocated.paths.schemas / f"{schema_id}.json").is_file()


def test_sparse_long_layout_round_trips(data_root: DataRoot) -> None:
    from consciousness_lab.session.model import SampleLayout

    spec = SyntheticStreamSpec(
        "synthetic.marker",
        RawCaptureLevel.SYNTHETIC,
        n_channels=2,
        samples_per_packet=2,
        layout=SampleLayout.SPARSE_LONG,
    )
    built = build_session(data_root, streams=[spec], required=("synthetic.marker",), chunks=1)
    assert verify_package(built.allocated.paths).is_completed
    samples = list(open_package(built.allocated.paths).stream("synthetic.marker").samples())
    assert samples and set(samples[0]) == {
        "packet_seq",
        "sample_index_in_packet",
        "channel_id",
        "value",
    }


def test_unknown_major_schema_version_fails_closed(data_root: DataRoot) -> None:
    """A v2 reader opening a v1 package dispatches or refuses; it never guesses."""
    built = build_session(data_root)
    with pytest.raises(UnsupportedSchemaVersionError):
        open_package(built.allocated.paths, supported_major=2)


def test_descriptor_records_the_generator_and_stays_unverified(data_root: DataRoot) -> None:
    built = build_session(data_root)
    descriptor = open_package(built.allocated.paths).stream("synthetic.eeg").descriptor
    assert descriptor.hardware_verification.status.value in {"unknown", "assumed"}
    assert descriptor.actual_sample_rate_hz is None, "actual rate is derived, never recorded here"
