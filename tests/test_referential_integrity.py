"""CL-002B-R1: manifest / run.json / raw referential integrity.

The defect these close: the manifest stream summary was built from the writer's
in-memory state while verification iterated the directories that happened to
exist, so neither view proved anything about the other. A manifest could
describe a required stream whose raw directory had been deleted and the package
still satisfied all eight completion conditions.

Every tamper test here recomputes whatever hashes the attacker could recompute —
record hashes, chain hashes, the manifest and its SHA — so each asks the harder
question: can an internally self-consistent but semantically false package pass?
"""

import shutil
from typing import Any

import pytest

from consciousness_lab.session.finalizer import FinalizationError, finalize
from consciousness_lab.session.model import (
    RawCaptureLevel,
    RecordingOutcome,
    Run,
)
from consciousness_lab.session.writer import SessionWriter
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import sha256_bytes
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.verifier import Finding, verify_package
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSpec,
    build_descriptor,
)
from tests.conftest import build_session, now_reading

EEG = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD)
IMU = SyntheticStreamSpec("synthetic.imu", RawCaptureLevel.SYNTHETIC)
ECG = SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED)


def reseal_manifest(paths: PackagePaths, mutate: Any) -> None:
    """Rewrite the manifest and its SHA so only the semantics are wrong.

    An attacker can recompute every ordinary hash in a file they rewrite, so a
    stale hash must never be the only thing standing between a forged manifest
    and a completed verdict.
    """
    obj = canonical_json.loads(paths.manifest.read_bytes())
    mutate(obj)
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")


def reinventory(paths: PackagePaths, relative: str) -> None:
    """Refresh one inventory entry after rewriting the file it describes."""
    target = paths.root / relative
    entry_bytes = str(target.stat().st_size)
    digest = sha256_bytes(target.read_bytes())

    def mutate(obj: dict[str, Any]) -> None:
        for entry in obj["inventory"]:
            if entry["path"] == relative:
                entry["bytes"] = entry_bytes
                entry["sha256"] = digest

    reseal_manifest(paths, mutate)


# --- R1: the original false-complete -----------------------------------------


def test_finalizer_refuses_when_a_required_stream_directory_was_deleted(
    data_root: DataRoot,
) -> None:
    """R1. Reproduces the CL-002B-R1 defect: it passed before the fix.

    Writer memory still said CLEAN with two committed chunks while the whole
    raw directory was gone, and all eight conditions returned true.
    """
    from consciousness_lab.session.allocator import allocate_session

    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(Run(sealed_at=now_reading(), required_streams=[EEG.stream_id]))
    writer.open_stream(build_descriptor(EEG))
    source = SyntheticSource(EEG, seed=7)
    for _ in range(2):
        writer.commit_chunk(EEG.stream_id, source.next_chunk(3))

    shutil.rmtree(allocated.paths.raw / EEG.stream_id)

    with pytest.raises(FinalizationError, match="no raw directory"):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)

    # And no immutable record may claim COMPLETED.
    from consciousness_lab.session.lifecycle import read_records, summarize

    summary = summarize(read_records(allocated.paths.lifecycle))
    assert summary.sealed_outcome is not RecordingOutcome.COMPLETED
    assert not verify_package(allocated.paths).is_completed


def test_manifest_cannot_claim_a_required_stream_whose_raw_directory_is_missing(
    data_root: DataRoot,
) -> None:
    """R2. The same attack, executed after sealing instead of before."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,))
    assert verify_package(built.allocated.paths).is_completed

    shutil.rmtree(built.allocated.paths.raw / EEG.stream_id)

    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.MANIFEST_STREAM_MISSING_RAW in result.findings()
    assert result.conditions[5] is False and result.conditions[7] is False


# --- R3/R4: bidirectional set reconciliation ---------------------------------


def test_a_manifest_cannot_invent_a_stream(data_root: DataRoot) -> None:
    """R3. A self-consistent manifest is not evidence that raw data exists."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,))

    def mutate(obj: dict[str, Any]) -> None:
        invented = dict(obj["streams"][0])
        invented["stream_id"] = "synthetic.fake"
        invented["required"] = False
        obj["streams"].append(invented)

    reseal_manifest(built.allocated.paths, mutate)
    result = verify_package(built.allocated.paths)
    assert result.conditions[1], "the manifest pair itself is internally valid"
    assert not result.is_completed
    assert Finding.MANIFEST_STREAM_MISSING_RAW in result.findings()


def test_a_manifest_cannot_omit_a_physical_stream(data_root: DataRoot) -> None:
    """R4. Dropping a stream from the manifest must not hide it."""
    built = build_session(
        data_root,
        streams=[EEG, IMU],
        required=(EEG.stream_id,),
        optional=(IMU.stream_id,),
    )
    reseal_manifest(
        built.allocated.paths,
        lambda obj: obj.__setitem__(
            "streams", [s for s in obj["streams"] if s["stream_id"] != IMU.stream_id]
        ),
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.RAW_STREAM_MISSING_MANIFEST in result.findings()


def test_duplicate_manifest_stream_ids_are_rejected(data_root: DataRoot) -> None:
    """Sibling case: the same stream listed twice."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,))
    reseal_manifest(
        built.allocated.paths, lambda obj: obj["streams"].append(dict(obj["streams"][0]))
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.DUPLICATE_MANIFEST_STREAM in result.findings()


# --- R5/R6/R7/R8: descriptor referential integrity ---------------------------


def test_a_deleted_descriptor_is_detected(data_root: DataRoot) -> None:
    """R5."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,))
    (built.allocated.paths.raw / EEG.stream_id / "descriptor.json").unlink()
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.MISSING_DESCRIPTOR in result.findings()


def test_a_descriptor_declaring_another_stream_id_is_rejected(data_root: DataRoot) -> None:
    """R6. Every downstream hash is recomputed, so only the semantics are wrong."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,))
    paths = built.allocated.paths
    descriptor_path = paths.raw / EEG.stream_id / "descriptor.json"

    obj = canonical_json.loads(descriptor_path.read_bytes())
    obj["stream_id"] = "synthetic.other"
    body = canonical_json.canonicalize(obj)
    descriptor_path.write_bytes(body)
    new_hash = sha256_bytes(body)

    # Recompute the chunk chain so every record references the new descriptor.
    _rechain(paths, EEG.stream_id, descriptor_sha256=new_hash)

    def mutate(manifest: dict[str, Any]) -> None:
        for entry in manifest["streams"]:
            if entry["stream_id"] == EEG.stream_id:
                entry["descriptor_sha256"] = new_hash
                entry["chunk_chain_head_sha256"] = _chain_head(paths, EEG.stream_id)
        for entry in manifest["inventory"]:
            target = paths.root / entry["path"]
            if entry["path"].endswith(("descriptor.json", "chunks.jsonl")):
                entry["bytes"] = str(target.stat().st_size)
                entry["sha256"] = sha256_bytes(target.read_bytes())

    reseal_manifest(paths, mutate)
    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2], "hashes were all recomputed"
    assert not result.is_completed
    assert Finding.DESCRIPTOR_STREAM_ID_MISMATCH in result.findings()


def test_manifest_descriptor_hash_must_match_the_stored_bytes(data_root: DataRoot) -> None:
    """R7."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,))
    reseal_manifest(
        built.allocated.paths,
        lambda obj: obj["streams"][0].__setitem__("descriptor_sha256", "0" * 64),
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.MANIFEST_DESCRIPTOR_HASH_MISMATCH in result.findings()


def test_a_chunk_written_under_another_descriptor_is_rejected(data_root: DataRoot) -> None:
    """R8. The chain is fully re-hashed, so the chain itself stays valid."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    paths = built.allocated.paths
    _rechain(paths, EEG.stream_id, descriptor_sha256="1" * 64)

    def mutate(manifest: dict[str, Any]) -> None:
        for entry in manifest["streams"]:
            if entry["stream_id"] == EEG.stream_id:
                entry["chunk_chain_head_sha256"] = _chain_head(paths, EEG.stream_id)
        for entry in manifest["inventory"]:
            if entry["path"].endswith("chunks.jsonl"):
                target = paths.root / entry["path"]
                entry["bytes"] = str(target.stat().st_size)
                entry["sha256"] = sha256_bytes(target.read_bytes())

    reseal_manifest(paths, mutate)
    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2]
    assert not result.is_completed
    assert Finding.CHUNK_DESCRIPTOR_HASH_MISMATCH in result.findings()


# --- R9/R10/R11: chunk summary referential integrity -------------------------


@pytest.mark.parametrize("delta", [-1, 1])
def test_manifest_chunk_count_must_match_the_chain(data_root: DataRoot, delta: int) -> None:
    """R9."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    reseal_manifest(
        built.allocated.paths,
        lambda obj: obj["streams"][0].__setitem__(
            "chunk_count", str(int(obj["streams"][0]["chunk_count"]) + delta)
        ),
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.CHUNK_COUNT_MISMATCH in result.findings()


def test_manifest_chain_head_must_match_the_final_record(data_root: DataRoot) -> None:
    """R10."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    reseal_manifest(
        built.allocated.paths,
        lambda obj: obj["streams"][0].__setitem__("chunk_chain_head_sha256", "a" * 64),
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.CHAIN_HEAD_MISMATCH in result.findings()


@pytest.mark.parametrize("field", ["first_packet_seq", "last_packet_seq"])
def test_manifest_packet_range_must_match_the_commits(data_root: DataRoot, field: str) -> None:
    """R11."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    reseal_manifest(
        built.allocated.paths,
        lambda obj: obj["streams"][0].__setitem__(field, "4242"),
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.PACKET_RANGE_MISMATCH in result.findings()


# --- R12/R13: run.json <-> manifest required flag ----------------------------


def test_a_required_stream_marked_not_required_is_rejected(data_root: DataRoot) -> None:
    """R12."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,))
    reseal_manifest(
        built.allocated.paths, lambda obj: obj["streams"][0].__setitem__("required", False)
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.REQUIRED_FLAG_MISMATCH in result.findings()


def test_an_optional_stream_marked_required_is_rejected(data_root: DataRoot) -> None:
    """R13."""
    built = build_session(
        data_root,
        streams=[EEG, IMU],
        required=(EEG.stream_id,),
        optional=(IMU.stream_id,),
    )

    def mutate(obj: dict[str, Any]) -> None:
        for entry in obj["streams"]:
            if entry["stream_id"] == IMU.stream_id:
                entry["required"] = True

    reseal_manifest(built.allocated.paths, mutate)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.REQUIRED_FLAG_MISMATCH in result.findings()


# --- R14/R15: positive controls ----------------------------------------------


def test_a_consistent_multi_stream_session_still_completes(data_root: DataRoot) -> None:
    """R14. Positive control: required + optional, everything agreeing."""
    built = build_session(
        data_root,
        streams=[EEG, IMU],
        required=(EEG.stream_id,),
        optional=(IMU.stream_id,),
    )
    result = verify_package(built.allocated.paths)
    assert result.is_completed
    assert result.manifest is not None
    by_id = {s.stream_id: s for s in result.manifest.streams}
    assert by_id[EEG.stream_id].required is True
    assert by_id[IMU.stream_id].required is False


def test_reconciliation_holds_across_all_three_capture_levels(data_root: DataRoot) -> None:
    """R15. Mixed capture levels must not regress under the new checks."""
    built = build_session(
        data_root,
        streams=[EEG, ECG, IMU],
        required=(EEG.stream_id, ECG.stream_id),
        optional=(IMU.stream_id,),
    )
    result = verify_package(built.allocated.paths)
    assert result.is_completed
    assert result.manifest is not None
    assert {s.stream_id for s in result.manifest.streams} == {
        EEG.stream_id,
        ECG.stream_id,
        IMU.stream_id,
    }


# --- sibling cases -----------------------------------------------------------


def test_a_stream_missing_its_chunk_index_is_rejected(data_root: DataRoot) -> None:
    """chunks.jsonl exists from stream open, so its absence is never ambiguous."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,))
    (built.allocated.paths.raw / EEG.stream_id / "chunks.jsonl").unlink()
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.MISSING_STREAM_STRUCTURE in result.findings()


def test_a_zero_chunk_stream_is_structurally_valid(data_root: DataRoot) -> None:
    """Structural truth only. Whether zero chunks is scientifically usable is a
    future protocol decision and is deliberately not asserted here."""
    from consciousness_lab.session.allocator import allocate_session

    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(Run(sealed_at=now_reading(), required_streams=[EEG.stream_id]))
    writer.open_stream(build_descriptor(EEG))
    finalize(writer, outcome=RecordingOutcome.COMPLETED)

    result = verify_package(allocated.paths)
    assert result.is_completed, "no minimum chunk/sample/duration rule exists"
    assert result.manifest is not None
    entry = result.manifest.streams[0]
    assert entry.chunk_count == 0
    assert entry.chunk_chain_head_sha256 is None
    assert entry.first_packet_seq is None and entry.last_packet_seq is None


def test_finalizer_refuses_when_a_committed_chunk_artifact_vanished(
    data_root: DataRoot,
) -> None:
    """Preflight covers artifacts, not just directories."""
    from consciousness_lab.session.allocator import allocate_session

    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(Run(sealed_at=now_reading(), required_streams=[EEG.stream_id]))
    writer.open_stream(build_descriptor(EEG))
    source = SyntheticSource(EEG, seed=5)
    writer.commit_chunk(EEG.stream_id, source.next_chunk(2))
    next((allocated.paths.raw / EEG.stream_id / "samples").iterdir()).unlink()

    with pytest.raises(FinalizationError, match="artifact"):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)


def test_finalizer_refuses_when_the_chunk_index_was_truncated(data_root: DataRoot) -> None:
    """A commit the writer believes in must still be in the index."""
    from consciousness_lab.session.allocator import allocate_session

    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(Run(sealed_at=now_reading(), required_streams=[EEG.stream_id]))
    writer.open_stream(build_descriptor(EEG))
    source = SyntheticSource(EEG, seed=5)
    writer.commit_chunk(EEG.stream_id, source.next_chunk(2))
    (allocated.paths.raw / EEG.stream_id / "chunks.jsonl").write_bytes(b"")

    with pytest.raises(FinalizationError, match=r"absent from chunks\.jsonl"):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)


def test_an_aborted_session_with_a_deleted_stream_still_seals_honestly(
    data_root: DataRoot,
) -> None:
    """The preflight gates COMPLETED only. An abort still records what is real."""
    from consciousness_lab.session.allocator import allocate_session

    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(Run(sealed_at=now_reading(), required_streams=[EEG.stream_id]))
    writer.open_stream(build_descriptor(EEG))
    source = SyntheticSource(EEG, seed=5)
    writer.commit_chunk(EEG.stream_id, source.next_chunk(2))
    shutil.rmtree(allocated.paths.raw / EEG.stream_id)

    finalize(writer, outcome=RecordingOutcome.ABORTED)
    result = verify_package(allocated.paths)
    assert result.manifest is not None
    assert result.manifest.streams == [], "the manifest describes what exists, nothing more"
    assert not result.is_completed


# --- helpers -----------------------------------------------------------------


def _rechain(paths: PackagePaths, stream_id: str, *, descriptor_sha256: str) -> None:
    """Rewrite a stream's chunk index with a new descriptor hash, re-chaining it."""
    index = paths.stream(stream_id).chunks_index
    prev = canonical_json.ZERO_HASH
    lines: list[bytes] = []
    for line in index.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        record = canonical_json.loads(line)
        record.pop(canonical_json.RECORD_HASH_KEY, None)
        record["descriptor_sha256"] = descriptor_sha256
        record["prev_record_sha256"] = prev
        prev = canonical_json.record_hash(record)
        lines.append(canonical_json.dump_line(record))
    index.write_bytes(b"".join(lines))


def _chain_head(paths: PackagePaths, stream_id: str) -> str:
    raw = paths.stream(stream_id).chunks_index.read_bytes().strip().split(b"\n")
    return str(canonical_json.loads(raw[-1])[canonical_json.RECORD_HASH_KEY])
