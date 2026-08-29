"""CL-002B-R1-C4: leaf-level integrity (L1-L20).

C3 decomposed JSON *records* into fields but treated three things as atomic that
are themselves multi-field representations: a byte-framed artifact, a row set,
and a list. Those three gaps produced F1, F2 and F3. These tests cover the
leaves.

Threat-model boundary: this establishes structural, referential and
cross-representation integrity. It does NOT establish cryptographic authenticity
against an actor who coherently rewrites every artifact and all metadata — that
needs signatures or append-only media and is outside Session Package v1. The
standard here is: if any representation contradicts its authority, recomputing
ordinary hashes must not hide it.
"""

import struct
from collections.abc import Callable
from typing import Any

import pyarrow as pa
import pytest

from consciousness_lab.session.model import RawCaptureLevel, SampleLayout
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.arrow_schema import (
    OBSERVATIONS_SCHEMA,
    PACKETS_SCHEMA,
    samples_schema,
)
from consciousness_lab.storage.checksums import sha256_bytes
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.verifier import Finding, verify_package
from consciousness_lab.synthetic.source import SyntheticStreamSpec
from tests.conftest import build_session
from tests.test_canonical_and_physical import forge_chain, refresh_manifest

EEG = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD)
SYN = SyntheticStreamSpec("synthetic.imu", RawCaptureLevel.SYNTHETIC)


def reseal_artifacts(paths: PackagePaths, stream_id: str) -> None:
    """Recompute every artifact hash AND byte length, then the manifest.

    Deliberately independent of production reconciliation: it recomputes from
    the filesystem directly, so a shared bug cannot make an attack look caught.
    """
    root = paths.stream(stream_id).root

    def refresh(chunk_id: int, record: dict[str, Any]) -> None:
        for kind in ("packets", "observations", "samples", "payloads"):
            if record.get(kind) is None:
                continue
            target = root / record[kind]["path"]
            record[kind] = dict(
                record[kind],
                sha256=sha256_bytes(target.read_bytes()),
                bytes=str(target.stat().st_size),
            )

    forge_chain(paths, stream_id, refresh)
    refresh_manifest(paths, stream_id)


def rewrite_arrow(path: Any, schema: pa.Schema, mutate: Callable[[list[Any]], None]) -> None:
    with path.open("rb") as handle:
        rows = pa.ipc.open_stream(handle).read_all().to_pylist()
    mutate(rows)
    table = pa.Table.from_pylist(rows, schema=schema)
    with path.open("wb") as sink, pa.ipc.new_stream(sink, schema) as writer:
        writer.write_table(table)


def dense_schema(spec: SyntheticStreamSpec) -> pa.Schema:
    return samples_schema(SampleLayout.DENSE_FIXED_LIST, spec.n_channels)


def completed(paths: PackagePaths) -> bool:
    return verify_package(paths).is_completed


# =============================================================================
# L1-L3 — payload frame packet identity (F1)
# =============================================================================


def _forge_frame_packet_seq(paths: PackagePaths, frame_index: int, value: int) -> None:
    """Rewrite one frame's packet_seq leaf, keeping the frame internally valid.

    CRC covers the payload bytes only, so it stays correct — which is the whole
    point: an intact frame is not a frame that belongs to this packet.
    """
    from consciousness_lab.storage.payload import iter_frames

    blob = paths.stream(EEG.stream_id).root / "payloads/000000.bin"
    data = bytearray(blob.read_bytes())
    frames, error = iter_frames(bytes(data))
    assert error is None and len(frames) > frame_index
    struct.pack_into("<Q", data, frames[frame_index].offset + 4, value)
    blob.write_bytes(bytes(data))
    reseal_artifacts(paths, EEG.stream_id)


def test_l1_payload_frame_packet_seq_mismatch(data_root: DataRoot) -> None:
    """L1. The exact F1 attack, with every downstream hash recomputed."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1)
    paths = built.allocated.paths
    assert completed(paths)
    _forge_frame_packet_seq(paths, 0, 987654321)

    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2], "package is self-consistent"
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


@pytest.mark.parametrize("position", ["first", "middle", "last"])
def test_l2_payload_frame_mismatch_at_each_position(data_root: DataRoot, position: str) -> None:
    """L2. Position must not matter."""
    built = build_session(
        data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1, packets_per_chunk=3
    )
    index = {"first": 0, "middle": 1, "last": 2}[position]
    _forge_frame_packet_seq(built.allocated.paths, index, 555000 + index)
    assert not completed(built.allocated.paths)


def test_l3_frame_internally_valid_but_identity_wrong(data_root: DataRoot) -> None:
    """L3. CRC valid, length valid, offset valid — only the identity is wrong."""
    from consciousness_lab.storage.payload import iter_frames

    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1)
    paths = built.allocated.paths
    _forge_frame_packet_seq(paths, 0, 424242)

    blob = paths.stream(EEG.stream_id).root / "payloads/000000.bin"
    frames, error = iter_frames(blob.read_bytes())
    assert error is None, "every frame is still internally intact"
    assert frames[0].packet_seq == 424242
    assert not completed(paths)


# =============================================================================
# L4-L10 — dense sample key-set identity (F2)
# =============================================================================


def _mutate_samples(
    data_root: DataRoot, mutate: Callable[[list[Any]], None], *, chunks: int = 1
) -> PackagePaths:
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=chunks)
    paths = built.allocated.paths
    assert completed(paths)
    rewrite_arrow(
        paths.stream(SYN.stream_id).root / "samples/000000.arrow", dense_schema(SYN), mutate
    )
    reseal_artifacts(paths, SYN.stream_id)
    return paths


def test_l4_all_dense_sample_rows_deleted(data_root: DataRoot) -> None:
    """L4. The exact F2a attack."""
    paths = _mutate_samples(data_root, lambda rows: rows.clear())
    result = verify_package(paths)
    assert result.conditions[1] and result.conditions[2]
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_l5_one_dense_sample_row_deleted(data_root: DataRoot) -> None:
    """L5. A single missing row, which a set comparison alone would still catch —
    but only if cardinality is compared too (see L6)."""
    paths = _mutate_samples(data_root, lambda rows: rows.pop(1))
    assert not completed(paths)


def test_l6_duplicate_sample_key_replacing_another(data_root: DataRoot) -> None:
    """L6. The exact F2b attack: a duplicate hides a missing key.

    Row count is unchanged and the key *set* is a subset of the expected set, so
    only a multiset comparison catches it.
    """

    def swap(rows: list[Any]) -> None:
        rows[1] = dict(rows[0])

    paths = _mutate_samples(data_root, swap)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_l7_extra_dense_sample_row(data_root: DataRoot) -> None:
    """L7."""
    paths = _mutate_samples(data_root, lambda rows: rows.append(dict(rows[0])))
    assert not completed(paths)


def test_l8_dense_sample_points_to_absent_packet(data_root: DataRoot) -> None:
    """L8."""
    paths = _mutate_samples(data_root, lambda rows: rows[0].__setitem__("packet_seq", 999999))
    assert not completed(paths)


def test_l9_sample_index_equal_to_n_samples(data_root: DataRoot) -> None:
    """L9. The boundary: index == n_samples is out of range."""
    paths = _mutate_samples(
        data_root,
        lambda rows: rows[0].__setitem__("sample_index_in_packet", SYN.samples_per_packet),
    )
    assert not completed(paths)


def test_l10_zero_samples_per_packet_is_structurally_valid(data_root: DataRoot) -> None:
    """L10 positive control: n_samples = 0 expects an empty key set.

    Zero data is structurally valid; whether it is scientifically usable is a
    future protocol decision and is not asserted here.
    """
    spec = SyntheticStreamSpec("synthetic.imu", RawCaptureLevel.SYNTHETIC, samples_per_packet=0)
    built = build_session(data_root, streams=[spec], required=(spec.stream_id,), chunks=1)
    result = verify_package(built.allocated.paths)
    assert result.is_completed, [i.finding.value for i in result.issues]


def test_dense_positive_control_untouched(data_root: DataRoot) -> None:
    """The key-set rule must accept a genuine package."""
    built = build_session(
        data_root, streams=[SYN], required=(SYN.stream_id,), chunks=3, packets_per_chunk=4
    )
    assert completed(built.allocated.paths)


# =============================================================================
# L11-L17 — artifact and inventory byte leaves (F3a)
# =============================================================================


@pytest.mark.parametrize("kind", ["packets", "samples", "observations", "payloads"])
@pytest.mark.parametrize("delta", ["0", "999999999"])
def test_l11_to_l16_artifact_bytes_lie_while_sha_is_correct(
    data_root: DataRoot, kind: str, delta: str
) -> None:
    """L11-L16. bytes is an independent leaf; a correct SHA does not validate it."""
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1)
    paths = built.allocated.paths
    forge_chain(
        paths,
        EEG.stream_id,
        lambda cid, record: record.__setitem__(kind, dict(record[kind], bytes=delta)),
    )
    refresh_manifest(paths, EEG.stream_id)

    result = verify_package(paths)
    assert result.conditions[1], "manifest pair is valid"
    assert not result.is_completed
    assert Finding.ROW_REFERENCE_INVALID in result.findings()


def test_l17_inventory_bytes_mismatch(data_root: DataRoot) -> None:
    """L17."""
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj["inventory"][0]["bytes"] = "999999999"
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")

    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.INVENTORY_BYTES_MISMATCH in result.findings()


# =============================================================================
# L18-L20 — inventory bijection (F3b)
# =============================================================================


def _duplicate_inventory(paths: PackagePaths, mutate: Callable[[dict[str, Any]], None]) -> None:
    obj = canonical_json.loads(paths.manifest.read_bytes())
    clone = dict(obj["inventory"][0])
    mutate(clone)
    obj["inventory"].append(clone)
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")


def test_l18_exact_duplicate_inventory_entry(data_root: DataRoot) -> None:
    """L18. The exact F3b attack: set conversion collapsed it."""
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    _duplicate_inventory(built.allocated.paths, lambda entry: None)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.DUPLICATE_INVENTORY_PATH in result.findings()


def test_l19_duplicate_inventory_path_with_different_metadata(data_root: DataRoot) -> None:
    """L19. Same path, contradictory metadata — a set would keep whichever it saw."""
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    _duplicate_inventory(
        built.allocated.paths,
        lambda entry: entry.update({"sha256": "0" * 64, "bytes": "1"}),
    )
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.DUPLICATE_INVENTORY_PATH in result.findings()


def test_l20_normal_inventory_bijection(data_root: DataRoot) -> None:
    """L20 positive control."""
    built = build_session(
        data_root, streams=[EEG, SYN], required=(EEG.stream_id,), optional=(SYN.stream_id,)
    )
    result = verify_package(built.allocated.paths)
    assert result.is_completed
    assert result.manifest is not None
    paths_listed = [entry.path for entry in result.manifest.inventory]
    assert len(paths_listed) == len(set(paths_listed)), "inventory is a bijection"


# =============================================================================
# payload_ref leaves and frame bijection
# =============================================================================


def test_payload_ref_offset_not_at_a_frame_boundary(data_root: DataRoot) -> None:
    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1)
    paths = built.allocated.paths
    rewrite_arrow(
        paths.stream(EEG.stream_id).root / "packets/000000.arrow",
        PACKETS_SCHEMA,
        lambda rows: rows[0].__setitem__("payload_ref", dict(rows[0]["payload_ref"], offset=3)),
    )
    reseal_artifacts(paths, EEG.stream_id)
    assert not completed(paths)


def test_two_packets_referencing_the_same_frame(data_root: DataRoot) -> None:
    """§9.1: one record per received packet, so frames and packets are a bijection."""
    built = build_session(
        data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1, packets_per_chunk=2
    )
    paths = built.allocated.paths
    rewrite_arrow(
        paths.stream(EEG.stream_id).root / "packets/000000.arrow",
        PACKETS_SCHEMA,
        lambda rows: rows[1].__setitem__("payload_ref", dict(rows[0]["payload_ref"])),
    )
    reseal_artifacts(paths, EEG.stream_id)
    assert not completed(paths)


def test_unreferenced_payload_frame(data_root: DataRoot) -> None:
    """A frame no packet points at is an orphan in the payload log."""
    from consciousness_lab.storage.payload import frame as build_frame

    built = build_session(data_root, streams=[EEG], required=(EEG.stream_id,), chunks=1)
    paths = built.allocated.paths
    blob = paths.stream(EEG.stream_id).root / "payloads/000000.bin"
    blob.write_bytes(blob.read_bytes() + build_frame(999, b"orphan"))
    reseal_artifacts(paths, EEG.stream_id)
    assert not completed(paths)


def test_observations_have_no_invented_cardinality_rule(data_root: DataRoot) -> None:
    """Observations are not a complete set by contract; removing rows is structural.

    This is a *positive* control: it pins that no observation cardinality rule
    was invented while adding the dense sample rule.
    """
    built = build_session(data_root, streams=[SYN], required=(SYN.stream_id,), chunks=1)
    paths = built.allocated.paths
    rewrite_arrow(
        paths.stream(SYN.stream_id).root / "observations/000000.arrow",
        OBSERVATIONS_SCHEMA,
        lambda rows: rows.pop(0),
    )
    reseal_artifacts(paths, SYN.stream_id)
    assert completed(paths), "no observation cardinality rule exists"
