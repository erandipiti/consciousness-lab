"""One test per referential relation V01-V14 of `PACKAGE_INTEGRITY_V2.md`.

Each test violates exactly one relation in the **physical bytes** and asserts
the verifier rejects the package for that reason. Where a forger could restore a
hash, the test restores it — via ``rewrite_chunk_chain`` and ``reseal_manifest``
— so what survives is the relation itself, not an incidental hash mismatch.
"""

import hashlib
from pathlib import Path
from typing import Any

import pyarrow as pa

from consciousness_lab.session.model import RawCaptureLevel, SampleLayout
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.arrow_schema import PACKETS_SCHEMA
from consciousness_lab.storage.checksums import sha256_file
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.payload import frame
from consciousness_lab.storage.verifier import Finding, verify_package
from consciousness_lab.synthetic.source import SyntheticStreamSpec
from tests.conftest import (
    build_session,
    read_chunk_records,
    reseal_manifest,
    rewrite_chunk_chain,
)

STREAM = "synthetic.eeg"


def _rewrite_arrow(path: Path, schema: pa.Schema, rows: list[dict[str, Any]]) -> None:
    table = pa.Table.from_pylist(rows, schema=schema)
    with path.open("wb") as sink, pa.ipc.new_stream(sink, schema) as writer:
        writer.write_table(table)


def _packets(paths: PackagePaths, chunk: int = 0) -> tuple[Path, list[dict[str, Any]]]:
    path = paths.stream(STREAM).root / f"packets/{chunk:06d}.arrow"
    with path.open("rb") as handle:
        return path, pa.ipc.open_stream(handle).read_all().to_pylist()


def _reseal_chunk(paths: PackagePaths, chunk_id: int) -> None:
    """Restore every hash the attacker can recompute after editing an artifact."""
    records = read_chunk_records(paths, STREAM)
    stream_root = paths.stream(STREAM).root
    for record in records:
        if int(record["chunk_id"]) != chunk_id:
            continue
        for kind in record["artifact_sha256"]:
            suffix = "bin" if kind == "payloads" else "arrow"
            record["artifact_sha256"][kind] = sha256_file(
                stream_root / f"{kind}/{chunk_id:06d}.{suffix}"
            )
    rewrite_chunk_chain(paths, STREAM, records)
    reseal_manifest(paths)


def test_v01_a_broken_hash_chain_is_rejected(data_root: DataRoot) -> None:
    built = build_session(data_root, chunks=3)
    paths = built.allocated.paths
    records = read_chunk_records(paths, STREAM)
    # Rewrite WITHOUT recomputing the chain: point record 2 at nothing.
    records[2]["prev_record_sha256"] = "0" * 64
    lines = [canonical_json.canonicalize(r) for r in records]
    paths.stream(STREAM).chunks_index.write_bytes(b"".join(line + b"\n" for line in lines))
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_CHUNK_CHAIN in result.findings()


def test_v02_chunk_id_must_strictly_increase(data_root: DataRoot) -> None:
    built = build_session(data_root, chunks=3)
    paths = built.allocated.paths
    records = read_chunk_records(paths, STREAM)
    records[1], records[2] = records[2], records[1]
    rewrite_chunk_chain(paths, STREAM, records)
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.CHAIN_ORDER_INVALID in result.findings()


def test_v03_a_committed_artifact_must_exist_and_hash_right(data_root: DataRoot) -> None:
    built = build_session(data_root, chunks=2)
    paths = built.allocated.paths
    (paths.stream(STREAM).root / "samples/000001.arrow").unlink()
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.CHUNK_ARTIFACT_INVALID in result.findings()


def test_v03b_a_swapped_artifact_with_a_recomputed_hash_is_still_rejected(
    data_root: DataRoot,
) -> None:
    """The hash can be recomputed; the deterministic path cannot be renegotiated."""
    built = build_session(data_root, chunks=2)
    paths = built.allocated.paths
    stream_root = paths.stream(STREAM).root
    (stream_root / "packets/000000.arrow").write_bytes(
        (stream_root / "packets/000001.arrow").read_bytes()
    )
    _reseal_chunk(paths, 0)
    result = verify_package(paths)
    assert not result.is_completed
    # Chunk 0 now physically holds chunk 1's packets, so the chain ordering and
    # the samples key identity both contradict it.
    assert result.findings() & {
        Finding.CHAIN_ORDER_INVALID,
        Finding.ROW_REFERENCE_INVALID,
    }


def test_v04_an_uncommitted_raw_artifact_is_an_orphan(data_root: DataRoot) -> None:
    built = build_session(data_root, chunks=1)
    paths = built.allocated.paths
    (paths.stream(STREAM).root / "samples/000999.arrow").write_bytes(b"orphan")
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ORPHAN_FILE in result.findings()


def test_v05_the_artifact_key_set_must_match_the_capture_level(
    data_root: DataRoot,
) -> None:
    built = build_session(
        data_root,
        streams=[SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED)],
        required=("synthetic.ecg",),
        chunks=1,
    )
    paths = built.allocated.paths
    records = read_chunk_records(paths, "synthetic.ecg")
    records[0]["artifact_sha256"]["payloads"] = "0" * 64
    rewrite_chunk_chain(paths, "synthetic.ecg", records)
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.CHUNK_ARTIFACT_INVALID in result.findings()


def test_v06_packet_seq_must_strictly_increase_within_a_chunk(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root, chunks=1, packets_per_chunk=4)
    paths = built.allocated.paths
    path, rows = _packets(paths)
    rows[1], rows[2] = rows[2], rows[1]
    _rewrite_arrow(path, PACKETS_SCHEMA, rows)
    _reseal_chunk(paths, 0)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.UNREADABLE_PACKETS_ARTIFACT in result.findings()


def test_v07_a_sample_referencing_no_packet_is_rejected(data_root: DataRoot) -> None:
    built = build_session(data_root, chunks=1, packets_per_chunk=2)
    paths = built.allocated.paths
    samples_path = paths.stream(STREAM).root / "samples/000000.arrow"
    with samples_path.open("rb") as handle:
        table = pa.ipc.open_stream(handle).read_all()
    rows = table.to_pylist()
    rows[0]["packet_seq"] = 424242
    _rewrite_arrow(samples_path, table.schema, rows)
    _reseal_chunk(paths, 0)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_v07b_a_deleted_sample_row_is_rejected(data_root: DataRoot) -> None:
    """Dense key identity is a multiset: a set comparison would miss this."""
    built = build_session(data_root, chunks=1, packets_per_chunk=2)
    paths = built.allocated.paths
    samples_path = paths.stream(STREAM).root / "samples/000000.arrow"
    with samples_path.open("rb") as handle:
        table = pa.ipc.open_stream(handle).read_all()
    rows = table.to_pylist()
    _rewrite_arrow(samples_path, table.schema, rows[:-1])
    _reseal_chunk(paths, 0)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_v08_a_frame_with_no_packet_row_is_rejected(data_root: DataRoot) -> None:
    """Bijection by identity: no pointer to keep in step, only a comparison."""
    built = build_session(data_root, chunks=1)
    paths = built.allocated.paths
    payload_path = paths.stream(STREAM).root / "payloads/000000.bin"
    payload_path.write_bytes(payload_path.read_bytes() + frame(999_999, b"x"))
    _reseal_chunk(paths, 0)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.PAYLOAD_FRAMING_INVALID in result.findings()


def test_v08b_a_packet_with_no_frame_is_rejected(data_root: DataRoot) -> None:
    built = build_session(data_root, chunks=1, packets_per_chunk=3)
    paths = built.allocated.paths
    payload_path = paths.stream(STREAM).root / "payloads/000000.bin"
    from consciousness_lab.storage.payload import iter_frames

    frames, _ = iter_frames(payload_path.read_bytes())
    payload_path.write_bytes(b"".join(frame(f.packet_seq, f.payload) for f in frames[:-1]))
    _reseal_chunk(paths, 0)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.PAYLOAD_FRAMING_INVALID in result.findings()


def test_v09_an_undeclared_physical_stream_is_rejected(data_root: DataRoot) -> None:
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec(STREAM, RawCaptureLevel.TRANSPORT_PAYLOAD),
            SyntheticStreamSpec("synthetic.rogue", RawCaptureLevel.SYNTHETIC),
        ],
        required=(STREAM,),
        finalize_outcome=None,
    )
    paths = built.allocated.paths
    from consciousness_lab.session.finalizer import finalize
    from consciousness_lab.session.model import RecordingOutcome

    finalize(built.writer, outcome=RecordingOutcome.COMPLETED)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.STREAM_NOT_DECLARED in result.findings()


def test_v10_a_control_file_missing_from_the_seal_is_rejected(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.manifest.read_bytes())
    del obj["control_sha256"][f"raw/{STREAM}/stream_close.json"]
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(hashlib.sha256(body).hexdigest() + "\n", encoding="utf-8")
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.CONTROL_SET_MISMATCH in result.findings()


def test_v10b_a_control_key_outside_the_derived_set_is_rejected(
    data_root: DataRoot,
) -> None:
    """Equality in both directions: a manifest cannot seal a path v2 never defines."""
    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj["control_sha256"]["logs/extra.txt"] = "0" * 64
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(hashlib.sha256(body).hexdigest() + "\n", encoding="utf-8")
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.CONTROL_SET_MISMATCH in result.findings()


def test_v11_a_torn_tail_invalidates_a_finalized_log(data_root: DataRoot) -> None:
    built = build_session(data_root, chunks=2)
    paths = built.allocated.paths
    index = paths.stream(STREAM).chunks_index
    index.write_bytes(index.read_bytes() + b'{"chunk_id":"2"')
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_CHUNK_CHAIN in result.findings()


def test_v12_a_noncanonical_but_equivalent_record_is_rejected(
    data_root: DataRoot,
) -> None:
    """ "It parses and would canonicalize alike" is not sufficient (§9.3)."""
    built = build_session(data_root, chunks=1)
    paths = built.allocated.paths
    records = read_chunk_records(paths, STREAM)
    # Same content, different spelling: keys in a non-JCS order, with spaces.
    import json

    spelled = json.dumps(records[0], indent=1).encode("utf-8")
    paths.stream(STREAM).chunks_index.write_bytes(spelled + b"\n")
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_CHUNK_CHAIN in result.findings()


def test_v13_an_unexpected_immutable_file_invalidates_the_package(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    (paths.root / "mystery.bin").write_bytes(b"unexpected")
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.UNEXPECTED_FILE in result.findings()


def test_v14_a_tampered_lifecycle_record_hash_is_rejected(data_root: DataRoot) -> None:
    """The pre-seal layer, kept because recovery reads exactly these records."""
    built = build_session(data_root)
    paths = built.allocated.paths
    raw = paths.lifecycle.read_bytes()
    tampered = raw.replace(b'"actor":"system"', b'"actor":"attack"', 1)
    assert tampered != raw
    paths.lifecycle.write_bytes(tampered)
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_LIFECYCLE_SEAL in result.findings()


def test_sparse_layout_enforces_the_v2_triple(data_root: DataRoot) -> None:
    """D32: ``(packet_seq, sample_index_in_packet, channel_id)``, unique."""
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec(
                "synthetic.sparse",
                RawCaptureLevel.SYNTHETIC,
                layout=SampleLayout.SPARSE_LONG,
            )
        ],
        required=("synthetic.sparse",),
        chunks=1,
        packets_per_chunk=2,
    )
    paths = built.allocated.paths
    assert verify_package(paths).is_completed

    samples_path = paths.stream("synthetic.sparse").root / "samples/000000.arrow"
    with samples_path.open("rb") as handle:
        table = pa.ipc.open_stream(handle).read_all()
    rows = table.to_pylist()
    rows.append(dict(rows[0]))  # duplicate the triple
    _rewrite_arrow(samples_path, table.schema, rows)
    records = read_chunk_records(paths, "synthetic.sparse")
    records[0]["artifact_sha256"]["samples"] = sha256_file(samples_path)
    rewrite_chunk_chain(paths, "synthetic.sparse", records)
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_sparse_samples_must_name_a_channel_in_the_descriptor(
    data_root: DataRoot,
) -> None:
    """D32: ``channel_id`` exists in ``descriptor.channels``.

    Coverage is deliberately NOT required — sparse means absence may be
    meaningful — but a channel the descriptor never declared is a reference to
    nothing, which is structural.
    """
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec(
                "synthetic.sparse",
                RawCaptureLevel.SYNTHETIC,
                layout=SampleLayout.SPARSE_LONG,
            )
        ],
        required=("synthetic.sparse",),
        chunks=1,
        packets_per_chunk=2,
    )
    paths = built.allocated.paths
    samples_path = paths.stream("synthetic.sparse").root / "samples/000000.arrow"
    with samples_path.open("rb") as handle:
        table = pa.ipc.open_stream(handle).read_all()
    rows = table.to_pylist()
    rows[0]["channel_id"] = "ch_never_declared"
    _rewrite_arrow(samples_path, table.schema, rows)
    records = read_chunk_records(paths, "synthetic.sparse")
    records[0]["artifact_sha256"]["samples"] = sha256_file(samples_path)
    rewrite_chunk_chain(paths, "synthetic.sparse", records)
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_sparse_coverage_is_deliberately_not_required(data_root: DataRoot) -> None:
    """No rule demands ``n_channels`` rows per sample position (D32)."""
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec(
                "synthetic.sparse",
                RawCaptureLevel.SYNTHETIC,
                layout=SampleLayout.SPARSE_LONG,
            )
        ],
        required=("synthetic.sparse",),
        chunks=1,
        packets_per_chunk=2,
    )
    paths = built.allocated.paths
    samples_path = paths.stream("synthetic.sparse").root / "samples/000000.arrow"
    with samples_path.open("rb") as handle:
        table = pa.ipc.open_stream(handle).read_all()
    rows = [r for r in table.to_pylist() if r["channel_id"] == "ch0"]
    assert rows, "the fixture has more than one channel"
    _rewrite_arrow(samples_path, table.schema, rows)
    records = read_chunk_records(paths, "synthetic.sparse")
    records[0]["artifact_sha256"]["samples"] = sha256_file(samples_path)
    rewrite_chunk_chain(paths, "synthetic.sparse", records)
    reseal_manifest(paths)
    assert verify_package(paths).is_completed, "absence is allowed to be meaningful"


def test_pre_seal_record_hashes_verify_before_any_manifest_exists(
    data_root: DataRoot,
) -> None:
    """D34: this is the only integrity over those logs during the pre-seal window.

    The package here has never been finalized, so there is no whole-file seal
    anywhere — which is exactly the window recovery reads them in.
    """
    from consciousness_lab.session.model import EventRecord, LifecycleRecord
    from consciousness_lab.storage import package_layout

    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    assert not paths.manifest.exists()

    for path, model in ((paths.lifecycle, LifecycleRecord), (paths.events, EventRecord)):
        raw = path.read_bytes()
        assert package_layout.verify_jsonl_region(raw, model, require_record_hash=True) is None
        tampered = raw.replace(b'"actor":"system"', b'"actor":"forged"', 1)
        if tampered == raw:
            tampered = raw.replace(b'"origin":"system"', b'"origin":"forged"', 1)
        assert tampered != raw
        error = package_layout.verify_jsonl_region(tampered, model, require_record_hash=True)
        assert error is not None and "record_sha256" in error
