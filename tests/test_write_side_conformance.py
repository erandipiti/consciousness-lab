"""Write-side conformance (CL-002B-R2-R3).

Earlier rounds hardened the verifier. This file covers the other direction:
**no write path may put a package into a state the verifier would reject, or
make a durable claim that is not true at the moment it is written.**

That distinction matters because raw data is immutable. A verifier finding is
recoverable — you learn the package is bad. A bad write is not: the false claim
is now permanent acquisition data.
"""

import os
import time

import pytest

from consciousness_lab.session import recovery
from consciousness_lab.session.allocator import allocate_session
from consciousness_lab.session.finalizer import FinalizationError, finalize
from consciousness_lab.session.lifecycle import parse_records, summarize
from consciousness_lab.session.model import (
    ClockReading,
    LifecycleState,
    RawCaptureLevel,
    RecordingOutcome,
    Run,
    SampleLayout,
    StreamCloseStatus,
)
from consciousness_lab.session.writer import SealedPackageError, SessionWriter
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import ImmutableFileError, atomic_write_new
from consciousness_lab.storage.chunk_writer import ChunkWriteError
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.verifier import verify_package
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSpec,
    build_descriptor,
)
from tests.conftest import build_session

STREAM = "synthetic.eeg"
SPEC = SyntheticStreamSpec(STREAM, RawCaptureLevel.TRANSPORT_PAYLOAD)


def _live(
    data_root: DataRoot, chunks: int = 1
) -> tuple[PackagePaths, SessionWriter, SyntheticSource]:
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(
        Run(
            sealed_at=ClockReading(utc_ns=time.time_ns(), monotonic_ns=time.monotonic_ns()),
            required_streams=[STREAM],
        )
    )
    writer.open_stream(build_descriptor(SPEC))
    source = SyntheticSource(SPEC, seed=7)
    for _ in range(chunks):
        writer.commit_chunk(STREAM, source.next_chunk(2))
    return allocated.paths, writer, source


def _terminal(paths: PackagePaths) -> LifecycleState | None:
    return summarize(parse_records(paths.lifecycle.read_bytes())).terminal_state


# --- 1: closure is terminal for a stream ------------------------------------


def test_a_chunk_cannot_be_committed_after_the_stream_closed(
    data_root: DataRoot,
) -> None:
    """The close record is immutable, so the commit is refused, not the record.

    Otherwise a package could seal and verify COMPLETED while its durable
    closure fact — written before the chunk existed — was false.
    """
    _paths, writer, source = _live(data_root)
    writer.close_stream(STREAM, StreamCloseStatus.CLEAN)
    with pytest.raises(SealedPackageError, match="no more chunks"):
        writer.commit_chunk(STREAM, source.next_chunk(2))


def test_a_stream_cannot_be_reopened_after_closure(data_root: DataRoot) -> None:
    _paths, writer, _ = _live(data_root)
    writer.close_stream(STREAM, StreamCloseStatus.CLEAN)
    with pytest.raises(SealedPackageError):
        writer.open_stream(build_descriptor(SPEC))


# --- 2: the writer commits nothing the verifier would reject ----------------


def test_a_chunk_violating_packet_order_is_not_committed(data_root: DataRoot) -> None:
    """V06, enforced at write time against the physical bytes just written."""
    paths, writer, source = _live(data_root, chunks=0)
    pending = source.next_chunk(3)
    pending.packets[1]["packet_seq"], pending.packets[2]["packet_seq"] = (
        pending.packets[2]["packet_seq"],
        pending.packets[1]["packet_seq"],
    )
    with pytest.raises(ChunkWriteError, match="does not conform"):
        writer.commit_chunk(STREAM, pending)
    assert paths.stream(STREAM).chunks_index.read_bytes() == b"", "nothing was committed"


def test_a_chunk_with_a_missing_dense_sample_is_not_committed(
    data_root: DataRoot,
) -> None:
    """V07 dense key identity, at write time."""
    _paths, writer, source = _live(data_root, chunks=0)
    pending = source.next_chunk(2)
    pending.samples.pop()
    with pytest.raises(ChunkWriteError, match="does not conform"):
        writer.commit_chunk(STREAM, pending)


def test_a_chunk_whose_sample_references_no_packet_is_not_committed(
    data_root: DataRoot,
) -> None:
    _paths, writer, source = _live(data_root, chunks=0)
    pending = source.next_chunk(2)
    pending.samples[0]["packet_seq"] = 987_654
    with pytest.raises(ChunkWriteError, match="does not conform"):
        writer.commit_chunk(STREAM, pending)


def test_a_chunk_whose_payload_frames_do_not_match_is_not_committed(
    data_root: DataRoot,
) -> None:
    """V08, at write time — the bijection is checked before it becomes immutable."""
    _paths, writer, source = _live(data_root, chunks=0)
    pending = source.next_chunk(2)
    pending.payloads[0] = (424_242, pending.payloads[0][1])
    with pytest.raises(ChunkWriteError, match="does not conform"):
        writer.commit_chunk(STREAM, pending)


def test_a_chunk_that_does_not_follow_the_previous_one_is_not_committed(
    data_root: DataRoot,
) -> None:
    """Chain-level ordering: per-chunk validity does not imply chain validity."""
    _paths, writer, _source = _live(data_root, chunks=1)
    replay = SyntheticSource(SPEC, seed=7)  # restarts at packet_seq 0
    with pytest.raises(ChunkWriteError, match="does not follow"):
        writer.commit_chunk(STREAM, replay.next_chunk(2))


def test_a_sparse_chunk_with_a_duplicate_triple_is_not_committed(
    data_root: DataRoot,
) -> None:
    sparse = SyntheticStreamSpec(
        "synthetic.sparse", RawCaptureLevel.SYNTHETIC, layout=SampleLayout.SPARSE_LONG
    )
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(
        Run(
            sealed_at=ClockReading(utc_ns=time.time_ns(), monotonic_ns=time.monotonic_ns()),
            required_streams=["synthetic.sparse"],
        )
    )
    writer.open_stream(build_descriptor(sparse))
    pending = SyntheticSource(sparse, seed=7).next_chunk(2)
    pending.samples.append(dict(pending.samples[0]))
    with pytest.raises(ChunkWriteError, match="does not conform"):
        writer.commit_chunk("synthetic.sparse", pending)


def test_a_conforming_chunk_still_commits_normally(data_root: DataRoot) -> None:
    """The gate rejects only what the verifier would reject."""
    built = build_session(data_root, chunks=3, packets_per_chunk=4)
    assert verify_package(built.allocated.paths).is_completed


# --- 3 and 4: nothing terminal is claimed before it is checked --------------


def test_no_terminal_record_is_written_when_the_raw_tree_does_not_conform(
    data_root: DataRoot,
) -> None:
    """The immutable claim comes last, after everything it asserts is verified."""
    paths, writer, _source = _live(data_root, chunks=1)
    # Corrupt a committed artifact behind the writer's back.
    target = paths.stream(STREAM).root / "samples/000000.arrow"
    target.write_bytes(target.read_bytes()[:-8])
    writer.close_stream(STREAM, StreamCloseStatus.CLEAN)

    with pytest.raises(FinalizationError):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)
    assert _terminal(paths) is LifecycleState.RECORDING, "no false terminal record"
    assert not paths.manifest.exists()


@pytest.mark.parametrize("log", ["lifecycle", "events"])
def test_no_manifest_is_written_over_a_log_that_does_not_verify(
    data_root: DataRoot, log: str
) -> None:
    """Hashing an unverifiable log would seal a package that cannot verify.

    The tamper keeps the line parseable and canonical, so only the record's own
    ``record_sha256`` catches it — the pre-seal layer D34 exists for.
    """
    paths, writer, _ = _live(data_root, chunks=1)
    writer.close_stream(STREAM, StreamCloseStatus.CLEAN)
    target = paths.lifecycle if log == "lifecycle" else paths.events
    lines = target.read_bytes().split(b"\n")[:-1]
    obj = canonical_json.loads(lines[0])
    obj["actor" if log == "lifecycle" else "origin"] = "forged"
    lines[0] = canonical_json.canonicalize(obj)
    target.write_bytes(b"".join(line + b"\n" for line in lines))

    with pytest.raises(FinalizationError, match="record_sha256"):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)
    assert not paths.manifest.exists()
    assert not paths.manifest_sha256.exists()


# --- 5: a sealed or closed package accepts no writes ------------------------


def test_no_mutator_works_after_the_package_is_sealed(data_root: DataRoot) -> None:
    built = build_session(data_root)
    writer = built.writer
    assert verify_package(built.allocated.paths).is_completed

    with pytest.raises(SealedPackageError):
        writer.emit_event("CLOCK_SNAPSHOT", "clock_snapshot.v1", payload={})
    with pytest.raises(SealedPackageError):
        writer.commit_chunk(STREAM, SyntheticSource(SPEC, seed=1).next_chunk(1))
    with pytest.raises(SealedPackageError):
        writer.open_stream(build_descriptor(SyntheticStreamSpec("late", RawCaptureLevel.SYNTHETIC)))
    assert verify_package(built.allocated.paths).is_completed, "and it stays completed"


def test_no_mutator_works_after_a_terminal_lifecycle_record(
    data_root: DataRoot,
) -> None:
    paths, writer, _source = _live(data_root)
    writer.close_stream(STREAM, StreamCloseStatus.CLEAN)
    writer.fail_technical("simulated fatal write", stream_id=STREAM)
    assert _terminal(paths) is LifecycleState.CLOSED
    with pytest.raises(SealedPackageError, match="terminal"):
        writer.emit_event("CLOCK_SNAPSHOT", "clock_snapshot.v1", payload={})


# --- 6: publishing a write-once file leaves no trap -------------------------


def test_publishing_a_write_once_file_leaves_no_residue(tmp_path) -> None:  # type: ignore[no-untyped-def]
    atomic_write_new(tmp_path / "manifest.sha256", b"abc\n")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.sha256"]
    with pytest.raises(ImmutableFileError):
        atomic_write_new(tmp_path / "manifest.sha256", b"xyz\n")
    assert (tmp_path / "manifest.sha256").read_bytes() == b"abc\n"


def test_interrupted_publish_residue_is_recognised_and_resumable(
    data_root: DataRoot,
) -> None:
    """The state a crash between link and unlink leaves, on the fallback path.

    ``manifest.sha256`` and ``manifest.sha256.tmp`` are the same inode: the
    package looks sealed to condition 1 and is rejected by condition 6, with
    nothing else able to clear it.
    """
    built = build_session(data_root)
    paths = built.allocated.paths
    residue = paths.manifest_sha256.with_name(paths.manifest_sha256.name + ".tmp")
    os.link(paths.manifest_sha256, residue)

    assert not verify_package(paths).is_completed, "condition 6 rejects it"
    report = recovery.scan(paths)
    assert report.publish_residue == ["manifest.sha256.tmp"]
    assert report.can_resume_finalization

    recovery.resume_finalization(paths)
    assert not residue.exists()
    assert verify_package(paths).is_completed


def test_a_genuine_incomplete_write_is_never_cleared(data_root: DataRoot) -> None:
    """Only provably redundant residue is clearable; evidence is left alone."""
    built = build_session(data_root)
    paths = built.allocated.paths
    genuine = paths.raw / STREAM / "packets" / "000009.arrow.part"
    genuine.write_bytes(b"half a chunk")

    report = recovery.scan(paths)
    assert report.publish_residue == []
    assert "raw/synthetic.eeg/packets/000009.arrow.part" in report.incomplete_files
    assert genuine.is_file()


# --- 7: recovery does not trap a package it can never seal ------------------


def test_close_unclean_refuses_to_trap_an_unsealable_package(
    data_root: DataRoot,
) -> None:
    """CLOSED is a one-way door, so it is not walked through by accident.

    A crash after an artifact rename and before the chain append leaves an
    orphan. A package with an orphan can never be a valid v2 package, so a
    terminal record over it can never be sealed and can never be re-closed.
    """
    paths, _writer, _ = _live(data_root, chunks=1)
    (paths.raw / STREAM / "samples" / "000099.arrow").write_bytes(b"orphan")

    report = recovery.close_unclean(paths)
    assert report.sealing_blockers, "the blockers are named"
    assert any("orphan" in blocker for blocker in report.sealing_blockers)
    assert _terminal(paths) is LifecycleState.RECORDING, "no terminal record was written"


def test_close_unclean_can_be_forced_and_says_what_it_cost(
    data_root: DataRoot,
) -> None:
    """Recording the terminal fact anyway is sometimes right — as a decision."""
    paths, _writer, _ = _live(data_root, chunks=1)
    (paths.raw / STREAM / "samples" / "000099.arrow").write_bytes(b"orphan")

    report = recovery.close_unclean(paths, force=True)
    assert _terminal(paths) is LifecycleState.CLOSED
    assert report.sealing_blockers
    assert any("cannot be sealed" in note for note in report.notes)


def test_close_unclean_still_closes_an_ordinary_crash(data_root: DataRoot) -> None:
    """The common case is unaffected: a clean interruption closes and seals."""
    paths, _writer, _ = _live(data_root, chunks=1)
    report = recovery.close_unclean(paths)
    assert report.sealing_blockers == []
    assert _terminal(paths) is LifecycleState.CLOSED
    recovery.resume_finalization(paths)
    result = verify_package(paths)
    assert result.conditions[1], "sealed"
    assert result.sealed_outcome is RecordingOutcome.UNCLASSIFIED
    assert not result.is_completed


def test_an_uncommitted_artifact_left_by_a_rejected_chunk_is_an_orphan(
    data_root: DataRoot,
) -> None:
    """A refused commit leaves exactly what a crash at that point leaves.

    The artifacts are on disk with no commit record. Nothing is deleted — that
    would be the writer editing acquisition data — and nothing is adopted.
    """
    paths, writer, source = _live(data_root, chunks=0)
    pending = source.next_chunk(2)
    pending.samples.pop()
    with pytest.raises(ChunkWriteError):
        writer.commit_chunk(STREAM, pending)

    assert (paths.raw / STREAM / "packets" / "000000.arrow").is_file()
    assert paths.stream(STREAM).chunks_index.read_bytes() == b""
    assert recovery.scan(paths).orphan_files
