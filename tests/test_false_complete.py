"""False-complete regressions (spec §5.1, §14; D18, D19, D20, D21, D22).

Every test here corresponds to an attack that was actually constructed against
the design during review. `is_completed()` must return True only when all eight
conditions hold; each test removes exactly one and asserts it does not.
"""

import pytest

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session.model import (
    ClosureCondition,
    RawCaptureLevel,
    RecordingOutcome,
    StreamCloseStatus,
)
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import Finding, verify_package
from consciousness_lab.synthetic.source import SyntheticStreamSpec
from tests.conftest import build_session


def test_a_clean_completed_session_is_completed(data_root: DataRoot) -> None:
    """Test A: sealed COMPLETED, no annotations -> effective COMPLETED, true."""
    built = build_session(data_root)
    result = verify_package(built.allocated.paths)
    assert result.is_completed
    assert result.effective is not None
    assert result.effective.outcome is RecordingOutcome.COMPLETED
    assert result.conditions == dict.fromkeys(range(1, 9), True)


def test_b_valid_downgrade_to_aborted_blocks_completion(data_root: DataRoot) -> None:
    """Test B: a human downgrade after sealing must be visible to the predicate."""
    built = build_session(data_root)
    paths = built.allocated.paths
    annotations_mod.append_annotation(
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
        sealed_outcome=RecordingOutcome.COMPLETED,
        to_outcome=RecordingOutcome.ABORTED,
        actor="researcher",
        reason="participant asked to stop; reviewed afterwards",
    )
    result = verify_package(paths)
    assert result.effective is not None
    assert result.effective.outcome is RecordingOutcome.ABORTED
    assert not result.is_completed
    assert Finding.EFFECTIVE_OUTCOME_NOT_COMPLETED in result.findings()


def test_c_valid_downgrade_to_technical_failure_blocks_completion(data_root: DataRoot) -> None:
    """Test C."""
    built = build_session(data_root)
    paths = built.allocated.paths
    annotations_mod.append_annotation(
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
        sealed_outcome=RecordingOutcome.COMPLETED,
        to_outcome=RecordingOutcome.TECHNICAL_FAILURE,
        actor="engineer",
        reason="post-hoc review found a device fault",
    )
    result = verify_package(paths)
    assert result.effective is not None
    assert result.effective.outcome is RecordingOutcome.TECHNICAL_FAILURE
    assert not result.is_completed


def test_d_annotation_targeting_completed_is_refused(data_root: DataRoot) -> None:
    """Test D: nothing may create or restore COMPLETED (D18)."""
    built = build_session(data_root, finalize_outcome=RecordingOutcome.ABORTED)
    paths = built.allocated.paths
    with pytest.raises(annotations_mod.AnnotationError):
        annotations_mod.append_annotation(
            annotations_path=paths.annotations,
            head_path=paths.annotations_head,
            sealed_outcome=RecordingOutcome.ABORTED,
            to_outcome=RecordingOutcome.COMPLETED,
            actor="attacker",
            reason="promote",
        )
    assert not verify_package(paths).is_completed


def test_d_hand_forged_upgrade_record_is_rejected_not_applied(data_root: DataRoot) -> None:
    """A forged, correctly-hashed upgrade record must still be rejected.

    The API refuses to write one, so this bypasses the API entirely and writes
    a fully valid chain by hand. Chain integrity is not the same as semantic
    validity, and the resolver must enforce both.
    """
    from consciousness_lab.storage import canonical_json
    from consciousness_lab.storage.checksums import append_line, atomic_write

    built = build_session(data_root, finalize_outcome=RecordingOutcome.ABORTED)
    paths = built.allocated.paths
    body = {
        "seq": "0",
        "from": "ABORTED",
        "to": "COMPLETED",
        "actor": "attacker",
        "reason": "promote",
        "utc_ns": "1787923530123456789",
        "prev_record_sha256": canonical_json.ZERO_HASH,
    }
    append_line(paths.annotations, canonical_json.dump_line(body))
    head = {
        "bytes": str(paths.annotations.stat().st_size),
        "record_count": "1",
        "head_record_sha256": canonical_json.record_hash(body),
    }
    atomic_write(paths.annotations_head, canonical_json.canonicalize(head))

    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ANNOTATION_REJECTED in result.findings()
    assert result.effective is not None
    assert result.effective.outcome is RecordingOutcome.ABORTED


def test_e_corrupt_annotation_log_is_indeterminate_never_completed(data_root: DataRoot) -> None:
    """Test E: a broken chain fails closed and must never report COMPLETED."""
    built = build_session(data_root)
    paths = built.allocated.paths
    annotations_mod.append_annotation(
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
        sealed_outcome=RecordingOutcome.COMPLETED,
        to_outcome=RecordingOutcome.ABORTED,
        actor="researcher",
        reason="reviewed",
    )
    raw = paths.annotations.read_bytes()
    paths.annotations.write_bytes(raw.replace(b'"reviewed"', b'"tampered"'))

    result = verify_package(paths)
    assert result.effective is not None
    assert result.effective.status is annotations_mod.AnnotationStatus.INDETERMINATE
    assert result.effective.outcome is None, "must not fall back to the sealed outcome"
    assert not result.is_completed
    assert Finding.ANNOTATION_INTEGRITY_INDETERMINATE in result.findings()


def test_deleted_annotation_log_is_indeterminate(data_root: DataRoot) -> None:
    """11f: a hash chain cannot prove no record was removed; the head can."""
    built = build_session(data_root)
    paths = built.allocated.paths
    annotations_mod.append_annotation(
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
        sealed_outcome=RecordingOutcome.COMPLETED,
        to_outcome=RecordingOutcome.ABORTED,
        actor="researcher",
        reason="reviewed",
    )
    paths.annotations.unlink()
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ANNOTATION_INTEGRITY_INDETERMINATE in result.findings()


def test_truncated_annotation_log_on_a_record_boundary(data_root: DataRoot) -> None:
    """11g: boundary truncation leaves a valid shorter chain; the head catches it."""
    built = build_session(data_root)
    paths = built.allocated.paths
    annotations_mod.append_annotation(
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
        sealed_outcome=RecordingOutcome.COMPLETED,
        to_outcome=RecordingOutcome.ABORTED,
        actor="researcher",
        reason="downgraded",
    )
    assert paths.annotations.stat().st_size > 0
    # Truncate on the record boundary: the remaining chain is perfectly valid,
    # and only the head file reveals that a record went missing.
    paths.annotations.write_bytes(b"")
    result = verify_package(paths)
    assert result.effective is not None
    assert result.effective.status is annotations_mod.AnnotationStatus.INDETERMINATE
    assert result.effective.outcome is None
    assert not result.is_completed


def test_missing_annotation_head_is_indeterminate(data_root: DataRoot) -> None:
    """11h: finalization always writes the head, so its absence means tampering."""
    built = build_session(data_root)
    paths = built.allocated.paths
    paths.annotations_head.unlink()
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ANNOTATION_INTEGRITY_INDETERMINATE in result.findings()


def test_head_claims_zero_but_log_is_not_empty(data_root: DataRoot) -> None:
    """11i."""
    from consciousness_lab.storage import canonical_json
    from consciousness_lab.storage.checksums import append_line

    built = build_session(data_root)
    paths = built.allocated.paths
    append_line(paths.annotations, canonical_json.dump_line({"seq": "0"}))
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ANNOTATION_INTEGRITY_INDETERMINATE in result.findings()


def test_stale_from_is_rejected(data_root: DataRoot) -> None:
    """11j: a second record whose ``from`` no longer matches is rejected."""
    from consciousness_lab.storage import canonical_json
    from consciousness_lab.storage.checksums import append_line, atomic_write

    built = build_session(data_root)
    paths = built.allocated.paths
    first = annotations_mod.append_annotation(
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
        sealed_outcome=RecordingOutcome.COMPLETED,
        to_outcome=RecordingOutcome.ABORTED,
        actor="researcher",
        reason="first",
    )
    stale = {
        "seq": "1",
        "from": "COMPLETED",
        "to": "TECHNICAL_FAILURE",
        "actor": "researcher",
        "reason": "stale premise",
        "utc_ns": "1787923530123456790",
        "prev_record_sha256": str(first.record_sha256),
    }
    append_line(paths.annotations, canonical_json.dump_line(stale))
    atomic_write(
        paths.annotations_head,
        canonical_json.canonicalize(
            {
                "bytes": str(paths.annotations.stat().st_size),
                "record_count": "2",
                "head_record_sha256": canonical_json.record_hash(stale),
            }
        ),
    )
    result = verify_package(paths)
    assert result.effective is not None
    assert result.effective.outcome is RecordingOutcome.ABORTED
    assert any(
        r.reason is annotations_mod.RejectReason.STALE_FROM for r in result.effective.rejected
    )
    assert not result.is_completed


def test_lateral_reclassification_is_forbidden(data_root: DataRoot) -> None:
    """11e / D19: ABORTED <-> TECHNICAL_FAILURE is not a v1 transition."""
    built = build_session(data_root, finalize_outcome=RecordingOutcome.ABORTED)
    paths = built.allocated.paths
    with pytest.raises(annotations_mod.AnnotationError):
        annotations_mod.append_annotation(
            annotations_path=paths.annotations,
            head_path=paths.annotations_head,
            sealed_outcome=RecordingOutcome.ABORTED,
            to_outcome=RecordingOutcome.TECHNICAL_FAILURE,
            actor="engineer",
            reason="sideways correction",
        )


def test_required_stream_unclean_blocks_completion(data_root: DataRoot) -> None:
    """Condition 5: a required device disconnecting must not still read COMPLETED."""
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD),
            SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED),
        ],
        required=("synthetic.eeg", "synthetic.ecg"),
        close_statuses={"synthetic.ecg": StreamCloseStatus.DISCONNECTED},
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.REQUIRED_STREAM_UNCLEAN in result.findings()


def test_optional_stream_unclean_does_not_block_completion(data_root: DataRoot) -> None:
    """The mirror of the previous test: optional streams are optional."""
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD),
            SyntheticStreamSpec("synthetic.imu", RawCaptureLevel.SYNTHETIC),
        ],
        required=("synthetic.eeg",),
        optional=("synthetic.imu",),
        close_statuses={"synthetic.imu": StreamCloseStatus.DISCONNECTED},
    )
    assert verify_package(built.allocated.paths).is_completed


def test_aborted_session_has_a_valid_manifest_pair_but_is_not_completed(
    data_root: DataRoot,
) -> None:
    """11b / D21: the manifest pair is a FINALIZATION marker, not a completion marker."""
    built = build_session(data_root, finalize_outcome=RecordingOutcome.ABORTED)
    result = verify_package(built.allocated.paths)
    assert result.conditions[1], "the manifest pair itself is perfectly valid"
    assert result.is_sealed
    assert not result.is_completed
    assert Finding.SEALED_OUTCOME_NOT_COMPLETED in result.findings()


def test_manifest_without_sha_is_not_completed(data_root: DataRoot) -> None:
    built = build_session(data_root)
    built.allocated.paths.manifest_sha256.unlink()
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.MISSING_MANIFEST_SHA in result.findings()


def test_corrupted_lifecycle_prefix_is_detected(data_root: DataRoot) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    raw = paths.lifecycle.read_bytes()
    paths.lifecycle.write_bytes(raw.replace(b'"system"', b'"forged"', 1))
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_LIFECYCLE_SEAL in result.findings()


def test_recovered_unclean_session_is_not_completed(data_root: DataRoot) -> None:
    built = build_session(
        data_root,
        finalize_outcome=RecordingOutcome.UNCLASSIFIED,
        closure=ClosureCondition.RECOVERED_UNCLEAN,
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.NOT_CLEANLY_CLOSED in result.findings()
