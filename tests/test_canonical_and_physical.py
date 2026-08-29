"""CL-002B-R1-C2: canonical on-disk record identity and physical packet truth.

Closes three BLOCKING findings that shared one root cause — interpreted
representations were compared where the actual bytes and the actual data were
required:

* **B1** sidecar reconciliation compared parsed ``ChunkCommit`` models, so two
  different on-disk documents that normalize identically compared equal.
* **B2** a forged ``chunks.jsonl`` plus matching sidecars could lie about packet
  ranges and artifact paths, because summaries were only ever checked against
  other summaries.
* **B3** the finalizer preflight checked which chunk ids were present but never
  whether the records themselves still matched what the writer committed.

Every negative test below recomputes everything the attacker controls — record
hashes, chain links, sidecars, manifest summaries, inventory and
``manifest.sha256`` — so no test passes merely because a hash was left stale.
"""

from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest

from consciousness_lab.session.allocator import allocate_session
from consciousness_lab.session.finalizer import FinalizationError, finalize
from consciousness_lab.session.model import RawCaptureLevel, RecordingOutcome, Run
from consciousness_lab.session.writer import SessionWriter
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import sha256_bytes
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.stream_state import read_physical_stream
from consciousness_lab.storage.verifier import Finding, verify_package
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSpec,
    build_descriptor,
)
from tests.conftest import build_session, now_reading

EEG = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD)
SYN = SyntheticStreamSpec("synthetic.imu", RawCaptureLevel.SYNTHETIC)


# --- attacker toolkit --------------------------------------------------------


def refresh_manifest(paths: PackagePaths, stream_id: str) -> None:
    """Recompute every manifest fact the attacker legitimately controls.

    Stream summary from the (possibly forged) chain, full inventory from disk,
    then ``manifest.sha256``. After this the package is internally
    self-consistent, so only a check against physical data can catch a lie.
    """
    state = read_physical_stream(paths, stream_id)
    chain = [c.record.model for c in state.chunks]

    obj = canonical_json.loads(paths.manifest.read_bytes())
    for entry in obj["streams"]:
        if entry["stream_id"] != stream_id:
            continue
        entry["chunk_count"] = str(len(chain))
        entry["chunk_chain_head_sha256"] = (
            state.chunks[-1].record.record_sha256 if state.chunks else None
        )
        if chain:
            entry["first_packet_seq"] = str(chain[0].first_packet_seq)
            entry["last_packet_seq"] = str(chain[-1].last_packet_seq)
        entry["descriptor_sha256"] = state.descriptor_sha256

    listed = {e["path"] for e in obj["inventory"]}
    for path in sorted(paths.root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(paths.root).as_posix()
        if rel in {
            "manifest.json",
            "manifest.sha256",
            "annotations.jsonl",
            "annotations.head.json",
        } or rel.startswith("logs/"):
            continue
        fresh = {
            "path": rel,
            "bytes": str(path.stat().st_size),
            "sha256": sha256_bytes(path.read_bytes()),
        }
        if rel in listed:
            for existing in obj["inventory"]:
                if existing["path"] == rel:
                    existing.update(fresh)
        else:
            obj["inventory"].append(fresh)
    obj["inventory"] = [e for e in obj["inventory"] if (paths.root / e["path"]).is_file()]
    obj["inventory"].sort(key=lambda e: str(e["path"]))

    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")


def forge_chain(
    paths: PackagePaths,
    stream_id: str,
    mutate: Callable[[int, dict[str, Any]], None],
    *,
    rewrite_sidecars: bool = True,
) -> None:
    """Rewrite the chain, re-chaining it, and optionally its sidecars to match.

    ``mutate(chunk_id, record)`` edits each record in place before it is
    re-hashed, so the forged chain is fully valid on its own terms.
    """
    stream = paths.stream(stream_id)
    prev = canonical_json.ZERO_HASH
    lines: list[bytes] = []
    for line in stream.chunks_index.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        record = canonical_json.loads(line)
        record.pop(canonical_json.RECORD_HASH_KEY, None)
        chunk_id = int(record["chunk_id"])
        record["prev_record_sha256"] = prev
        mutate(chunk_id, record)
        digest = canonical_json.record_hash(record)
        prev = digest
        sealed = dict(record)
        sealed[canonical_json.RECORD_HASH_KEY] = digest
        lines.append(canonical_json.canonicalize(sealed) + b"\n")
        if rewrite_sidecars:
            (stream.root / f"{chunk_id:06d}.commit.json").write_bytes(
                canonical_json.canonicalize(sealed)
            )
    stream.chunks_index.write_bytes(b"".join(lines))


def rewrite_sidecar(paths: PackagePaths, stream_id: str, chunk_id: int, mutate: Any) -> None:
    """Rewrite one sidecar only, re-sealing it, leaving the chain untouched."""
    target = paths.stream(stream_id).root / f"{chunk_id:06d}.commit.json"
    record = canonical_json.loads(target.read_bytes())
    record.pop(canonical_json.RECORD_HASH_KEY, None)
    mutate(record)
    target.write_bytes(canonical_json.seal_record(record))


# --- B1: canonical record identity -------------------------------------------


def test_b1_sidecar_with_explicit_null_payloads_is_rejected(data_root: DataRoot) -> None:
    """B1, the reviewer's exact attack.

    ``chunks.jsonl`` omits ``payloads`` as §12.2 requires. The sidecar adds
    ``"payloads": null``. Both parse to ``payloads=None``, so parsed-model
    equality called them identical while the on-disk records differ.
    """
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    paths = built.allocated.paths
    rewrite_sidecar(paths, SYN.stream_id, 0, lambda r: r.__setitem__("payloads", None))
    refresh_manifest(paths, SYN.stream_id)

    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2], "the package is self-consistent"
    assert not result.is_completed
    assert Finding.SIDECAR_MISMATCH in result.findings()


def test_c2_1_sidecar_with_an_extra_ignored_field_is_rejected(data_root: DataRoot) -> None:
    """C2-1. Minor-version tolerance ignores the field; identity must not."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1)
    paths = built.allocated.paths
    rewrite_sidecar(
        paths, EEG.stream_id, 0, lambda r: r.__setitem__("future_optional_field", "forged")
    )
    refresh_manifest(paths, EEG.stream_id)

    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2]
    assert not result.is_completed
    assert Finding.SIDECAR_MISMATCH in result.findings()


def test_c2_2_chain_record_with_an_extra_ignored_field_is_rejected(
    data_root: DataRoot,
) -> None:
    """C2-2. The mirror image: the extra field is on the chain, not the sidecar."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    paths = built.allocated.paths
    forge_chain(
        paths,
        EEG.stream_id,
        lambda chunk_id, record: (
            record.__setitem__("future_optional_field", "forged") if chunk_id == 0 else None
        ),
        rewrite_sidecars=False,
    )
    refresh_manifest(paths, EEG.stream_id)

    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2]
    assert not result.is_completed
    assert Finding.SIDECAR_MISMATCH in result.findings()


def test_c2_3_matching_records_positive_control(data_root: DataRoot) -> None:
    """C2-3. Identical canonical records must still verify and complete."""
    built = build_session(
        data_root,
        streams=[EEG, SYN],
        required=(EEG.stream_id,),
        optional=(SYN.stream_id,),
        chunks=2,
    )
    assert verify_package(built.allocated.paths).is_completed


def test_parsed_models_alone_would_have_missed_these(data_root: DataRoot) -> None:
    """Pins the reason the fix is what it is, not merely that it works.

    Both documents parse to equal models; only the canonical bytes differ.
    """
    from consciousness_lab.session.model import ChunkCommit, load_on_disk

    base = {
        "chunk_id": "0",
        "prev_record_sha256": "0" * 64,
        "packets": {"path": "packets/000000.arrow", "sha256": "a" * 64, "bytes": "10"},
        "observations": {"path": "observations/000000.arrow", "sha256": "b" * 64, "bytes": "10"},
        "samples": {"path": "samples/000000.arrow", "sha256": "c" * 64, "bytes": "10"},
        "first_packet_seq": "0",
        "last_packet_seq": "2",
        "descriptor_sha256": "d" * 64,
    }
    with_null = dict(base, payloads=None)
    assert load_on_disk(ChunkCommit, dict(base)) == load_on_disk(ChunkCommit, with_null)
    assert canonical_json.canonicalize(base) != canonical_json.canonicalize(with_null)


# --- B2: physical packet truth ------------------------------------------------


def _forge_packet_range(
    paths: PackagePaths, stream_id: str, chunk_id: int, field: str, value: str
) -> None:
    """Forge one chunk's claimed packet range in both the chain and its sidecar.

    ``value`` must keep the record internally valid (``last >= first``),
    otherwise the record's own validator rejects it and the test would pass for
    the wrong reason rather than because physical rows disagree.
    """
    forge_chain(
        paths,
        stream_id,
        lambda cid, record: record.__setitem__(field, value) if cid == chunk_id else None,
    )
    refresh_manifest(paths, stream_id)


def test_b2_forged_last_packet_seq_in_chain_and_sidecar_is_rejected(
    data_root: DataRoot,
) -> None:
    """B2, the reviewer's exact attack: chain and sidecars agree, the data does not."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    paths = built.allocated.paths
    _forge_packet_range(paths, EEG.stream_id, 0, "last_packet_seq", "424242")

    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2], "metadata is mutually consistent"
    assert not result.is_completed
    assert Finding.PACKET_SUMMARY_MISMATCH in result.findings()


def test_c2_4_forged_first_packet_seq_is_rejected(data_root: DataRoot) -> None:
    """C2-4."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    # 1 instead of the real 0: still <= last_packet_seq, so the record remains
    # internally valid and only the physical rows can expose the lie.
    _forge_packet_range(built.allocated.paths, EEG.stream_id, 0, "first_packet_seq", "1")
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.PACKET_SUMMARY_MISMATCH in result.findings()


def test_c2_6_forged_middle_chunk_packet_range_is_rejected(data_root: DataRoot) -> None:
    """C2-6. Three chunks, only the middle one forged.

    The stream endpoints still look right, which is exactly why validation has
    to run per chunk rather than on the range alone.
    """
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=3)
    paths = built.allocated.paths
    _forge_packet_range(paths, EEG.stream_id, 1, "last_packet_seq", "424242")

    result = verify_package(paths)
    assert not result.is_completed
    issues = [i for i in result.issues if i.finding is Finding.PACKET_SUMMARY_MISMATCH]
    assert issues and "chunk 1" in issues[0].detail


def test_two_commits_cannot_claim_the_same_artifact(data_root: DataRoot) -> None:
    """B2's second construction: point two chunks at one artifact set."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    paths = built.allocated.paths

    def repoint(chunk_id: int, record: dict[str, Any]) -> None:
        if chunk_id != 1:
            return
        for kind in ("packets", "observations", "samples", "payloads"):
            if kind in record and record[kind] is not None:
                record[kind] = dict(record[kind])
                record[kind]["path"] = record[kind]["path"].replace("000001", "000000")

    forge_chain(paths, EEG.stream_id, repoint)
    refresh_manifest(paths, EEG.stream_id)

    result = verify_package(paths)
    assert not result.is_completed
    assert {Finding.ARTIFACT_PATH_NOT_CANONICAL, Finding.ARTIFACT_PATH_REUSED} & result.findings()


def test_c2_7_consistently_rewritten_packets_and_summaries_verify(
    data_root: DataRoot,
) -> None:
    """C2-7 positive control: when everything genuinely agrees, it passes.

    The packets artifact is rewritten in fixture code with a real, shifted
    packet range, and every dependent hash and summary is recomputed. This
    proves the new checks accept truth, not merely that they reject change.
    """
    from consciousness_lab.storage.arrow_schema import PACKETS_SCHEMA

    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    paths = built.allocated.paths
    packets_path = paths.stream(SYN.stream_id).root / "packets/000000.arrow"

    with packets_path.open("rb") as handle:
        rows = pa.ipc.open_stream(handle).read_all().to_pylist()
    shift = 1000
    for row in rows:
        row["packet_seq"] = int(row["packet_seq"]) + shift
    table = pa.Table.from_pylist(rows, schema=PACKETS_SCHEMA)
    with packets_path.open("wb") as sink, pa.ipc.new_stream(sink, PACKETS_SCHEMA) as writer:
        writer.write_table(table)

    def shift_range(chunk_id: int, record: dict[str, Any]) -> None:
        record["first_packet_seq"] = str(int(record["first_packet_seq"]) + shift)
        record["last_packet_seq"] = str(int(record["last_packet_seq"]) + shift)
        record["packets"] = dict(
            record["packets"],
            sha256=sha256_bytes(packets_path.read_bytes()),
            bytes=str(packets_path.stat().st_size),
        )

    forge_chain(paths, SYN.stream_id, shift_range)
    refresh_manifest(paths, SYN.stream_id)

    result = verify_package(paths)
    assert result.is_completed, [i.finding.value for i in result.issues]


# --- B3: finalizer preflight --------------------------------------------------


def _recording_writer(data_root: DataRoot, spec: SyntheticStreamSpec, chunks: int = 2):  # type: ignore[no-untyped-def]
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(Run(sealed_at=now_reading(), required_streams=[spec.stream_id]))
    writer.open_stream(build_descriptor(spec))
    source = SyntheticSource(spec, seed=4)
    for _ in range(chunks):
        writer.commit_chunk(spec.stream_id, source.next_chunk(3))
    return allocated, writer


def test_b3_finalizer_refuses_a_chain_rewritten_with_the_same_chunk_ids(
    data_root: DataRoot,
) -> None:
    """B3, the reviewer's exact attack. Chunk ids are unchanged, records are not."""
    allocated, writer = _recording_writer(data_root, EEG)
    forge_chain(
        allocated.paths,
        EEG.stream_id,
        lambda cid, record: record.__setitem__("last_packet_seq", "424242") if cid == 0 else None,
    )
    with pytest.raises(FinalizationError, match="not the record this writer committed"):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)


def test_c2_8_finalizer_refuses_a_canonically_altered_sidecar(data_root: DataRoot) -> None:
    """C2-8, first variant: sidecar altered, chain untouched."""
    allocated, writer = _recording_writer(data_root, EEG)
    rewrite_sidecar(
        allocated.paths, EEG.stream_id, 0, lambda r: r.__setitem__("future_optional_field", "x")
    )
    with pytest.raises(FinalizationError, match="same on-disk record"):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)


def test_finalizer_refuses_a_chain_holding_chunks_it_never_committed(
    data_root: DataRoot,
) -> None:
    """B3's other half: extra disk chunk ids must be rejected too."""
    allocated, writer = _recording_writer(data_root, EEG, chunks=1)
    stream = allocated.paths.stream(EEG.stream_id)
    line = stream.chunks_index.read_bytes().strip()
    record = canonical_json.loads(line)
    record.pop(canonical_json.RECORD_HASH_KEY, None)
    record["chunk_id"] = "9"
    record["prev_record_sha256"] = str(canonical_json.loads(line)[canonical_json.RECORD_HASH_KEY])
    sealed = canonical_json.seal_record(record)
    stream.chunks_index.write_bytes(line + b"\n" + sealed + b"\n")
    (stream.root / "000009.commit.json").write_bytes(sealed)

    with pytest.raises(FinalizationError, match="never committed"):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)


def test_finalizer_refuses_when_packet_rows_contradict_the_summary(
    data_root: DataRoot,
) -> None:
    """The finalizer uses the same physical truth as the verifier."""
    allocated, writer = _recording_writer(data_root, EEG, chunks=1)
    # Forge only the sidecar's twin on disk via the chain, keeping writer memory
    # stale, then confirm the preflight catches the physical disagreement.
    forge_chain(
        allocated.paths,
        EEG.stream_id,
        lambda cid, record: record.__setitem__("first_packet_seq", "777"),
    )
    with pytest.raises(FinalizationError):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)


# --- C2-9 / C2-10 are the existing suites, which must keep passing -----------


def test_c2_9_and_c2_10_zero_chunk_stream_remains_valid(data_root: DataRoot) -> None:
    """No minimum packet, chunk, sample or duration rule was introduced."""
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(Run(sealed_at=now_reading(), required_streams=[EEG.stream_id]))
    writer.open_stream(build_descriptor(EEG))
    finalize(writer, outcome=RecordingOutcome.COMPLETED)

    result = verify_package(allocated.paths)
    assert result.is_completed
    assert result.manifest is not None
    entry = result.manifest.streams[0]
    assert entry.chunk_count == 0
    assert entry.first_packet_seq is None and entry.last_packet_seq is None
