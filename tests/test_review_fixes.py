"""Regressions for every BLOCKING/SERIOUS/MODERATE finding of the CL-002B review.

One test per finding, named after what it prevents rather than after the
finding number, so the reason survives longer than the review does.
"""

import sqlite3

import pytest
from pydantic import ValidationError

from consciousness_lab.session.model import (
    ClockReading,
    HardwareVerification,
    Origin,
    RawCaptureLevel,
    RecordingOutcome,
    Run,
    SoftwareInfo,
    VerificationStatus,
    load_on_disk,
)
from consciousness_lab.session.writer import SealedPackageError, SessionWriter
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import ImmutableFileError, atomic_write_new
from consciousness_lab.storage.chunk_writer import ChunkWriteError, ChunkWriter, PendingChunk
from consciousness_lab.storage.observations import ObservationError, validate_observation
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.reader import UnverifiedPackageError, open_package
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSpec,
    build_descriptor,
)
from tests.conftest import build_session, now_reading

# --- BLOCKING: immutable package content cannot be overwritten ----------------


def test_a_sealed_package_cannot_be_reopened_for_writing(data_root: DataRoot) -> None:
    built = build_session(data_root)
    with pytest.raises(SealedPackageError):
        SessionWriter.open(built.allocated.paths)


def test_run_json_cannot_be_rewritten(data_root: DataRoot) -> None:
    built = build_session(data_root, finalize_outcome=None)
    with pytest.raises(SealedPackageError):
        built.writer.start_recording(Run(sealed_at=now_reading(), required_streams=["x"]))


def test_a_stream_descriptor_cannot_be_rewritten(data_root: DataRoot) -> None:
    built = build_session(data_root, finalize_outcome=None)
    spec = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD)
    with pytest.raises(SealedPackageError):
        built.writer.open_stream(build_descriptor(spec))


def test_a_fresh_chunk_writer_refuses_an_occupied_stream(data_root: DataRoot) -> None:
    """A new writer restarts at chunk 0 and would overwrite immutable raw files."""
    built = build_session(data_root, finalize_outcome=None)
    spec = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD)
    stream_paths = built.allocated.paths.stream("synthetic.eeg")
    with pytest.raises(ChunkWriteError, match="immutable"):
        ChunkWriter(stream_paths, build_descriptor(spec), "deadbeef")


def test_atomic_write_new_refuses_to_clobber(data_root: DataRoot) -> None:
    target = data_root.root / "once.json"
    atomic_write_new(target, b"first")
    with pytest.raises(ImmutableFileError):
        atomic_write_new(target, b"second")
    assert target.read_bytes() == b"first"


# --- SERIOUS: verification must not write inside the package ------------------


def test_verification_leaves_no_trace_in_the_package(data_root: DataRoot) -> None:
    from consciousness_lab.storage.verifier import verify_package

    built = build_session(data_root)
    before = sorted(
        p.relative_to(built.allocated.paths.root).as_posix()
        for p in built.allocated.paths.root.rglob("*")
    )
    assert verify_package(built.allocated.paths).is_completed
    after = sorted(
        p.relative_to(built.allocated.paths.root).as_posix()
        for p in built.allocated.paths.root.rglob("*")
    )
    assert before == after, "verification must not add a file to a sealed package"


# --- SERIOUS: int64/uint64 on disk must be decimal strings --------------------


def test_a_json_number_is_refused_for_an_int64_field_on_disk() -> None:
    """A JSON Number has already passed through a double by the time we see it."""
    with pytest.raises(ValidationError):
        load_on_disk(ClockReading, {"utc_ns": 1787923530123456789, "monotonic_ns": "1"})


def test_the_same_value_as_a_decimal_string_is_accepted() -> None:
    reading = load_on_disk(ClockReading, {"utc_ns": "1787923530123456789", "monotonic_ns": "1"})
    assert reading.utc_ns == 1787923530123456789


def test_in_process_construction_still_accepts_python_ints() -> None:
    """The value never touched JSON, so it never lost precision."""
    assert ClockReading(utc_ns=1787923530123456789, monotonic_ns=1).utc_ns == 1787923530123456789


def test_every_on_disk_int64_is_written_as_a_string(data_root: DataRoot) -> None:
    built = build_session(data_root)
    raw = canonical_json.loads(built.allocated.paths.allocation.read_bytes())
    assert isinstance(raw["allocated_at"]["utc_ns"], str)
    manifest = canonical_json.loads(built.allocated.paths.manifest.read_bytes())
    assert isinstance(manifest["lifecycle_seal"]["sealed_len"], str)
    assert isinstance(manifest["inventory"][0]["bytes"], str)


# --- SERIOUS: observation rows -----------------------------------------------


def _observation(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "packet_seq": 0,
        "sample_index_in_packet": None,
        "kind": "time",
        "name": "x.t",
        "value_type": "float64",
        "value_i64": None,
        "value_u64": None,
        "value_f64": 1.0,
        "value_str": None,
        "unit": None,
        "clock_id": None,
        "applies_to": "unknown",
        "provenance": "device_provided",
        "status": "assumed",
    }
    row.update(overrides)
    return row


def test_a_valid_observation_passes() -> None:
    validate_observation(_observation())


def test_two_value_columns_set_is_rejected() -> None:
    with pytest.raises(ObservationError):
        validate_observation(_observation(value_u64=3))


def test_no_value_column_set_is_rejected() -> None:
    with pytest.raises(ObservationError):
        validate_observation(_observation(value_f64=None))


def test_value_column_must_match_value_type() -> None:
    with pytest.raises(ObservationError):
        validate_observation(_observation(value_type="uint64"))


@pytest.mark.parametrize(
    "bad", [{"kind": "guess"}, {"applies_to": "later"}, {"provenance": "vibes"}, {"status": "sure"}]
)
def test_invalid_enums_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ObservationError):
        validate_observation(_observation(**bad))


def test_the_chunk_writer_refuses_an_invalid_observation(data_root: DataRoot) -> None:
    spec = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.SYNTHETIC)
    built = build_session(data_root, streams=[spec], chunks=1, finalize_outcome=None)
    source = SyntheticSource(spec, seed=1)
    pending: PendingChunk = source.next_chunk(1)
    pending.observations[0]["value_u64"] = 5  # now two value columns are set
    with pytest.raises(ObservationError):
        built.writer.commit_chunk("synthetic.eeg", pending)


# --- SERIOUS: hardware verification shape ------------------------------------


def test_hardware_verification_uses_the_approved_shape() -> None:
    parsed = load_on_disk(HardwareVerification, {"status": "unverified", "ref": "docs/HARDWARE.md"})
    assert parsed.status is VerificationStatus.UNVERIFIED
    assert HardwareVerification().status is VerificationStatus.UNVERIFIED


# --- MODERATE: reader verifies; origins carry provenance; unknown git is unknown


def test_open_package_refuses_a_corrupt_package(data_root: DataRoot) -> None:
    built = build_session(data_root)
    target = next((built.allocated.paths.raw / "synthetic.eeg" / "samples").iterdir())
    target.write_bytes(b"corrupted")
    with pytest.raises(UnverifiedPackageError):
        open_package(built.allocated.paths)
    # Inspection tooling may still look, deliberately.
    assert open_package(built.allocated.paths, verify=False) is not None


def test_open_package_still_reads_a_cleanly_aborted_session(data_root: DataRoot) -> None:
    """Integrity gates reading; the outcome does not."""
    built = build_session(data_root, finalize_outcome=RecordingOutcome.ABORTED)
    assert open_package(built.allocated.paths) is not None


def test_replay_origin_must_declare_its_source() -> None:
    with pytest.raises(ValidationError):
        Origin(kind="replay")
    assert (
        Origin(
            kind="replay",
            source_session_id="s",
            source_manifest_sha256="h",
            replay_tool_version="v",
        ).kind
        == "replay"
    )


def test_synthetic_origin_must_record_its_seed() -> None:
    with pytest.raises(ValidationError):
        Origin(kind="synthetic")
    assert Origin(kind="synthetic", generator_seed=7).generator_seed == 7


def test_unknown_git_provenance_is_unknown_not_clean(data_root: DataRoot) -> None:
    """``dirty=False`` would assert a clean tree we never observed."""
    from consciousness_lab.session.allocator import allocate_session

    allocated = allocate_session(data_root, participant_pseudonym="P001", repo_root=None)
    assert allocated.allocation.software.dirty is None
    assert SoftwareInfo().dirty is None


# --- MINOR: event payload validation -----------------------------------------


def test_an_event_payload_with_undeclared_keys_is_refused(data_root: DataRoot) -> None:
    built = build_session(data_root, finalize_outcome=None)
    with pytest.raises(ValueError, match="undeclared keys"):
        built.writer.emit_event(
            "TECHNICAL_ERROR", "technical_error.v1", payload={"message": "x", "typo": 1}
        )


def test_an_event_payload_missing_a_required_key_is_refused(data_root: DataRoot) -> None:
    built = build_session(data_root, finalize_outcome=None)
    with pytest.raises(ValueError, match="missing required keys"):
        built.writer.emit_event("TECHNICAL_ERROR", "technical_error.v1", payload={})


# --- minor-version tolerance (spec §17) --------------------------------------


def test_an_unknown_optional_field_in_a_minor_version_is_tolerated() -> None:
    parsed = load_on_disk(
        ClockReading,
        {"utc_ns": "1", "monotonic_ns": "1", "future_optional_field": "added in v1.1"},
    )
    assert parsed.utc_ns == 1


def test_registry_failure_does_not_break_allocation(data_root: DataRoot) -> None:
    """Ordering: the derived index is written last and is allowed to fail."""
    from consciousness_lab.session.allocator import allocate_session

    data_root.root.mkdir(parents=True, exist_ok=True)
    data_root.registry.mkdir()
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    assert allocated.paths.allocation.is_file()
    with pytest.raises(sqlite3.Error):
        sqlite3.connect(data_root.registry).execute("SELECT 1")
