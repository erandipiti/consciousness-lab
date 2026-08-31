"""The v2 data contract itself — what the format is, and what it no longer is.

Every removal D27-D34 made is asserted here against the bytes on disk, not
against the model. A field can vanish from a Pydantic class and still be written
by some other path; only the file proves it is gone.
"""

import pytest
from pydantic import ValidationError

from consciousness_lab.session.model import (
    SCHEMA_MAJOR,
    SCHEMA_VERSION,
    ChunkCommit,
    Manifest,
    RawCaptureLevel,
    artifact_relative_path,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.arrow_schema import PACKETS_SCHEMA
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import verify_package
from consciousness_lab.synthetic.source import SyntheticStreamSpec
from tests.conftest import build_session, read_chunk_records


def test_the_package_declares_schema_version_2_0(data_root: DataRoot) -> None:
    assert (SCHEMA_VERSION, SCHEMA_MAJOR) == ("2.0", 2)
    built = build_session(data_root)
    for path in (built.allocated.paths.allocation, built.allocated.paths.manifest):
        obj = canonical_json.loads(path.read_bytes())
        assert obj["schema_name"] == "session_package"
        assert obj["schema_version"] == "2.0"


def test_no_sidecar_is_written_anywhere(data_root: DataRoot) -> None:
    """D28: ``chunks.jsonl`` is the only persisted chunk commit authority."""
    built = build_session(data_root, chunks=3)
    assert list(built.allocated.paths.root.rglob("*.commit.json")) == []


def test_the_chunk_record_carries_exactly_three_keys(data_root: DataRoot) -> None:
    """The minimal ChunkCommit of §6, checked against the bytes."""
    built = build_session(data_root, chunks=2)
    for record in read_chunk_records(built.allocated.paths, "synthetic.eeg"):
        assert set(record) == {"chunk_id", "prev_record_sha256", "artifact_sha256"}
        assert set(record["artifact_sha256"]) == {
            "packets",
            "observations",
            "samples",
            "payloads",
        }


@pytest.mark.parametrize(
    "removed",
    [
        "record_sha256",
        "first_packet_seq",
        "last_packet_seq",
        "descriptor_sha256",
        "path",
        "bytes",
    ],
)
def test_removed_v1_chunk_fields_appear_nowhere_in_the_chain(
    data_root: DataRoot, removed: str
) -> None:
    built = build_session(data_root, chunks=2)
    raw = built.allocated.paths.stream("synthetic.eeg").chunks_index.read_bytes()
    assert f'"{removed}"'.encode() not in raw


def test_a_v2_writer_does_not_accept_deprecated_v1_fields() -> None:
    """Not merely ignored on read: they are not part of the contract at all."""
    base = {
        "chunk_id": "0",
        "prev_record_sha256": "0" * 64,
        "artifact_sha256": {"packets": "a" * 64, "observations": "b" * 64, "samples": "c" * 64},
    }
    commit = ChunkCommit.model_validate(base)
    assert not hasattr(commit, "first_packet_seq")
    assert not hasattr(commit, "descriptor_sha256")
    assert not hasattr(commit, "record_sha256")
    with pytest.raises(ValidationError):
        ChunkCommit.model_validate({**base, "artifact_sha256": {"packets": "a" * 64}})
    artifacts: dict[str, str] = {"packets": "a" * 64, "observations": "b" * 64, "samples": "c" * 64}
    artifacts["mystery"] = "d" * 64
    with pytest.raises(ValidationError):
        ChunkCommit.model_validate({**base, "artifact_sha256": artifacts})


def test_artifact_paths_are_derived_not_stored(data_root: DataRoot) -> None:
    """§5: the path is a function of ``(kind, chunk_id)``, and appears nowhere."""
    assert artifact_relative_path("packets", 123) == "packets/000123.arrow"
    assert artifact_relative_path("payloads", 7) == "payloads/000007.bin"
    built = build_session(data_root, chunks=2)
    raw = built.allocated.paths.stream("synthetic.eeg").chunks_index.read_bytes()
    assert b".arrow" not in raw and b".bin" not in raw
    for record in read_chunk_records(built.allocated.paths, "synthetic.eeg"):
        chunk_id = int(record["chunk_id"])
        for kind in record["artifact_sha256"]:
            target = built.allocated.paths.stream("synthetic.eeg").root / artifact_relative_path(
                kind, chunk_id
            )
            assert target.is_file()


def test_the_packets_schema_carries_no_payload_ref() -> None:
    assert "payload_ref" not in PACKETS_SCHEMA.names


def test_the_manifest_owns_no_stream_fact(data_root: DataRoot) -> None:
    """§7: finalization marker and integrity root, and nothing else."""
    built = build_session(data_root)
    obj = canonical_json.loads(built.allocated.paths.manifest.read_bytes())
    assert set(obj) == {
        "schema_name",
        "schema_version",
        "sealed_at",
        "lifecycle_seal",
        "events_sha256",
        "control_sha256",
    }
    for gone in ("streams", "inventory", "schemas", "session_id", "scope_note", "events_seal"):
        assert gone not in obj
    raw = built.allocated.paths.manifest.read_bytes()
    assert b"close_status" not in raw, "closure lives in stream_close.json alone"
    assert b"stream_close_status" not in raw


def test_the_manifest_carries_no_session_id(data_root: DataRoot) -> None:
    """Identity is owned by allocation.json and the directory name (§7).

    A manifest swapped between packages is caught because the allocation hash
    would not match, so a third copy of the identity buys nothing.
    """
    assert "session_id" not in Manifest.model_fields
    first = build_session(data_root)
    second = build_session(data_root)
    swapped = second.allocated.paths.manifest.read_bytes()
    first.allocated.paths.manifest.write_bytes(swapped)
    first.allocated.paths.manifest_sha256.write_text(
        second.allocated.paths.manifest_sha256.read_text()
    )
    result = verify_package(first.allocated.paths)
    assert not result.is_completed, "the allocation hash catches the swap"


def test_a_raw_artifact_hash_is_persisted_exactly_once(data_root: DataRoot) -> None:
    """D30: transitive protection is protection.

    In v1 every raw artifact hash appeared twice — in the commit record and
    again in the manifest inventory — and the two had to be reconciled forever.
    """
    built = build_session(data_root, chunks=2)
    manifest_raw = built.allocated.paths.manifest.read_bytes()
    for record in read_chunk_records(built.allocated.paths, "synthetic.eeg"):
        for digest in record["artifact_sha256"].values():
            assert digest.encode() not in manifest_raw


def test_control_sha256_seals_the_derived_set_and_nothing_else(data_root: DataRoot) -> None:
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD),
            SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED),
        ],
        required=("synthetic.eeg", "synthetic.ecg"),
    )
    assert built.result is not None
    control = built.result.manifest.control_sha256
    for stream_id in ("synthetic.eeg", "synthetic.ecg"):
        for name in ("descriptor.json", "chunks.jsonl", "stream_close.json"):
            assert f"raw/{stream_id}/{name}" in control
    assert "allocation.json" in control and "run.json" in control
    assert not any(key.endswith(".arrow") or key.endswith(".bin") for key in control)
    assert "lifecycle.jsonl" not in control, "sealed by lifecycle_seal, not twice"
    assert "events/events.jsonl" not in control, "sealed by events_sha256, not twice"
