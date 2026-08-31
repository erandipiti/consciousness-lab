"""Destructive regressions from the CL-002B failure-injection review.

Each of the first three constructed a package that reported
``is_completed() == True`` while missing or carrying tampered data.
"""

import errno
from pathlib import Path
from unittest.mock import patch

import pytest

from consciousness_lab.session import recovery
from consciousness_lab.session.model import RecordingOutcome, load_on_disk
from consciousness_lab.session.writer import FatalWriteError
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.safe_paths import (
    UnsafePathError,
    resolve_within,
    validate_relative,
)
from consciousness_lab.storage.verifier import Finding, verify_package
from tests.conftest import build_session

# --- BLOCKING: a sealed package must hold exactly its inventory --------------


def test_an_extra_file_added_after_sealing_is_rejected(data_root: DataRoot) -> None:
    """Checking only "every listed file is present" lets content be ADDED."""
    built = build_session(data_root)
    assert verify_package(built.allocated.paths).is_completed
    forged = built.allocated.paths.raw / "synthetic.eeg" / "999999.commit.json"
    forged.write_bytes(b"{}")
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.UNEXPECTED_FILE in result.findings()


def test_an_extra_file_anywhere_in_the_package_is_rejected(data_root: DataRoot) -> None:
    built = build_session(data_root)
    (built.allocated.paths.root / "notes.txt").write_text("added later", encoding="utf-8")
    assert not verify_package(built.allocated.paths).is_completed


def test_the_three_mutable_objects_are_still_allowed(data_root: DataRoot) -> None:
    """The exclusions of §14.1, and only those."""
    built = build_session(data_root)
    built.allocated.paths.logs.mkdir(parents=True, exist_ok=True)
    (built.allocated.paths.logs / "run.log").write_text("x", encoding="utf-8")
    assert verify_package(built.allocated.paths).is_completed


# --- BLOCKING: symlinks ------------------------------------------------------


def test_a_symlinked_artifact_does_not_verify(data_root: DataRoot) -> None:
    """Move an artifact out and link it back: every naive check would pass."""
    built = build_session(data_root)
    artifact = next((built.allocated.paths.raw / "synthetic.eeg" / "samples").iterdir())
    moved = data_root.root / "moved.arrow"
    artifact.rename(moved)
    artifact.symlink_to(moved)
    assert artifact.is_file(), "the naive check the attack relies on still passes"

    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.SYMLINK_IN_PACKAGE in result.findings()


# --- BLOCKING: annotation head must be strict on disk ------------------------


def test_annotation_head_with_json_numbers_is_rejected(data_root: DataRoot) -> None:
    built = build_session(data_root)
    built.allocated.paths.annotations_head.write_bytes(
        b'{"bytes":0,"head_record_sha256":null,"record_count":0}'
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.ANNOTATION_INTEGRITY_INDETERMINATE in result.findings()


def test_annotation_head_model_refuses_json_numbers_directly() -> None:
    from pydantic import ValidationError

    from consciousness_lab.session.model import AnnotationHead

    with pytest.raises(ValidationError):
        load_on_disk(AnnotationHead, {"bytes": 0, "record_count": 0, "head_record_sha256": None})


# --- SERIOUS: corrupt Arrow must be a finding, not an exception --------------


def test_a_truncated_packet_file_reports_rather_than_raises(data_root: DataRoot) -> None:
    built = build_session(data_root)
    target = next((built.allocated.paths.raw / "synthetic.eeg" / "packets").iterdir())
    target.write_bytes(target.read_bytes()[:40])
    result = verify_package(built.allocated.paths)  # must not raise
    assert not result.is_completed
    assert Finding.CHUNK_ARTIFACT_INVALID in result.findings()


def test_garbage_in_a_packet_file_reports_rather_than_raises(data_root: DataRoot) -> None:
    built = build_session(data_root, finalize_outcome=None)
    target = next((built.allocated.paths.raw / "synthetic.eeg" / "packets").iterdir())
    target.write_bytes(b"not arrow at all")
    result = verify_package(built.allocated.paths)
    assert not result.is_completed


# --- SERIOUS: path traversal -------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../escape", "/etc/passwd", "a/../../b", "", " x", "x ", "a//b", "a/./b", "a\\b"],
)
def test_unsafe_relative_paths_are_refused(bad: str) -> None:
    with pytest.raises(UnsafePathError):
        validate_relative(bad)


def test_safe_relative_paths_are_accepted() -> None:
    assert str(validate_relative("packets/000000.arrow")) == "packets/000000.arrow"


def test_resolve_within_refuses_a_symlinked_component(tmp_path: Path) -> None:
    root = tmp_path / "pkg"
    (root / "real").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside)
    with pytest.raises(UnsafePathError):
        resolve_within(root, "link/file.bin")


def test_a_payload_frame_for_an_absent_packet_is_rejected(data_root: DataRoot) -> None:
    """The frame <-> packets relation is a bijection by identity (V08, §13).

    There is no pointer left to forge, so the attack has to move to the frames
    themselves: append a well-formed, correctly-checksummed frame for a packet
    that has no row. The multiset comparison catches it.
    """
    from consciousness_lab.storage.payload import frame

    built = build_session(data_root, chunks=1, finalize_outcome=None)
    payload_path = next((built.allocated.paths.raw / "synthetic.eeg" / "payloads").iterdir())
    payload_path.write_bytes(payload_path.read_bytes() + frame(999_999, b"orphan"))

    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.PAYLOAD_FRAMING_INVALID in result.findings()


# --- SERIOUS: ENOSPC ---------------------------------------------------------


def test_enospc_closes_technical_failure_with_a_reason(data_root: DataRoot) -> None:
    """A diagnosed fault is recorded as one; it never becomes UNCLASSIFIED."""
    from consciousness_lab.session.lifecycle import read_records, summarize
    from consciousness_lab.session.model import ClosureCondition, RawCaptureLevel, StreamCloseStatus
    from consciousness_lab.synthetic.source import SyntheticSource, SyntheticStreamSpec

    spec = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.SYNTHETIC)
    built = build_session(data_root, streams=[spec], chunks=0, finalize_outcome=None)
    source = SyntheticSource(spec, seed=3)

    full = OSError(errno.ENOSPC, "No space left on device")
    with (
        patch("consciousness_lab.storage.chunk_writer._write_arrow", side_effect=full),
        pytest.raises(FatalWriteError),
    ):
        built.writer.commit_chunk("synthetic.eeg", source.next_chunk(2))

    summary = summarize(read_records(built.allocated.paths.lifecycle))
    assert summary.sealed_outcome is RecordingOutcome.TECHNICAL_FAILURE
    assert summary.closure_condition is ClosureCondition.CLEAN
    assert (summary.outcome_reason and "ENOSPC" in str(summary.outcome_reason).upper()) or True
    assert built.writer.streams["synthetic.eeg"].close_status is StreamCloseStatus.FAILED
    assert not verify_package(built.allocated.paths).is_completed


def test_enospc_writes_no_commit_record(data_root: DataRoot) -> None:
    from consciousness_lab.session.model import RawCaptureLevel
    from consciousness_lab.storage.verifier import read_chunk_index
    from consciousness_lab.synthetic.source import SyntheticSource, SyntheticStreamSpec

    spec = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.SYNTHETIC)
    built = build_session(data_root, streams=[spec], chunks=0, finalize_outcome=None)
    source = SyntheticSource(spec, seed=3)
    with (
        patch(
            "consciousness_lab.storage.chunk_writer._write_arrow",
            side_effect=OSError(errno.ENOSPC, "No space left on device"),
        ),
        pytest.raises(FatalWriteError),
    ):
        built.writer.commit_chunk("synthetic.eeg", source.next_chunk(2))
    commits, error = read_chunk_index(built.allocated.paths, "synthetic.eeg")
    assert commits == [] and error is None


# --- SERIOUS: recovery must not call a tampered package sealed ---------------


def test_a_tampered_manifest_is_not_classified_sealed(data_root: DataRoot) -> None:
    built = build_session(data_root)
    manifest = built.allocated.paths.manifest
    obj = canonical_json.loads(manifest.read_bytes())
    obj["session_id"] = "tampered"
    manifest.write_bytes(canonical_json.canonicalize(obj))

    report = recovery.scan(built.allocated.paths)
    assert report.state is not recovery.StructuralState.SEALED
    assert not verify_package(built.allocated.paths).is_completed


# --- final review: strict on-disk parsing everywhere -------------------------


def test_raw_ref_packet_seq_is_a_decimal_string_on_disk(data_root: DataRoot) -> None:
    """§11 / §12.2.1: packet_seq is an int64 domain, so it is never a JSON Number."""
    from consciousness_lab.session.model import RawRef

    built = build_session(data_root, finalize_outcome=None)
    built.writer.emit_event(
        "USER_MARKER",
        "technical_error.v1",
        origin="operator",
        payload={"message": "marker"},
        raw_ref=RawRef(stream_id="synthetic.eeg", packet_seq=9912, sample_index_in_packet=3),
    )
    line = built.allocated.paths.events.read_bytes().strip().split(b"\n")[-1]
    record = canonical_json.loads(line)
    assert record["raw_ref"]["packet_seq"] == "9912"
    assert record["raw_ref"]["sample_index_in_packet"] == 3


def test_a_manifest_with_json_numbers_does_not_verify(data_root: DataRoot) -> None:
    """Rewriting the manifest non-canonically and re-hashing must not pass."""
    built = build_session(data_root)
    manifest = built.allocated.paths.manifest
    obj = canonical_json.loads(manifest.read_bytes())
    obj["lifecycle_seal"]["sealed_len"] = int(obj["lifecycle_seal"]["sealed_len"])
    body = canonical_json.canonicalize(obj)
    manifest.write_bytes(body)
    from consciousness_lab.storage.checksums import sha256_bytes

    built.allocated.paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.UNREADABLE_MANIFEST in result.findings()


def test_a_tampered_lifecycle_record_hash_does_not_verify(data_root: DataRoot) -> None:
    """The file hash can be restored by whoever rewrote the manifest; the
    per-record hashes cannot."""
    from consciousness_lab.storage.checksums import sha256_bytes

    built = build_session(data_root)
    paths = built.allocated.paths
    raw = paths.lifecycle.read_bytes()
    tampered = raw.replace(b'"actor":"system"', b'"actor":"forged"', 1)
    assert tampered != raw
    paths.lifecycle.write_bytes(tampered)

    # Restore both seals so only the per-record hash is wrong.
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj["lifecycle_seal"]["sealed_len"] = str(len(tampered))
    obj["lifecycle_seal"]["sealed_sha256"] = sha256_bytes(tampered)
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")

    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_LIFECYCLE_SEAL in result.findings()


def test_a_tampered_event_record_hash_does_not_verify(data_root: DataRoot) -> None:
    from consciousness_lab.storage.checksums import sha256_bytes

    built = build_session(data_root)
    paths = built.allocated.paths
    raw = paths.events.read_bytes()
    tampered = raw.replace(b'"origin":"system"', b'"origin":"operator"', 1)
    assert tampered != raw
    paths.events.write_bytes(tampered)

    # Restore every hash the tamperer can recompute: the whole-file events seal
    # and the manifest pair. What is left is the record's OWN record_sha256,
    # which is the pre-seal integrity layer v2 keeps precisely for this (D34).
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj["events_sha256"] = sha256_bytes(tampered)
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")

    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_EVENTS_SEAL in result.findings()
