"""Post-seal mutability — M1-M4 (spec §14.1; D23)."""

import pytest

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session.model import AnnotationHead, RecordingOutcome
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import verify_package
from tests.conftest import build_session

#: Everything §14.1 declares immutable after sealing. Parameterised so that a
#: file added to the package later cannot quietly escape the check.
IMMUTABLE = [
    "allocation.json",
    "run.json",
    "lifecycle.jsonl",
    "events/events.jsonl",
    "raw/synthetic.eeg/descriptor.json",
    "raw/synthetic.eeg/chunks.jsonl",
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


def test_m4_annotation_files_are_absent_from_the_manifest_inventory(
    data_root: DataRoot,
) -> None:
    """They are written after sealing, so hashing them would recreate finding F2."""
    built = build_session(data_root)
    assert built.result is not None
    inventory = {entry.path for entry in built.result.manifest.inventory}
    assert "annotations.jsonl" not in inventory
    assert "annotations.head.json" not in inventory
    assert "manifest.json" not in inventory
    assert "manifest.sha256" not in inventory
    assert built.allocated.paths.annotations_head.is_file(), "but it is on disk"


def test_logs_are_outside_manifest_scope(data_root: DataRoot) -> None:
    """Operational, never authoritative: writing a log cannot break a sealed package."""
    built = build_session(data_root)
    logs = built.allocated.paths.logs
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "acquisition.log").write_text("written after sealing\n", encoding="utf-8")
    assert verify_package(built.allocated.paths).is_completed
