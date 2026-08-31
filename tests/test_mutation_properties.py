"""Property-based physical mutation (v2 §9, §10).

The threat model is explicit: an attacker can recompute every ordinary SHA-256,
every chain hash and the manifest pair in any file they rewrite. Cryptographic
authenticity against a fully coherent rewrite is out of scope. What must hold is
that a package which no longer *conforms* cannot verify as COMPLETED, however
carefully its hashes were restored.

So these mutate the **physical bytes**, not Pydantic objects, then restore every
hash a forger could restore, and assert the package still fails.
"""

import pyarrow as pa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from consciousness_lab.storage.checksums import sha256_file
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.verifier import verify_package
from tests.conftest import (
    build_session,
    read_chunk_records,
    reseal_manifest,
    rewrite_chunk_chain,
)

STREAM = "synthetic.eeg"
KINDS = ("packets", "observations", "samples", "payloads")
SLOW = settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _reseal_everything(paths: PackagePaths) -> None:
    """Restore every hash the attacker can see, so only conformance is left."""
    records = read_chunk_records(paths, STREAM)
    root = paths.stream(STREAM).root
    for record in records:
        chunk_id = int(record["chunk_id"])
        for kind in list(record["artifact_sha256"]):
            suffix = "bin" if kind == "payloads" else "arrow"
            target = root / f"{kind}/{chunk_id:06d}.{suffix}"
            if target.is_file():
                record["artifact_sha256"][kind] = sha256_file(target)
    rewrite_chunk_chain(paths, STREAM, records)
    reseal_manifest(paths)


@given(kind=st.sampled_from(KINDS), position=st.integers(min_value=0, max_value=200))
@SLOW
def test_truncating_any_artifact_is_detected(data_root: DataRoot, kind: str, position: int) -> None:
    built = build_session(data_root, chunks=1, packets_per_chunk=3)
    paths = built.allocated.paths
    suffix = "bin" if kind == "payloads" else "arrow"
    target = paths.stream(STREAM).root / f"{kind}/000000.{suffix}"
    raw = target.read_bytes()
    cut = position % max(len(raw), 1)
    if cut == 0 or cut == len(raw):
        return
    target.write_bytes(raw[:cut])
    _reseal_everything(paths)
    assert not verify_package(paths).is_completed, f"{kind} truncated at {cut} verified clean"


@given(kind=st.sampled_from(("packets", "samples")))
@SLOW
def test_deleting_any_keyed_row_is_detected(data_root: DataRoot, kind: str) -> None:
    """``packets`` and ``samples`` carry a primary key, so a deletion shows.

    ``observations`` is excluded on purpose — see the test below.
    """
    built = build_session(data_root, chunks=1, packets_per_chunk=3)
    paths = built.allocated.paths
    target = paths.stream(STREAM).root / f"{kind}/000000.arrow"
    with target.open("rb") as handle:
        table = pa.ipc.open_stream(handle).read_all()
    rows = table.to_pylist()
    if len(rows) < 2:
        return
    trimmed = pa.Table.from_pylist(rows[:-1], schema=table.schema)
    with target.open("wb") as sink, pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(trimmed)
    _reseal_everything(paths)
    assert not verify_package(paths).is_completed, f"a deleted {kind} row verified clean"


@given(kind=st.sampled_from(("packets", "observations", "samples")))
@SLOW
def test_dropping_any_column_of_any_table_is_detected(data_root: DataRoot, kind: str) -> None:
    """A matching SHA proves identity, not conformance — this is C4's G1."""
    built = build_session(data_root, chunks=1, packets_per_chunk=2)
    paths = built.allocated.paths
    target = paths.stream(STREAM).root / f"{kind}/000000.arrow"
    with target.open("rb") as handle:
        table = pa.ipc.open_stream(handle).read_all()
    for name in table.schema.names:
        stripped = table.drop_columns([name])
        with target.open("wb") as sink, pa.ipc.new_stream(sink, stripped.schema) as writer:
            writer.write_table(stripped)
        _reseal_everything(paths)
        assert not verify_package(paths).is_completed, f"{kind} without {name} verified clean"
        return


def test_a_deleted_observation_row_is_deliberately_not_a_violation(
    data_root: DataRoot,
) -> None:
    """§10.3 names no observation primary key, and none is invented here.

    Observations are not a complete set by contract: a device may or may not
    report a counter for a given packet. Enforcing a cardinality rule would be
    inventing a completeness criterion the specification explicitly withholds,
    and that criterion would be scientific, not structural. The honest statement
    is that this class of loss is out of scope for package integrity — so the
    test asserts the boundary rather than pretending it is covered.
    """
    built = build_session(data_root, chunks=1, packets_per_chunk=3)
    paths = built.allocated.paths
    target = paths.stream(STREAM).root / "observations/000000.arrow"
    with target.open("rb") as handle:
        table = pa.ipc.open_stream(handle).read_all()
    rows = table.to_pylist()
    assert len(rows) > 1
    trimmed = pa.Table.from_pylist(rows[:-1], schema=table.schema)
    with target.open("wb") as sink, pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(trimmed)
    _reseal_everything(paths)
    assert verify_package(paths).is_completed

    # What IS enforced is the reference: an observation may not point at a
    # packet that is not in its own chunk.
    rows[-1]["packet_seq"] = 987_654
    forged = pa.Table.from_pylist(rows, schema=table.schema)
    with target.open("wb") as sink, pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(forged)
    _reseal_everything(paths)
    assert not verify_package(paths).is_completed


@given(chunk_id=st.integers(min_value=0, max_value=2))
@SLOW
def test_deleting_any_committed_artifact_is_detected(data_root: DataRoot, chunk_id: int) -> None:
    built = build_session(data_root, chunks=3)
    paths = built.allocated.paths
    for kind in KINDS:
        suffix = "bin" if kind == "payloads" else "arrow"
        target = paths.stream(STREAM).root / f"{kind}/{chunk_id:06d}.{suffix}"
        if not target.is_file():
            continue
        target.unlink()
        reseal_manifest(paths)
        assert not verify_package(paths).is_completed
        return


@given(index=st.integers(min_value=0, max_value=6))
@SLOW
def test_dropping_any_committed_chunk_record_is_detected(data_root: DataRoot, index: int) -> None:
    built = build_session(data_root, chunks=3)
    paths = built.allocated.paths
    records = read_chunk_records(paths, STREAM)
    position = index % len(records)
    del records[position]
    rewrite_chunk_chain(paths, STREAM, records)
    reseal_manifest(paths)
    assert not verify_package(paths).is_completed, "a dropped commit left an orphan artifact"


@given(name=st.text(alphabet="abcdefghijklmnop", min_size=1, max_size=8))
@SLOW
def test_any_added_immutable_file_is_detected(data_root: DataRoot, name: str) -> None:
    built = build_session(data_root, chunks=1)
    paths = built.allocated.paths
    (paths.root / f"{name}.dat").write_bytes(b"unexpected")
    reseal_manifest(paths)
    assert not verify_package(paths).is_completed, f"{name}.dat verified clean"
