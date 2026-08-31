"""Finalization order, crash windows and resumable sealing (v2 §9.4).

The ordering is the contract, and each crash boundary has a defined outcome. The
one that matters most is the last: a durable terminal lifecycle record with no
manifest pair is ``INTERRUPTED_FINALIZATION`` — resumable, and **never** a
promotion. Resuming an ABORTED package seals ABORTED.
"""

import pytest

from consciousness_lab.session import recovery
from consciousness_lab.session.finalizer import finalize
from consciousness_lab.session.lifecycle import parse_records, summarize
from consciousness_lab.session.model import (
    ClosureCondition,
    LifecycleState,
    RecordingOutcome,
    StreamCloseStatus,
)
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.verifier import verify_package
from tests.conftest import build_session

STREAM = "synthetic.eeg"


def _terminal(
    paths: PackagePaths,
) -> tuple[LifecycleState | None, ClosureCondition | None, RecordingOutcome | None]:
    summary = summarize(parse_records(paths.lifecycle.read_bytes()))
    return summary.terminal_state, summary.closure_condition, summary.sealed_outcome


# --- crash windows (§9.4.1) --------------------------------------------------


def test_crash_before_finalizing_recovers_unclean(data_root: DataRoot) -> None:
    """Lifecycle ends at RECORDING; the intended outcome is never guessed."""
    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    assert _terminal(paths)[0] is LifecycleState.RECORDING

    report = recovery.scan(paths)
    assert report.state is recovery.StructuralState.INTERRUPTED_RECORDING
    assert report.can_close_unclean and not report.can_resume_finalization

    recovery.close_unclean(paths)
    state, closure, outcome = _terminal(paths)
    assert state is LifecycleState.CLOSED
    assert closure is ClosureCondition.RECOVERED_UNCLEAN
    assert outcome is RecordingOutcome.UNCLASSIFIED


def test_crash_after_allocation_recovers_unclean(data_root: DataRoot) -> None:
    from consciousness_lab.session.allocator import allocate_session

    allocated = allocate_session(data_root, participant_pseudonym="P002")
    report = recovery.scan(allocated.paths)
    assert report.state is recovery.StructuralState.ALLOCATED_ONLY
    recovery.close_unclean(allocated.paths)
    assert _terminal(allocated.paths)[2] is RecordingOutcome.UNCLASSIFIED


def test_crash_during_finalizing_before_closed_recovers_unclean(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    built.writer.close_stream(STREAM, StreamCloseStatus.CLEAN)
    built.writer.lifecycle.append(LifecycleState.FINALIZING)
    assert _terminal(paths)[0] is LifecycleState.FINALIZING

    report = recovery.scan(paths)
    assert report.state is recovery.StructuralState.INTERRUPTED_FINALIZATION
    assert report.can_close_unclean, "no terminal record yet, so it is closed, not resumed"
    assert not report.can_resume_finalization

    recovery.close_unclean(paths)
    assert _terminal(paths)[2] is RecordingOutcome.UNCLASSIFIED


def test_crash_after_closed_before_the_manifest_is_interrupted_finalization(
    data_root: DataRoot,
) -> None:
    """NOT unreadable, and NOT automatically RECOVERED_UNCLEAN (§9.4.2)."""
    built = build_session(data_root)
    paths = built.allocated.paths
    paths.manifest.unlink()
    paths.manifest_sha256.unlink()

    report = recovery.scan(paths)
    assert report.state is recovery.StructuralState.INTERRUPTED_FINALIZATION
    assert report.can_resume_finalization
    assert not report.can_close_unclean, "CLOSED is terminal; it is resumed, never re-closed"
    assert _terminal(paths)[2] is RecordingOutcome.COMPLETED, "the durable fact is untouched"


# --- resumable sealing (§9.4.2) ---------------------------------------------


def test_resuming_seals_the_outcome_that_was_already_decided(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    before = paths.lifecycle.read_bytes()
    paths.manifest.unlink()
    paths.manifest_sha256.unlink()

    recovery.resume_finalization(paths)
    assert paths.lifecycle.read_bytes() == before, "lifecycle history is never rewritten"
    assert verify_package(paths).is_completed


@pytest.mark.parametrize(
    "outcome",
    [RecordingOutcome.ABORTED, RecordingOutcome.TECHNICAL_FAILURE],
)
def test_resuming_never_promotes_an_outcome(data_root: DataRoot, outcome: RecordingOutcome) -> None:
    """This completes a sealing transaction; it does not decide anything."""
    built = build_session(data_root, finalize_outcome=outcome)
    paths = built.allocated.paths
    paths.manifest.unlink()
    paths.manifest_sha256.unlink()

    recovery.resume_finalization(paths)
    result = verify_package(paths)
    assert result.conditions[1], "the package is sealed"
    assert result.sealed_outcome is outcome
    assert not result.is_completed, "and it is not completed, because it never was"


def test_resuming_an_unclassified_package_seals_unclassified(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    recovery.close_unclean(paths)
    recovery.resume_finalization(paths)
    result = verify_package(paths)
    assert result.conditions[1]
    assert result.sealed_outcome is RecordingOutcome.UNCLASSIFIED
    assert not result.is_completed


def test_resuming_refuses_when_the_durable_state_is_insufficient(
    data_root: DataRoot,
) -> None:
    """Contradictory or insufficient state is BLOCKED, never guessed."""
    built = build_session(data_root)
    paths = built.allocated.paths
    paths.manifest.unlink()
    paths.manifest_sha256.unlink()
    paths.stream(STREAM).stream_close.unlink()

    with pytest.raises(recovery.ResumeError, match="stream_close"):
        recovery.resume_finalization(paths)
    assert not paths.manifest.exists(), "nothing was written"


def test_resuming_refuses_when_a_pre_seal_record_hash_fails(
    data_root: DataRoot,
) -> None:
    """The pre-seal integrity layer is exactly what resumption depends on (D34)."""
    built = build_session(data_root)
    paths = built.allocated.paths
    paths.manifest.unlink()
    paths.manifest_sha256.unlink()
    raw = paths.lifecycle.read_bytes()
    paths.lifecycle.write_bytes(raw.replace(b'"actor":"system"', b'"actor":"forged"', 1))

    with pytest.raises(recovery.ResumeError, match="record_sha256"):
        recovery.resume_finalization(paths)
    assert not paths.manifest.exists()


def test_resuming_refuses_an_unexpected_file(data_root: DataRoot) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    paths.manifest.unlink()
    paths.manifest_sha256.unlink()
    (paths.root / "mystery.bin").write_bytes(b"unexpected")

    with pytest.raises(recovery.ResumeError, match="not closed"):
        recovery.resume_finalization(paths)


def test_resuming_refuses_a_sealed_package(data_root: DataRoot) -> None:
    built = build_session(data_root)
    with pytest.raises(recovery.ResumeError, match="sealed"):
        recovery.resume_finalization(built.allocated.paths)


def test_recovery_never_creates_completed(data_root: DataRoot) -> None:
    """No recovery path produces a completed package that was not one already."""
    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    recovery.close_unclean(paths)
    recovery.resume_finalization(paths)
    assert not verify_package(paths).is_completed
    assert _terminal(paths)[2] is not RecordingOutcome.COMPLETED


# --- ordering ---------------------------------------------------------------


def test_the_manifest_is_the_last_durable_act(data_root: DataRoot) -> None:
    """Step 11 is the finalization marker: everything else is already on disk."""
    built = build_session(data_root)
    paths = built.allocated.paths
    manifest_time = paths.manifest_sha256.stat().st_mtime_ns
    for other in (
        paths.lifecycle,
        paths.events,
        paths.annotations_head,
        paths.stream(STREAM).stream_close,
        paths.stream(STREAM).chunks_index,
    ):
        assert other.stat().st_mtime_ns <= manifest_time


def test_the_manifest_pair_is_a_finalization_marker_not_a_completion_marker(
    data_root: DataRoot,
) -> None:
    """A cleanly aborted session produces an identically valid pair (D21)."""
    completed = build_session(data_root, finalize_outcome=RecordingOutcome.COMPLETED)
    aborted = build_session(data_root, finalize_outcome=RecordingOutcome.ABORTED)
    for built in (completed, aborted):
        assert verify_package(built.allocated.paths).conditions[1]
    assert verify_package(completed.allocated.paths).is_completed
    assert not verify_package(aborted.allocated.paths).is_completed


def test_the_predicate_has_exactly_eight_conditions(data_root: DataRoot) -> None:
    built = build_session(data_root)
    result = verify_package(built.allocated.paths)
    assert sorted(result.conditions) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert result.is_completed


def test_finalizing_with_an_open_write_marker_is_refused(data_root: DataRoot) -> None:
    built = build_session(data_root, finalize_outcome=None)
    (built.allocated.paths.raw / STREAM / "packets" / "000009.arrow.part").write_bytes(b"x")
    from consciousness_lab.session.finalizer import FinalizationError

    with pytest.raises(FinalizationError, match="incomplete write marker"):
        finalize(built.writer, outcome=RecordingOutcome.COMPLETED)
