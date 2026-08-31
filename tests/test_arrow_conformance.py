"""Physical Arrow conformance and row semantics (v2 §10; D31).

**A matching SHA proves identity, not conformance.** It proves the bytes are the
bytes that were sealed; it says nothing about whether those bytes are a valid v2
artifact. Every test here recomputes the artifact hash and reseals the manifest,
so the only thing left standing between the forgery and a COMPLETED verdict is
the schema and row-semantics check itself.
"""

import pyarrow as pa
import pytest

from consciousness_lab.session.model import RawCaptureLevel, SampleLayout
from consciousness_lab.storage.arrow_schema import (
    OBSERVATIONS_SCHEMA,
    PACKETS_SCHEMA,
    samples_schema,
    schema_conformance_error,
)
from consciousness_lab.storage.checksums import sha256_file
from consciousness_lab.storage.observations import ObservationError, validate_observation
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.verifier import Finding, verify_package
from consciousness_lab.synthetic.source import SyntheticStreamSpec
from tests.conftest import (
    build_session,
    read_chunk_records,
    reseal_manifest,
    rewrite_chunk_chain,
)

STREAM = "synthetic.eeg"


def _replace_artifact(
    paths: PackagePaths, stream_id: str, kind: str, chunk_id: int, table: pa.Table
) -> None:
    """Rewrite one artifact and restore every hash a forger could restore."""
    suffix = "bin" if kind == "payloads" else "arrow"
    target = paths.stream(stream_id).root / f"{kind}/{chunk_id:06d}.{suffix}"
    with target.open("wb") as sink, pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    records = read_chunk_records(paths, stream_id)
    for record in records:
        if int(record["chunk_id"]) == chunk_id:
            record["artifact_sha256"][kind] = sha256_file(target)
    rewrite_chunk_chain(paths, stream_id, records)
    reseal_manifest(paths)


def _read(paths: PackagePaths, stream_id: str, kind: str, chunk_id: int = 0) -> pa.Table:
    target = paths.stream(stream_id).root / f"{kind}/{chunk_id:06d}.arrow"
    with target.open("rb") as handle:
        return pa.ipc.open_stream(handle).read_all()


def test_schema_conformance_compares_names_order_types_and_nullability() -> None:
    assert schema_conformance_error(PACKETS_SCHEMA, PACKETS_SCHEMA) is None
    renamed = pa.schema([pa.field("nope", f.type, f.nullable) for f in PACKETS_SCHEMA])
    assert "fields are" in (schema_conformance_error(renamed, PACKETS_SCHEMA) or "")
    retyped = pa.schema(
        [
            pa.field(f.name, pa.string() if f.name == "packet_seq" else f.type, f.nullable)
            for f in PACKETS_SCHEMA
        ]
    )
    assert "packet_seq" in (schema_conformance_error(retyped, PACKETS_SCHEMA) or "")
    loosened = pa.schema([pa.field(f.name, f.type, True) for f in PACKETS_SCHEMA])
    assert "nullable" in (schema_conformance_error(loosened, PACKETS_SCHEMA) or "")
    reordered = pa.schema(list(PACKETS_SCHEMA)[::-1])
    assert schema_conformance_error(reordered, PACKETS_SCHEMA) is not None


def test_arrow_schema_metadata_is_not_contractual() -> None:
    """Unrecognised key-value metadata is ignored, not compared (§10.1)."""
    tagged = PACKETS_SCHEMA.with_metadata({b"produced_by": b"some tool"})
    assert schema_conformance_error(tagged, PACKETS_SCHEMA) is None


def test_a_deleted_samples_values_column_fails_verification(data_root: DataRoot) -> None:
    """C4's G1: in v1 this verified clean, because the SHA still matched."""
    built = build_session(data_root, chunks=1, packets_per_chunk=2)
    paths = built.allocated.paths
    table = _read(paths, STREAM, "samples")
    stripped = table.drop_columns(["values"])
    _replace_artifact(paths, STREAM, "samples", 0, stripped)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ARROW_SCHEMA_INVALID in result.findings()


def test_dense_samples_must_use_a_fixed_size_list_of_the_declared_width(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root, chunks=1, packets_per_chunk=2)
    paths = built.allocated.paths
    descriptor = _read(paths, STREAM, "samples").schema
    values = descriptor.field("values").type
    assert pa.types.is_fixed_size_list(values)
    assert values.list_size == 2
    assert values.value_type == pa.float32()

    wide = pa.schema(
        [
            pa.field("packet_seq", pa.int64(), nullable=False),
            pa.field("sample_index_in_packet", pa.int32(), nullable=False),
            pa.field(
                "values",
                pa.list_(pa.field("item", pa.float32(), nullable=False), 3),
                nullable=False,
            ),
        ]
    )
    rows = [
        {
            "packet_seq": r["packet_seq"],
            "sample_index_in_packet": r["sample_index_in_packet"],
            "values": [*r["values"], 0.0],
        }
        for r in _read(paths, STREAM, "samples").to_pylist()
    ]
    _replace_artifact(paths, STREAM, "samples", 0, pa.Table.from_pylist(rows, schema=wide))
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.ARROW_SCHEMA_INVALID in result.findings()


def test_the_samples_schema_is_derived_from_the_descriptor() -> None:
    dense = samples_schema(SampleLayout.DENSE_FIXED_LIST, 4)
    assert dense.names == ["packet_seq", "sample_index_in_packet", "values"]
    assert dense.field("values").type.list_size == 4
    sparse = samples_schema(SampleLayout.SPARSE_LONG, 4)
    assert sparse.names == ["packet_seq", "sample_index_in_packet", "channel_id", "value"]


def test_an_observation_violating_its_value_type_is_rejected_on_read(
    data_root: DataRoot,
) -> None:
    """C4's G2: v1 validated only on write, so this verified clean.

    ``value_type="uint64"`` with ``value_f64`` populated is not a device
    reading; it is a corrupt record that would later be read as a measurement.
    """
    built = build_session(data_root, chunks=1, packets_per_chunk=2)
    paths = built.allocated.paths
    rows = _read(paths, STREAM, "observations").to_pylist()
    for row in rows:
        if row["value_type"] == "uint64":
            row["value_u64"] = None
            row["value_f64"] = 1.5
            break
    else:  # pragma: no cover - the synthetic source always emits one
        pytest.skip("no uint64 observation in this fixture")
    _replace_artifact(
        paths, STREAM, "observations", 0, pa.Table.from_pylist(rows, schema=OBSERVATIONS_SCHEMA)
    )
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.OBSERVATION_ROW_INVALID in result.findings()


def test_an_observation_with_two_values_set_is_rejected_on_read(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root, chunks=1, packets_per_chunk=2)
    paths = built.allocated.paths
    rows = _read(paths, STREAM, "observations").to_pylist()
    rows[0]["value_str"] = "also set"
    _replace_artifact(
        paths, STREAM, "observations", 0, pa.Table.from_pylist(rows, schema=OBSERVATIONS_SCHEMA)
    )
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.OBSERVATION_ROW_INVALID in result.findings()


def test_an_out_of_domain_enum_is_rejected_on_read(data_root: DataRoot) -> None:
    built = build_session(data_root, chunks=1, packets_per_chunk=2)
    paths = built.allocated.paths
    rows = _read(paths, STREAM, "observations").to_pylist()
    rows[0]["applies_to"] = "made_up"
    _replace_artifact(
        paths, STREAM, "observations", 0, pa.Table.from_pylist(rows, schema=OBSERVATIONS_SCHEMA)
    )
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.OBSERVATION_ROW_INVALID in result.findings()


def test_write_and_read_use_the_same_validator(data_root: DataRoot) -> None:
    """One shared validator, invoked from both paths (§10.2).

    Two definitions of "valid observation" is the defect class this design
    removes, so the test asserts identity of the function, not similarity of
    behaviour.
    """
    import inspect

    from consciousness_lab.storage import chunk_writer, observations, stream_state

    writer_source = inspect.getsource(chunk_writer)
    reader_source = inspect.getsource(stream_state)
    assert "from consciousness_lab.storage.observations import validate_observations" in (
        writer_source
    )
    assert "validate_observation" in reader_source
    assert observations.validate_observations.__module__ == validate_observation.__module__

    bad = {
        "kind": "counter",
        "name": "x",
        "value_type": "uint64",
        "value_u64": None,
        "value_f64": 1.0,
        "applies_to": "unknown",
        "provenance": "library_provided",
        "status": "assumed",
    }
    with pytest.raises(ObservationError):
        validate_observation(bad)


def test_a_sparse_stream_verifies_end_to_end(data_root: DataRoot) -> None:
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec(
                "synthetic.sparse", RawCaptureLevel.SYNTHETIC, layout=SampleLayout.SPARSE_LONG
            )
        ],
        required=("synthetic.sparse",),
    )
    assert verify_package(built.allocated.paths).is_completed
