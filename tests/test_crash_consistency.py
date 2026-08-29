"""Deterministic fault injection at every writer stage (spec §12, §19).

Failures are injected through the writer's explicit stage hook, never by racing
threads: a test that depends on timing is a test that lies intermittently.

The invariant under test is the same at every stage: committed data stays
intact, incomplete data is identifiable, nothing is falsely complete, and no
orphan is silently adopted.
"""

import pytest

from consciousness_lab.session import recovery
from consciousness_lab.session.model import RawCaptureLevel, RecordingOutcome
from consciousness_lab.storage.chunk_writer import STAGES
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import Finding, read_chunk_index, verify_package
from consciousness_lab.synthetic.source import SyntheticStreamSpec
from tests.conftest import build_session


class InjectedFailureError(RuntimeError):
    """Stands in for the process dying at a chosen stage."""


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize(
    "level", [RawCaptureLevel.TRANSPORT_PAYLOAD, RawCaptureLevel.LIBRARY_DECODED]
)
def test_kill_at_every_chunk_stage(data_root: DataRoot, stage: str, level: RawCaptureLevel) -> None:
    """Interrupt the writer at each stage and assert the chunk index stays honest.

    A chunk is real iff its record is in ``chunks.jsonl``. Everything the writer
    put on disk before that append is an orphan, and the verifier must say so
    rather than treating a present file as committed data.
    """
    spec = SyntheticStreamSpec("synthetic.eeg", level)
    seen: list[str] = []

    def fault(current: str) -> None:
        seen.append(current)
        if current == stage:
            raise InjectedFailureError(current)

    with pytest.raises(InjectedFailureError):
        build_session(
            data_root,
            streams=[spec],
            chunks=1,
            finalize_outcome=None,
            fault=fault,
        )

    package = data_root.iter_packages()[0]
    commits, chain_error = read_chunk_index(package, "synthetic.eeg")
    assert chain_error is None, "a partially written index must never break the chain"

    if stage == "index_appended":
        assert len(commits) == 1, "the chunk was committed before the injected failure"
    else:
        assert commits == [], "nothing before the index append may count as committed"

    report = recovery.scan(package)
    assert report.state is not recovery.StructuralState.SEALED
    assert not verify_package(package).is_completed


@pytest.mark.parametrize("stage", ["packets_written", "samples_written", "sidecar_written"])
def test_orphan_files_are_reported_never_adopted(data_root: DataRoot, stage: str) -> None:
    """Files a crash left behind are surfaced, and they are not silently adopted."""

    def fault(current: str) -> None:
        if current == stage:
            raise InjectedFailureError(current)

    with pytest.raises(InjectedFailureError):
        build_session(data_root, chunks=1, finalize_outcome=None, fault=fault)

    package = data_root.iter_packages()[0]
    result = verify_package(package)
    assert Finding.ORPHAN_FILE in result.findings()
    report = recovery.scan(package)
    assert report.orphan_files, "recovery must report what it found"
    assert not result.is_completed


def test_crash_between_sidecar_and_index_leaves_an_uncommitted_chunk(
    data_root: DataRoot,
) -> None:
    """A sidecar alone does not commit a chunk (RC-R2-1 / spec §12.2)."""

    def fault(current: str) -> None:
        if current == "sidecar_written":
            raise InjectedFailureError(current)

    with pytest.raises(InjectedFailureError):
        build_session(data_root, chunks=1, finalize_outcome=None, fault=fault)

    package = data_root.iter_packages()[0]
    sidecars = list((package.raw / "synthetic.eeg").glob("*.commit.json"))
    assert sidecars, "the sidecar did land"
    commits, _ = read_chunk_index(package, "synthetic.eeg")
    assert commits == [], "but the chunk is not committed"


def test_committed_chunks_survive_a_later_crash(data_root: DataRoot) -> None:
    """Data committed before the failure is still readable and still verifies."""
    calls = {"n": 0}

    def fault(current: str) -> None:
        if current == "index_appended":
            calls["n"] += 1
            if calls["n"] == 2:
                raise InjectedFailureError(current)

    with pytest.raises(InjectedFailureError):
        build_session(data_root, chunks=3, finalize_outcome=None, fault=fault)

    package = data_root.iter_packages()[0]
    commits, chain_error = read_chunk_index(package, "synthetic.eeg")
    assert chain_error is None
    assert len(commits) == 2, "the first two chunks were committed and remain so"


def test_truncating_a_committed_chunk_is_detected(data_root: DataRoot) -> None:
    built = build_session(data_root)
    target = next((built.allocated.paths.raw / "synthetic.eeg" / "samples").iterdir())
    target.write_bytes(target.read_bytes()[:-1])
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.CHUNK_ARTIFACT_HASH_MISMATCH in result.findings()


def test_truncating_the_chunk_index_breaks_the_chain_detectably(data_root: DataRoot) -> None:
    built = build_session(data_root, chunks=3)
    index = built.allocated.paths.stream("synthetic.eeg").chunks_index
    lines = index.read_bytes().split(b"\n")
    index.write_bytes(b"\n".join(lines[1:]))  # drop the first record, keep the rest
    _, chain_error = read_chunk_index(built.allocated.paths, "synthetic.eeg")
    assert chain_error is not None
    assert not verify_package(built.allocated.paths).is_completed


def test_leftover_part_file_blocks_completion(data_root: DataRoot) -> None:
    """Condition 6: an incomplete write marker anywhere means not complete."""
    built = build_session(data_root)
    (built.allocated.paths.raw / "synthetic.eeg" / "samples" / "999999.arrow.part").write_bytes(
        b"partial"
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.INCOMPLETE_FILE_PRESENT in result.findings()


def test_interrupted_finalization_leaves_an_unsealed_package(data_root: DataRoot) -> None:
    """No manifest pair means not finalized; recovery says so and repairs nothing."""
    built = build_session(data_root)
    built.allocated.paths.manifest.unlink()
    built.allocated.paths.manifest_sha256.unlink()
    result = verify_package(built.allocated.paths)
    assert Finding.MISSING_MANIFEST in result.findings()
    assert not result.is_completed


def test_recovery_closes_a_crashed_session_without_guessing(data_root: DataRoot) -> None:
    """A crash closes RECOVERED_UNCLEAN / UNCLASSIFIED, never a guessed outcome."""
    built = build_session(data_root, finalize_outcome=None)
    package = built.allocated.paths
    report = recovery.close_unclean(package)
    assert report.can_close_unclean

    from consciousness_lab.session.lifecycle import read_records, summarize
    from consciousness_lab.session.model import ClosureCondition

    summary = summarize(read_records(package.lifecycle))
    assert summary.closure_condition is ClosureCondition.RECOVERED_UNCLEAN
    assert summary.sealed_outcome is RecordingOutcome.UNCLASSIFIED
    assert not verify_package(package).is_completed
