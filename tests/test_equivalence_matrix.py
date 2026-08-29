"""CL-002B-R1-C3: the chunk representation equivalence matrix.

Three review cycles closed one attack each and left an adjacent state of the
same relation open. These tests enumerate the relations instead:
``docs/CHUNK_EQUIVALENCE.md`` is the map, this file is its coverage.

Two layers:

* **E1-E25** name high-value attack classes so a failure says what broke.
* a **property-based mutation** sweep generates structurally valid adversarial
  edits across the matrix dimensions, so a state nobody thought to name is still
  covered.

Every negative case recomputes all attacker-controlled metadata — record hashes,
chain links, sidecars, manifest summaries, inventory and ``manifest.sha256`` —
so none passes merely because a hash was left stale.
"""

from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from consciousness_lab.session.allocator import allocate_session
from consciousness_lab.session.finalizer import FinalizationError, finalize
from consciousness_lab.session.model import (
    RawCaptureLevel,
    RecordingOutcome,
    Run,
    SampleLayout,
)
from consciousness_lab.session.writer import SessionWriter
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.arrow_schema import (
    OBSERVATIONS_SCHEMA,
    PACKETS_SCHEMA,
    samples_schema,
)
from consciousness_lab.storage.checksums import sha256_bytes
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.verifier import Finding, verify_package
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSpec,
    build_descriptor,
)
from tests.conftest import build_session, now_reading
from tests.test_canonical_and_physical import (
    forge_chain,
    refresh_manifest,
    rewrite_sidecar,
)
from tests.test_leaf_integrity import (
    reseal_artifacts as _reseal_artifacts,
)
from tests.test_leaf_integrity import (
    rewrite_arrow as _rewrite_arrow,
)

EEG = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD)
SYN = SyntheticStreamSpec("synthetic.imu", RawCaptureLevel.SYNTHETIC)
DEC = SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED)


def completed(paths: PackagePaths) -> bool:
    return verify_package(paths).is_completed


# =============================================================================
# E1-E7 — canonical record identity and the nullability matrix
# =============================================================================


def test_e1_sidecar_exact_canonical_match(data_root: DataRoot) -> None:
    """E1 positive control."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    assert completed(built.allocated.paths)


def test_e2_sidecar_extra_ignored_field(data_root: DataRoot) -> None:
    """E2. Minor-version tolerance ignores it; record identity must not."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1)
    rewrite_sidecar(built.allocated.paths, EEG.stream_id, 0, lambda r: r.__setitem__("extra", "x"))
    refresh_manifest(built.allocated.paths, EEG.stream_id)
    assert not completed(built.allocated.paths)


def test_e3_chain_extra_ignored_field_only(data_root: DataRoot) -> None:
    """E3. The mirror of E2."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    forge_chain(
        built.allocated.paths,
        EEG.stream_id,
        lambda cid, r: r.__setitem__("extra", "x") if cid == 0 else None,
        rewrite_sidecars=False,
    )
    refresh_manifest(built.allocated.paths, EEG.stream_id)
    assert not completed(built.allocated.paths)


def test_e4_sidecar_null_vs_chain_value(data_root: DataRoot) -> None:
    """E4. Asymmetric null, sidecar side."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1)
    rewrite_sidecar(
        built.allocated.paths, EEG.stream_id, 0, lambda r: r.__setitem__("payloads", None)
    )
    refresh_manifest(built.allocated.paths, EEG.stream_id)
    assert not completed(built.allocated.paths)


def test_e5_sidecar_value_vs_chain_null(data_root: DataRoot) -> None:
    """E5. Asymmetric null, chain side."""
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    rewrite_sidecar(
        built.allocated.paths,
        SYN.stream_id,
        0,
        lambda r: r.__setitem__(
            "payloads", {"path": "payloads/000000.bin", "sha256": "0" * 64, "bytes": "1"}
        ),
    )
    refresh_manifest(built.allocated.paths, SYN.stream_id)
    assert not completed(built.allocated.paths)


def test_e6_both_omit_the_key_where_that_is_valid(data_root: DataRoot) -> None:
    """E6 positive: at synthetic/library_decoded the key is absent on both sides."""
    for spec in (SYN, DEC):
        built = build_session(data_root, streams=[spec], required=(spec.stream_id,), chunks=1)
        assert completed(built.allocated.paths), spec.stream_id


@pytest.mark.parametrize("spec", [SYN, DEC], ids=lambda s: s.stream_id)
def test_e7_symmetric_null_where_the_key_must_be_absent(
    data_root: DataRoot, spec: SyntheticStreamSpec
) -> None:
    """E7. Both sides agree on a value §12.2 forbids — pairwise equality is blind.

    This is the state that survived the previous round: the two copies match
    each other, so record identity passes, and only key *presence* catches it.
    """
    built = build_session(data_root, streams=[spec], required=(spec.stream_id,), chunks=1)
    paths = built.allocated.paths
    forge_chain(paths, spec.stream_id, lambda cid, r: r.__setitem__("payloads", None))
    refresh_manifest(paths, spec.stream_id)

    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2], "package is self-consistent"
    assert not result.is_completed
    assert Finding.PAYLOAD_KEY_PRESENCE_INVALID in result.findings()


# =============================================================================
# E8-E13 — packet range, position and chain order
# =============================================================================


def _forge(paths: PackagePaths, stream_id: str, chunk_id: int, field: str, value: str) -> None:
    forge_chain(
        paths,
        stream_id,
        lambda cid, r: r.__setitem__(field, value) if cid == chunk_id else None,
    )
    refresh_manifest(paths, stream_id)


def test_e8_e9_e10_forged_packet_range_with_everything_recomputed(
    data_root: DataRoot,
) -> None:
    """E8/E9/E10 collapse into one: chain, sidecars and manifest all agree, data does not."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    paths = built.allocated.paths
    _forge(paths, EEG.stream_id, 0, "last_packet_seq", "424242")
    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2]
    assert not result.is_completed
    assert Finding.PACKET_SUMMARY_MISMATCH in result.findings()


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_e11_forged_chunk_at_each_position(data_root: DataRoot, position: str) -> None:
    """E11. Per-chunk validation must cover first, middle and last alike."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=3)
    target = {"first": 0, "middle": 1, "last": 2}[position]
    _forge(built.allocated.paths, EEG.stream_id, target, "last_packet_seq", "424242")
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert any(f"chunk {target}" in i.detail for i in result.issues)


def test_e12_reordered_chain(data_root: DataRoot) -> None:
    """E12. Every chunk still matches its own artifact; the chain does not.

    This is the state that survived the previous round: per-chunk validation
    passed because reordering breaks nothing per chunk.
    """
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=3)
    paths = built.allocated.paths
    stream = paths.stream(EEG.stream_id)
    records = [
        canonical_json.loads(line)
        for line in stream.chunks_index.read_bytes().split(b"\n")
        if line.strip()
    ]
    prev = canonical_json.ZERO_HASH
    lines: list[bytes] = []
    for record in reversed(records):
        record.pop(canonical_json.RECORD_HASH_KEY, None)
        record["prev_record_sha256"] = prev
        digest = canonical_json.record_hash(record)
        prev = digest
        body = canonical_json.canonicalize(dict(record, record_sha256=digest))
        lines.append(body + b"\n")
        (stream.root / f"{int(record['chunk_id']):06d}.commit.json").write_bytes(body)
    stream.chunks_index.write_bytes(b"".join(lines))
    refresh_manifest(paths, EEG.stream_id)

    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2], "package is self-consistent"
    assert not result.is_completed
    assert Finding.CHAIN_ORDER_INVALID in result.findings()


def test_e12b_adjacent_swap(data_root: DataRoot) -> None:
    """E12 variant: swap two adjacent chunks rather than reversing everything."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=3)
    paths = built.allocated.paths
    stream = paths.stream(EEG.stream_id)
    records = [
        canonical_json.loads(line)
        for line in stream.chunks_index.read_bytes().split(b"\n")
        if line.strip()
    ]
    records[0], records[1] = records[1], records[0]
    prev = canonical_json.ZERO_HASH
    lines: list[bytes] = []
    for record in records:
        record.pop(canonical_json.RECORD_HASH_KEY, None)
        record["prev_record_sha256"] = prev
        digest = canonical_json.record_hash(record)
        prev = digest
        body = canonical_json.canonicalize(dict(record, record_sha256=digest))
        lines.append(body + b"\n")
        (stream.root / f"{int(record['chunk_id']):06d}.commit.json").write_bytes(body)
    stream.chunks_index.write_bytes(b"".join(lines))
    refresh_manifest(paths, EEG.stream_id)
    assert not completed(paths)


def test_e13_duplicate_chunk_id(data_root: DataRoot) -> None:
    """E13. The same chunk id twice in the chain."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    paths = built.allocated.paths
    forge_chain(paths, EEG.stream_id, lambda cid, r: r.__setitem__("chunk_id", "0"))
    refresh_manifest(paths, EEG.stream_id)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.CHAIN_ORDER_INVALID in result.findings()


# =============================================================================
# E14-E18 — collection membership and cardinality
# =============================================================================


def test_e14_missing_middle_sidecar(data_root: DataRoot) -> None:
    """E14."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=3)
    (built.allocated.paths.stream(EEG.stream_id).root / "000001.commit.json").unlink()
    refresh_manifest(built.allocated.paths, EEG.stream_id)
    assert not completed(built.allocated.paths)


def test_e15_extra_sidecar(data_root: DataRoot) -> None:
    """E15."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    root = built.allocated.paths.stream(EEG.stream_id).root
    (root / "000009.commit.json").write_bytes((root / "000000.commit.json").read_bytes())
    refresh_manifest(built.allocated.paths, EEG.stream_id)
    assert not completed(built.allocated.paths)


@pytest.mark.parametrize("chunks", [0, 1, 2, 3, 5])
def test_e16_e17_e18_zero_one_and_many_chunks(data_root: DataRoot, chunks: int) -> None:
    """E16/E17/E18. Cardinality positives, including the zero-chunk case."""
    if chunks == 0:
        allocated = allocate_session(data_root, participant_pseudonym="P001")
        writer = SessionWriter.open(allocated.paths)
        writer.start_recording(Run(sealed_at=now_reading(), required_streams=[EEG.stream_id]))
        writer.open_stream(build_descriptor(EEG))
        finalize(writer, outcome=RecordingOutcome.COMPLETED)
        result = verify_package(allocated.paths)
        assert result.is_completed, "no minimum chunk rule exists"
        assert result.manifest is not None
        entry = result.manifest.streams[0]
        assert entry.chunk_count == 0
        assert entry.chunk_chain_head_sha256 is None
        assert entry.first_packet_seq is None and entry.last_packet_seq is None
        return
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=chunks)
    assert completed(built.allocated.paths)


# =============================================================================
# E19-E22 — the payload matrix
# =============================================================================


def test_e19_transport_payload_full_valid_relation(data_root: DataRoot) -> None:
    """E19 positive control."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=2)
    assert completed(built.allocated.paths)


def test_e20_transport_payload_symmetric_null(data_root: DataRoot) -> None:
    """E20. Both copies drop the payload entry where it is required."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1)
    paths = built.allocated.paths
    forge_chain(paths, EEG.stream_id, lambda cid, r: r.pop("payloads", None))
    refresh_manifest(paths, EEG.stream_id)
    result = verify_package(paths)
    assert not result.is_completed
    assert {
        Finding.PAYLOAD_KEY_PRESENCE_INVALID,
        Finding.MISSING_PAYLOAD_ARTIFACT,
    } & result.findings()


def test_e21_decoded_and_synthetic_all_null(data_root: DataRoot) -> None:
    """E21 positive control across both non-payload capture levels."""
    built = build_session(
        data_root,
        streams=[SYN, DEC],
        required=(SYN.stream_id,),
        optional=(DEC.stream_id,),
        chunks=2,
    )
    assert completed(built.allocated.paths)


def test_e22_unexpected_payload_artifact(data_root: DataRoot) -> None:
    """E22."""
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    forged = built.allocated.paths.stream(SYN.stream_id).root / "payloads" / "000000.bin"
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_bytes(b"fabricated")
    refresh_manifest(built.allocated.paths, SYN.stream_id)
    assert not completed(built.allocated.paths)


# =============================================================================
# E23-E25 — manifest and stream-set relations
# =============================================================================


def test_e23_manifest_fully_consistent(data_root: DataRoot) -> None:
    """E23 positive control, multi-stream and mixed capture levels."""
    built = build_session(
        data_root,
        streams=[EEG, DEC, SYN],
        required=(EEG.stream_id, DEC.stream_id),
        optional=(SYN.stream_id,),
        chunks=2,
    )
    assert completed(built.allocated.paths)


def test_e24_required_stream_physically_deleted(data_root: DataRoot) -> None:
    """E24."""
    import shutil

    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,))
    shutil.rmtree(built.allocated.paths.raw / EEG.stream_id)
    assert not completed(built.allocated.paths)


def test_e25_raw_stream_omitted_from_manifest(data_root: DataRoot) -> None:
    """E25."""
    built = build_session(
        data_root, streams=[EEG, SYN], required=(EEG.stream_id,), optional=(SYN.stream_id,)
    )
    obj = canonical_json.loads(built.allocated.paths.manifest.read_bytes())
    obj["streams"] = [s for s in obj["streams"] if s["stream_id"] != SYN.stream_id]
    body = canonical_json.canonicalize(obj)
    built.allocated.paths.manifest.write_bytes(body)
    built.allocated.paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")
    assert not completed(built.allocated.paths)


# =============================================================================
# Structural foreign keys (R20-R23)
# =============================================================================


def test_samples_referencing_a_packet_outside_the_chunk(data_root: DataRoot) -> None:
    """R20. A sample row pointing at a packet this chunk does not contain."""
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    paths = built.allocated.paths
    target = paths.stream(SYN.stream_id).root / "samples/000000.arrow"
    _rewrite_arrow(
        target,
        samples_schema(SampleLayout.DENSE_FIXED_LIST, SYN.n_channels),
        lambda rows: rows[0].__setitem__("packet_seq", 999999),
    )
    _reseal_artifacts(paths, SYN.stream_id)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_sample_index_outside_declared_n_samples(data_root: DataRoot) -> None:
    """R21. n_samples is "samples carried in this packet"; the index must fit."""
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    paths = built.allocated.paths
    target = paths.stream(SYN.stream_id).root / "samples/000000.arrow"
    _rewrite_arrow(
        target,
        samples_schema(SampleLayout.DENSE_FIXED_LIST, SYN.n_channels),
        lambda rows: rows[0].__setitem__("sample_index_in_packet", 9999),
    )
    _reseal_artifacts(paths, SYN.stream_id)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_observation_referencing_a_packet_outside_the_chunk(data_root: DataRoot) -> None:
    """R22."""
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    paths = built.allocated.paths
    target = paths.stream(SYN.stream_id).root / "observations/000000.arrow"
    _rewrite_arrow(
        target, OBSERVATIONS_SCHEMA, lambda rows: rows[0].__setitem__("packet_seq", 999999)
    )
    _reseal_artifacts(paths, SYN.stream_id)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_packets_not_strictly_increasing_within_a_chunk(data_root: DataRoot) -> None:
    """R19. §9.1: packet_seq is strictly increasing at arrival."""
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    paths = built.allocated.paths
    target = paths.stream(SYN.stream_id).root / "packets/000000.arrow"
    _rewrite_arrow(target, PACKETS_SCHEMA, lambda rows: rows.reverse())
    _reseal_artifacts(paths, SYN.stream_id)
    assert not completed(paths)


# =============================================================================
# Property-based mutation sweep
# =============================================================================

#: Mutation dimensions from the matrix. Each generates a structurally valid but
#: semantically false edit, with every attacker-controlled hash recomputed.
MUTATIONS = [
    "sidecar_extra_field",
    "sidecar_drop_payloads_key",
    "chain_extra_field",
    "chain_first_packet_seq",
    "chain_last_packet_seq",
    "chain_descriptor_hash",
    "chain_artifact_path",
    "chain_artifact_hash",
    "chain_payloads_null",
    "manifest_chunk_count",
    "manifest_chain_head",
    "manifest_packet_range",
    "manifest_descriptor_hash",
    "sidecar_delete",
    "sidecar_duplicate",
    # --- C4 leaf-level dimensions ---------------------------------------
    "payload_frame_packet_seq",
    "payload_ref_offset",
    "payload_ref_length",
    "payload_ref_file",
    "artifact_bytes",
    "artifact_sha",
    "inventory_duplicate_path",
    "inventory_bytes",
    "sample_row_missing",
    "sample_row_duplicate",
    "sample_row_extra",
    "sample_row_wrong_packet",
    "sample_row_wrong_index",
]


def _apply_mutation(paths: PackagePaths, stream_id: str, kind: str, chunk_id: int) -> bool:
    """Apply one named mutation. Returns False when it does not apply here."""
    root = paths.stream(stream_id).root

    if kind == "sidecar_extra_field":
        rewrite_sidecar(paths, stream_id, chunk_id, lambda r: r.__setitem__("x", "1"))
    elif kind == "sidecar_drop_payloads_key":
        if not (root / f"payloads/{chunk_id:06d}.bin").exists():
            return False
        rewrite_sidecar(paths, stream_id, chunk_id, lambda r: r.pop("payloads", None))
    elif kind == "chain_extra_field":
        forge_chain(
            paths,
            stream_id,
            lambda cid, r: r.__setitem__("x", "1") if cid == chunk_id else None,
            rewrite_sidecars=False,
        )
    elif kind == "chain_first_packet_seq":
        forge_chain(
            paths,
            stream_id,
            lambda cid, r: (
                r.__setitem__("first_packet_seq", str(int(r["last_packet_seq"])))
                if cid == chunk_id
                else None
            ),
        )
    elif kind == "chain_last_packet_seq":
        forge_chain(
            paths,
            stream_id,
            lambda cid, r: r.__setitem__("last_packet_seq", "999999") if cid == chunk_id else None,
        )
    elif kind == "chain_descriptor_hash":
        forge_chain(
            paths,
            stream_id,
            lambda cid, r: (
                r.__setitem__("descriptor_sha256", "e" * 64) if cid == chunk_id else None
            ),
        )
    elif kind == "chain_artifact_path":
        forge_chain(
            paths,
            stream_id,
            lambda cid, r: (
                r.__setitem__("packets", dict(r["packets"], path="packets/000999.arrow"))
                if cid == chunk_id
                else None
            ),
        )
    elif kind == "chain_artifact_hash":
        forge_chain(
            paths,
            stream_id,
            lambda cid, r: (
                r.__setitem__("samples", dict(r["samples"], sha256="f" * 64))
                if cid == chunk_id
                else None
            ),
        )
    elif kind == "chain_payloads_null":
        if (root / f"payloads/{chunk_id:06d}.bin").exists():
            return False
        forge_chain(paths, stream_id, lambda cid, r: r.__setitem__("payloads", None))
    elif kind == "sidecar_delete":
        (root / f"{chunk_id:06d}.commit.json").unlink()
    elif kind == "sidecar_duplicate":
        source = root / f"{chunk_id:06d}.commit.json"
        (root / "000777.commit.json").write_bytes(source.read_bytes())
    elif kind in {
        "payload_frame_packet_seq",
        "payload_ref_offset",
        "payload_ref_length",
        "payload_ref_file",
    }:
        blob = root / f"payloads/{chunk_id:06d}.bin"
        if not blob.exists():
            return False
        if kind == "payload_frame_packet_seq":
            import struct as _struct

            from consciousness_lab.storage.payload import iter_frames

            data = bytearray(blob.read_bytes())
            frames, error = iter_frames(bytes(data))
            if error is not None or not frames:
                return False
            _struct.pack_into("<Q", data, frames[0].offset + 4, 8888888)
            blob.write_bytes(bytes(data))
        else:
            field = {
                "payload_ref_offset": "offset",
                "payload_ref_length": "length",
                "payload_ref_file": "file",
            }[kind]
            value: Any = {"offset": 3, "length": 1, "file": "payloads/999999.bin"}[field]
            _rewrite_arrow(
                root / f"packets/{chunk_id:06d}.arrow",
                PACKETS_SCHEMA,
                lambda rows: rows[0].__setitem__(
                    "payload_ref", dict(rows[0]["payload_ref"], **{field: value})
                ),
            )
        _reseal_artifacts(paths, stream_id)
        return True
    elif kind in {"artifact_bytes", "artifact_sha"}:
        field = "bytes" if kind == "artifact_bytes" else "sha256"
        replacement = "424242" if field == "bytes" else "a" * 64
        forge_chain(
            paths,
            stream_id,
            lambda cid, r: (
                r.__setitem__("packets", dict(r["packets"], **{field: replacement}))
                if cid == chunk_id
                else None
            ),
        )
        refresh_manifest(paths, stream_id)
        return True
    elif kind in {"inventory_duplicate_path", "inventory_bytes"}:
        obj = canonical_json.loads(paths.manifest.read_bytes())
        if kind == "inventory_duplicate_path":
            obj["inventory"].append(dict(obj["inventory"][0]))
        else:
            obj["inventory"][0]["bytes"] = "424242"
        body = canonical_json.canonicalize(obj)
        paths.manifest.write_bytes(body)
        paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")
        return True
    elif kind.startswith("sample_row_"):
        from consciousness_lab.storage.arrow_schema import samples_schema as _ss

        target = root / f"samples/{chunk_id:06d}.arrow"
        if not target.exists():
            return False
        spec_channels = 2
        mutators: dict[str, Any] = {
            "sample_row_missing": lambda rows: rows.pop(0),
            "sample_row_duplicate": lambda rows: rows.__setitem__(1, dict(rows[0])),
            "sample_row_extra": lambda rows: rows.append(dict(rows[0])),
            "sample_row_wrong_packet": lambda rows: rows[0].__setitem__("packet_seq", 987654),
            "sample_row_wrong_index": lambda rows: rows[0].__setitem__(
                "sample_index_in_packet", 4242
            ),
        }
        _rewrite_arrow(target, _ss(SampleLayout.DENSE_FIXED_LIST, spec_channels), mutators[kind])
        _reseal_artifacts(paths, stream_id)
        return True
    elif kind.startswith("manifest_"):
        obj = canonical_json.loads(paths.manifest.read_bytes())
        for entry in obj["streams"]:
            if entry["stream_id"] != stream_id:
                continue
            if kind == "manifest_chunk_count":
                entry["chunk_count"] = str(int(entry["chunk_count"]) + 1)
            elif kind == "manifest_chain_head":
                entry["chunk_chain_head_sha256"] = "b" * 64
            elif kind == "manifest_packet_range":
                entry["last_packet_seq"] = "999999"
            elif kind == "manifest_descriptor_hash":
                entry["descriptor_sha256"] = "c" * 64
        body = canonical_json.canonicalize(obj)
        paths.manifest.write_bytes(body)
        paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")
        return True
    else:  # pragma: no cover - guarded by the strategy
        raise AssertionError(kind)

    if not kind.startswith("manifest_"):
        refresh_manifest(paths, stream_id)
    return True


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    mutation=st.sampled_from(MUTATIONS),
    chunk_index=st.integers(min_value=0, max_value=2),
    spec_name=st.sampled_from(["transport", "synthetic"]),
)
def test_any_matrix_violation_prevents_completion(
    tmp_path_factory: pytest.TempPathFactory,
    mutation: str,
    chunk_index: int,
    spec_name: str,
) -> None:
    """The central property.

    Given a valid completed package, if ONE representation is mutated so that a
    required equivalence relation is violated — with every hash the attacker
    controls recomputed — ``is_completed`` must be False.

    The mutation name and inputs are the reproduction: a failure names the
    dimension that was not enforced.
    """
    spec = EEG if spec_name == "transport" else SYN
    data_root = DataRoot(tmp_path_factory.mktemp("prop") / "data")
    built = build_session(data_root, streams=[spec], required=(spec.stream_id,), chunks=3)
    paths = built.allocated.paths
    assert completed(paths), "the baseline package must verify before mutation"

    if not _apply_mutation(paths, spec.stream_id, mutation, chunk_index):
        return  # mutation does not apply to this capture level

    assert not completed(paths), (
        f"mutation {mutation!r} on chunk {chunk_index} of a {spec_name} stream "
        "left the package completed"
    )


# =============================================================================
# Finalizer parity — the same relations, pre-seal
# =============================================================================


def _recording(data_root: DataRoot, spec: SyntheticStreamSpec, chunks: int = 3):  # type: ignore[no-untyped-def]
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(Run(sealed_at=now_reading(), required_streams=[spec.stream_id]))
    writer.open_stream(build_descriptor(spec))
    source = SyntheticSource(spec, seed=9)
    for _ in range(chunks):
        writer.commit_chunk(spec.stream_id, source.next_chunk(3))
    return allocated, writer


def test_finalizer_refuses_a_reordered_chain(data_root: DataRoot) -> None:
    """Chain ordering is checked pre-seal, not left to the verifier."""
    allocated, writer = _recording(data_root, EEG)
    stream = allocated.paths.stream(EEG.stream_id)
    records = [
        canonical_json.loads(line)
        for line in stream.chunks_index.read_bytes().split(b"\n")
        if line.strip()
    ]
    prev = canonical_json.ZERO_HASH
    lines: list[bytes] = []
    for record in reversed(records):
        record.pop(canonical_json.RECORD_HASH_KEY, None)
        record["prev_record_sha256"] = prev
        digest = canonical_json.record_hash(record)
        prev = digest
        body = canonical_json.canonicalize(dict(record, record_sha256=digest))
        lines.append(body + b"\n")
        (stream.root / f"{int(record['chunk_id']):06d}.commit.json").write_bytes(body)
    stream.chunks_index.write_bytes(b"".join(lines))

    with pytest.raises(FinalizationError):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)


def test_finalizer_refuses_symmetric_payload_null(data_root: DataRoot) -> None:
    """Key presence is checked pre-seal too."""
    allocated, writer = _recording(data_root, SYN, chunks=1)
    forge_chain(allocated.paths, SYN.stream_id, lambda cid, r: r.__setitem__("payloads", None))
    with pytest.raises(FinalizationError):
        finalize(writer, outcome=RecordingOutcome.COMPLETED)
