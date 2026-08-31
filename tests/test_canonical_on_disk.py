"""Canonical means canonical ON DISK, byte for byte (v2 §9.3; V11, V12).

"It parses, and canonicalizing it would produce equivalent content" is not
sufficient. Two byte sequences that parse alike are still two different files,
and a format that tolerates both has two spellings for one record — the same
class of ambiguity that produced several v1 findings.

Every test here restores the manifest afterwards, so the surviving check is the
byte-level conformance rule and not a hash mismatch.
"""

import json
from collections.abc import Callable
from typing import Any

import pytest

from consciousness_lab.session.model import LifecycleRecord
from consciousness_lab.storage import canonical_json, package_layout
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import Finding, verify_package
from tests.conftest import build_session, read_chunk_records, reseal_manifest

STREAM = "synthetic.eeg"


def test_every_structural_document_is_canonical_on_disk(data_root: DataRoot) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    documents = [
        paths.allocation,
        paths.run,
        paths.annotations_head,
        paths.manifest,
        paths.stream(STREAM).descriptor,
        paths.stream(STREAM).stream_close,
        *sorted(paths.schemas.iterdir()),
    ]
    for path in documents:
        assert package_layout.canonical_document_error(path.read_bytes()) is None, path


def test_every_jsonl_record_is_canonical_bytes_plus_one_newline(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root, chunks=2)
    paths = built.allocated.paths
    for path in (paths.lifecycle, paths.events, paths.stream(STREAM).chunks_index):
        raw = path.read_bytes()
        assert raw.endswith(b"\n") or raw == b""
        for line in raw.split(b"\n")[:-1]:
            assert canonical_json.canonicalize(canonical_json.loads(line)) == line


@pytest.mark.parametrize(
    "respell",
    [
        pytest.param(lambda obj: json.dumps(obj, indent=2).encode(), id="pretty_printed"),
        pytest.param(lambda obj: json.dumps(obj, separators=(", ", ": ")).encode(), id="spaced"),
        pytest.param(
            lambda obj: json.dumps(dict(reversed(list(obj.items())))).encode(), id="key_order"
        ),
    ],
)
def test_a_reserialized_chunk_record_is_rejected(
    data_root: DataRoot, respell: Callable[[dict[str, Any]], bytes]
) -> None:
    built = build_session(data_root, chunks=1)
    paths = built.allocated.paths
    record = read_chunk_records(paths, STREAM)[0]
    paths.stream(STREAM).chunks_index.write_bytes(respell(record) + b"\n")
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_CHUNK_CHAIN in result.findings()


def test_a_reserialized_lifecycle_record_is_rejected(data_root: DataRoot) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    lines = paths.lifecycle.read_bytes().split(b"\n")[:-1]
    obj = canonical_json.loads(lines[0])
    # Same content, same record_sha256 — only the spelling differs.
    lines[0] = json.dumps(obj, indent=1).encode("utf-8")
    paths.lifecycle.write_bytes(b"".join(line + b"\n" for line in lines))
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_LIFECYCLE_SEAL in result.findings()


def test_a_noncanonical_control_document_is_rejected(data_root: DataRoot) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.run.read_bytes())
    paths.run.write_bytes(json.dumps(obj, indent=2).encode("utf-8"))
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.UNREADABLE_RUN in result.findings()


def test_a_noncanonical_manifest_is_rejected(data_root: DataRoot) -> None:
    from consciousness_lab.storage.checksums import sha256_bytes

    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.manifest.read_bytes())
    body = json.dumps(obj, indent=2).encode("utf-8")
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.NOT_CANONICAL_ON_DISK in result.findings()


def test_trailing_bytes_after_the_last_newline_invalidate_a_finalized_log(
    data_root: DataRoot,
) -> None:
    """§4.1: a torn tail is not a record, and a finalized file should have none."""
    built = build_session(data_root)
    paths = built.allocated.paths
    paths.lifecycle.write_bytes(paths.lifecycle.read_bytes() + b'{"seq":"9"')
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_LIFECYCLE_SEAL in result.findings()


def test_an_empty_line_is_not_a_record(data_root: DataRoot) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    raw = paths.lifecycle.read_bytes()
    paths.lifecycle.write_bytes(raw.replace(b"\n", b"\n\n", 1))
    reseal_manifest(paths)
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.BROKEN_LIFECYCLE_SEAL in result.findings()


def test_verify_jsonl_region_accepts_a_conforming_region(data_root: DataRoot) -> None:
    built = build_session(data_root)
    raw = built.allocated.paths.lifecycle.read_bytes()
    assert (
        package_layout.verify_jsonl_region(raw, LifecycleRecord, require_record_hash=True) is None
    )
    assert (
        package_layout.verify_jsonl_region(b"", LifecycleRecord, require_record_hash=True) is None
    )


def test_chunk_records_have_no_record_hash_to_verify(data_root: DataRoot) -> None:
    """Accounting per file: the canonical form of a v2 chunk record excludes it."""
    built = build_session(data_root, chunks=1)
    record = read_chunk_records(built.allocated.paths, STREAM)[0]
    assert canonical_json.RECORD_HASH_KEY not in record
    lifecycle = canonical_json.loads(built.allocated.paths.lifecycle.read_bytes().split(b"\n")[0])
    assert canonical_json.RECORD_HASH_KEY in lifecycle
