"""Recovery scanner: reports, never repairs (spec §19)."""

from consciousness_lab.session import recovery
from consciousness_lab.session.model import RecordingOutcome
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import verify_package
from tests.conftest import build_session


def test_sealed_package_is_classified_sealed(data_root: DataRoot) -> None:
    built = build_session(data_root)
    report = recovery.scan(built.allocated.paths)
    assert report.state is recovery.StructuralState.SEALED
    assert not report.can_close_unclean


def test_interrupted_recording_is_classified(data_root: DataRoot) -> None:
    built = build_session(data_root, finalize_outcome=None)
    report = recovery.scan(built.allocated.paths)
    assert report.state is recovery.StructuralState.INTERRUPTED_RECORDING
    assert report.can_close_unclean


def test_a_directory_without_allocation_is_not_a_package(data_root: DataRoot) -> None:
    stray = data_root.sessions / "not-a-session"
    stray.mkdir(parents=True)
    report = recovery.scan(recovery.PackagePaths(stray))
    assert report.state is recovery.StructuralState.UNREADABLE
    assert not report.can_close_unclean


def test_recovery_never_promotes_to_completed(data_root: DataRoot) -> None:
    built = build_session(data_root, finalize_outcome=None)
    recovery.close_unclean(built.allocated.paths)
    assert not verify_package(built.allocated.paths).is_completed


def test_recovery_does_not_delete_or_adopt_anything(data_root: DataRoot) -> None:
    """Orphans are listed. The files stay exactly where they are."""
    built = build_session(data_root, finalize_outcome=None)
    stream_dir = built.allocated.paths.raw / "synthetic.eeg"
    orphan = stream_dir / "samples" / "999999.arrow"
    orphan.write_bytes(b"orphan")
    before = sorted(p.name for p in (stream_dir / "samples").iterdir())

    report = recovery.scan(built.allocated.paths)
    assert any("999999" in name for name in report.orphan_files)
    after = sorted(p.name for p in (stream_dir / "samples").iterdir())
    assert before == after, "recovery must not delete anything"

    from consciousness_lab.storage.verifier import read_chunk_index

    commits, _ = read_chunk_index(built.allocated.paths, "synthetic.eeg")
    assert all("999999" not in c.artifact_path("samples") for c in commits), "and must not adopt it"


def test_known_technical_failure_is_recorded_as_such(data_root: DataRoot) -> None:
    """A diagnosed fault closes CLEAN/TECHNICAL_FAILURE, not UNCLASSIFIED (D16)."""
    built = build_session(
        data_root,
        finalize_outcome=RecordingOutcome.TECHNICAL_FAILURE,
    )
    from consciousness_lab.session.lifecycle import read_records, summarize
    from consciousness_lab.session.model import ClosureCondition

    summary = summarize(read_records(built.allocated.paths.lifecycle))
    assert summary.sealed_outcome is RecordingOutcome.TECHNICAL_FAILURE
    assert summary.closure_condition is ClosureCondition.CLEAN
