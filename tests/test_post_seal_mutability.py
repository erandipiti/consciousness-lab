"""Post-seal mutability — M1-M4 (v2 §7, §8; D23, D30)."""

import pytest

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session.model import AnnotationHead, RecordingOutcome
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import Finding, verify_package
from tests.conftest import build_session

#: Every file the v2 DAG seals whole. Parameterised so that a file added to
#: the package later cannot quietly escape the check.
#:
#: ``lifecycle.jsonl`` is deliberately absent: the manifest seals its
#: ``[:sealed_len]`` PREFIX, not the whole file, so growth past that boundary is
#: not a hash violation. It gets its own two tests below — one for mutation
#: inside the seal, one proving that bytes outside it cannot change the answer
#: any reader gives.
IMMUTABLE = [
    "allocation.json",
    "run.json",
    "events/events.jsonl",
    "raw/synthetic.eeg/descriptor.json",
    "raw/synthetic.eeg/chunks.jsonl",
    "raw/synthetic.eeg/stream_close.json",
]


def test_m1_annotating_does_not_invalidate_the_manifest(data_root: DataRoot) -> None:
    """Appending an annotation leaves every manifest hash intact."""
    built = build_session(data_root)
    paths = built.allocated.paths
    before = verify_package(paths)
    assert before.conditions[1] and before.conditions[2]

    annotations_mod.append_annotation(
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
        sealed_outcome=RecordingOutcome.COMPLETED,
        to_outcome=RecordingOutcome.ABORTED,
        actor="researcher",
        reason="reviewed",
    )
    after = verify_package(paths)
    assert after.conditions[1], "manifest.sha256 still verifies"
    assert after.conditions[2], "every inventory hash still verifies"
    # Only the outcome changed, and only through the annotation path.
    assert not after.is_completed


@pytest.mark.parametrize("relative", IMMUTABLE)
def test_m2_mutating_an_immutable_file_is_detected(data_root: DataRoot, relative: str) -> None:
    built = build_session(data_root)
    target = built.allocated.paths.root / relative
    target.write_bytes(target.read_bytes() + b"\n")
    result = verify_package(built.allocated.paths)
    assert not result.is_completed, f"mutation of {relative} went undetected"


def test_m2b_mutating_the_sealed_lifecycle_prefix_is_detected(data_root: DataRoot) -> None:
    """Inside the seal, one changed byte breaks it."""
    built = build_session(data_root)
    target = built.allocated.paths.lifecycle
    raw = target.read_bytes()
    target.write_bytes(raw.replace(b'"actor":"system"', b'"actor":"tamperr"', 1))
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.BROKEN_LIFECYCLE_SEAL in result.findings()


def test_m2c_a_post_seal_lifecycle_append_cannot_change_the_outcome(
    data_root: DataRoot,
) -> None:
    """Bytes past ``sealed_len`` are outside the seal, and outside the answer.

    The manifest hashes ``lifecycle.jsonl[:sealed_len]``, so the contract does
    not forbid growth past that boundary — it makes it irrelevant. What matters
    is that **every** reader agrees on where the authority stops: the verifier,
    recovery and the derived registry all evaluate the sealed prefix, so an
    appended terminal record cannot restore a COMPLETED that was downgraded, or
    turn an ABORTED package into a completed one.
    """
    from consciousness_lab.session import recovery
    from consciousness_lab.session.registry import query_sessions

    built = build_session(data_root, finalize_outcome=RecordingOutcome.ABORTED, close_statuses={})
    paths = built.allocated.paths
    forged = {
        "seq": "99",
        "state": "CLOSED",
        "closure_condition": "CLEAN",
        "recording_outcome": "COMPLETED",
        "actor": "attacker",
        "utc_ns": "1787923530123456789",
        "monotonic_ns": "1000000000",
    }
    with paths.lifecycle.open("ab") as handle:
        handle.write(canonical_json.dump_line(forged))

    result = verify_package(paths)
    assert result.sealed_outcome is RecordingOutcome.ABORTED
    assert not result.is_completed
    assert recovery.scan(paths).state is recovery.StructuralState.SEALED
    rows = {row["session_id"]: row for row in query_sessions(built.data_root)}
    row = rows[built.allocated.session_id]
    assert row["sealed_outcome"] == "ABORTED"
    assert row["is_completed"] == 0


def test_m3_zero_annotation_session_still_ships_a_head_and_completes(
    data_root: DataRoot,
) -> None:
    """RC-R2-3: without step 6 of §14, every clean session would read INDETERMINATE."""
    built = build_session(data_root)
    head_path = built.allocated.paths.annotations_head
    assert head_path.is_file()
    head = AnnotationHead.model_validate(canonical_json.loads(head_path.read_bytes()))
    assert head.record_count == 0
    assert head.bytes == 0
    assert head.head_record_sha256 is None
    assert verify_package(built.allocated.paths).is_completed


def test_m4_annotation_files_are_outside_the_integrity_dag(data_root: DataRoot) -> None:
    """They are written after sealing, so hashing them would break the seal.

    v2 states this positively rather than by omission: ``control_sha256`` keys
    on exactly the derived control set, and these paths are not in it.
    """
    built = build_session(data_root)
    assert built.result is not None
    sealed = set(built.result.manifest.control_sha256)
    assert "annotations.jsonl" not in sealed
    assert "annotations.head.json" not in sealed
    assert "manifest.json" not in sealed
    assert "manifest.sha256" not in sealed
    assert built.allocated.paths.annotations_head.is_file(), "but it is on disk"


def test_logs_are_outside_manifest_scope(data_root: DataRoot) -> None:
    """Operational, never authoritative: writing a log cannot break a sealed package."""
    built = build_session(data_root)
    logs = built.allocated.paths.logs
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "acquisition.log").write_text("written after sealing\n", encoding="utf-8")
    assert verify_package(built.allocated.paths).is_completed
