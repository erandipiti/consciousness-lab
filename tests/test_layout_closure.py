"""The package file set is closed, not open-ended (v2 §9.2; V13).

The integrity DAG claims every immutable sealed byte is reachable from the
manifest. That claim is only true if the package cannot contain a file the DAG
never mentions, so every location gets a test: an unexpected file must invalidate
a sealed package even when it is simply absent from ``control_sha256``.
"""

import pytest

from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import Finding, verify_package
from tests.conftest import build_session, reseal_manifest

STREAM = "synthetic.eeg"


@pytest.mark.parametrize(
    "relative",
    [
        "mystery.bin",
        "events/extra.json",
        "schemas/unreferenced.json",
        f"raw/{STREAM}/foo.bin",
        f"raw/{STREAM}/packets/notes.txt",
        "stray.json",
    ],
)
def test_an_unexpected_immutable_file_invalidates_a_sealed_package(
    data_root: DataRoot, relative: str
) -> None:
    built = build_session(data_root)
    target = built.allocated.paths.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"{}")
    result = verify_package(built.allocated.paths)
    assert not result.is_completed, f"{relative} went unnoticed"
    assert result.findings() & {
        Finding.UNEXPECTED_FILE,
        Finding.SCHEMA_SET_MISMATCH,
        Finding.ORPHAN_FILE,
    }


def test_an_unexpected_file_is_caught_even_after_a_full_reseal(
    data_root: DataRoot,
) -> None:
    """Absent from ``control_sha256`` is not a defence — it is the attack.

    The forger adds a file and recomputes every hash they can see. What catches
    it is that the expected set is DERIVED from the layout, not read back from
    the manifest.
    """
    built = build_session(data_root)
    (built.allocated.paths.root / "mystery.bin").write_bytes(b"unexpected")
    reseal_manifest(built.allocated.paths)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.UNEXPECTED_FILE in result.findings()


def test_an_unexpected_root_directory_is_rejected(data_root: DataRoot) -> None:
    built = build_session(data_root)
    (built.allocated.paths.root / "derived").mkdir()
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.UNEXPECTED_FILE in result.findings()


def test_an_unreferenced_schema_snapshot_is_rejected(data_root: DataRoot) -> None:
    """No snapshot may sit silently outside the integrity DAG."""
    built = build_session(data_root)
    (built.allocated.paths.schemas / "invented.v9.json").write_bytes(b"{}")
    reseal_manifest(built.allocated.paths)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.SCHEMA_SET_MISMATCH in result.findings()


def test_a_referenced_schema_with_no_snapshot_is_rejected(data_root: DataRoot) -> None:
    """The set must equal the referenced ids exactly — no more, no fewer."""
    built = build_session(data_root)
    next(built.allocated.paths.schemas.iterdir()).unlink()
    reseal_manifest(built.allocated.paths)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.SCHEMA_SET_MISMATCH in result.findings()


def test_the_schemas_directory_holds_exactly_the_referenced_ids(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root)
    from consciousness_lab.storage import package_layout

    events = built.allocated.paths.events.read_bytes()
    referenced, error = package_layout.referenced_schema_ids(events)
    assert error is None
    on_disk = {p.name[: -len(".json")] for p in built.allocated.paths.schemas.iterdir()}
    assert on_disk == referenced
    assert verify_package(built.allocated.paths).is_completed


def test_logs_are_outside_the_dag_and_may_hold_anything(data_root: DataRoot) -> None:
    """Operational, never authoritative — writing a log cannot break a package."""
    built = build_session(data_root)
    logs = built.allocated.paths.logs
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "acquisition.log").write_text("whatever the operator logged")
    (logs / "nested").mkdir()
    (logs / "nested" / "more.log").write_text("still fine")
    assert verify_package(built.allocated.paths).is_completed


def test_a_symlink_anywhere_invalidates_the_package(data_root: DataRoot) -> None:
    """A symlink lets bytes leave the package while every check still passes."""
    built = build_session(data_root)
    outside = data_root.root / "outside.bin"
    outside.write_bytes(b"elsewhere")
    (built.allocated.paths.logs).mkdir(parents=True, exist_ok=True)
    (built.allocated.paths.logs / "link.bin").symlink_to(outside)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.SYMLINK_IN_PACKAGE in result.findings()


def test_a_file_directly_under_raw_is_rejected(data_root: DataRoot) -> None:
    """No per-stream scan would ever see it: ``raw/`` holds only directories.

    Found by the CL-002B-R2 conformance review — the layout scan checked the
    root, ``events/`` and ``schemas/``, and descended into each stream, so a
    file dropped straight into ``raw/`` fell between the two passes.
    """
    built = build_session(data_root)
    (built.allocated.paths.raw / "mystery.bin").write_bytes(b"unexpected")
    reseal_manifest(built.allocated.paths)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.UNEXPECTED_FILE in result.findings()


@pytest.mark.parametrize(
    "relative",
    [
        "allocation.json",
        "run.json",
        "annotations.head.json",
        f"raw/{STREAM}/descriptor.json",
        f"raw/{STREAM}/stream_close.json",
    ],
)
def test_a_noncanonical_structural_document_is_rejected(data_root: DataRoot, relative: str) -> None:
    """A hash proves identity; it does not prove the bytes are canonical (§9.3).

    ``annotations.head.json`` is included deliberately: it sits outside the
    manifest DAG, so nothing else would have noticed a second spelling of it.
    """
    import json

    from consciousness_lab.storage import canonical_json

    built = build_session(data_root)
    target = built.allocated.paths.root / relative
    obj = canonical_json.loads(target.read_bytes())
    target.write_bytes(json.dumps(obj, indent=2).encode("utf-8"))
    reseal_manifest(built.allocated.paths)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.NOT_CANONICAL_ON_DISK in result.findings()


@pytest.mark.parametrize(
    "removed", ["session_id", "streams", "inventory", "schemas", "scope_note", "events_seal"]
)
def test_a_resurrected_v1_manifest_key_is_rejected(data_root: DataRoot, removed: str) -> None:
    """Minor-version tolerance ignores a NEW field, not a deleted one (§7, D29).

    Left accepted, a v2 package could carry a ``streams`` block or a
    ``session_id`` contradicting the authority the fact actually lives in — the
    exact defect class v2 exists to remove.
    """
    from consciousness_lab.storage import canonical_json
    from consciousness_lab.storage.checksums import sha256_bytes

    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj[removed] = "resurrected"
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")
    result = verify_package(paths)
    assert not result.is_completed
    assert Finding.REMOVED_FIELD_PRESENT in result.findings()


def test_an_unknown_new_manifest_field_is_still_tolerated(data_root: DataRoot) -> None:
    """The other direction: minor-version tolerance is deliberate, and kept."""
    from consciousness_lab.storage import canonical_json
    from consciousness_lab.storage.checksums import sha256_bytes

    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj["some_future_v2_1_field"] = "ignored"
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")
    assert verify_package(paths).is_completed


def test_finalization_refuses_to_seal_an_unclosed_file_set(data_root: DataRoot) -> None:
    """The manifest is an integrity root — for every outcome, not only COMPLETED."""
    from consciousness_lab.session.finalizer import FinalizationError, finalize
    from consciousness_lab.session.model import RecordingOutcome

    built = build_session(data_root, finalize_outcome=None)
    (built.allocated.paths.root / "mystery.bin").write_bytes(b"unexpected")
    with pytest.raises(FinalizationError, match="file set is not closed"):
        finalize(built.writer, outcome=RecordingOutcome.ABORTED)


def test_an_aborted_package_with_an_unclean_required_stream_is_still_readable(
    data_root: DataRoot,
) -> None:
    """Condition 5 is split: its structural half gates reading, its outcome half does not.

    Refusing to read this would make the failure case the unreadable one, which
    is exactly backwards — an aborted session is what you most need to inspect.
    """
    from consciousness_lab.session.finalizer import finalize
    from consciousness_lab.session.model import RecordingOutcome, StreamCloseStatus
    from consciousness_lab.storage.reader import open_package

    built = build_session(data_root, finalize_outcome=None)
    built.writer.close_stream(STREAM, StreamCloseStatus.DISCONNECTED)
    finalize(built.writer, outcome=RecordingOutcome.ABORTED)
    package = open_package(built.allocated.paths)
    assert list(package.stream(STREAM).packets())
    assert not verify_package(built.allocated.paths).is_completed


def test_a_stream_with_no_closure_record_is_not_readable(data_root: DataRoot) -> None:
    """The structural half: the reader cannot say what it is handing back."""
    from consciousness_lab.storage.reader import UnverifiedPackageError, open_package

    built = build_session(data_root)
    built.allocated.paths.stream(STREAM).stream_close.unlink()
    reseal_manifest(built.allocated.paths)
    with pytest.raises(UnverifiedPackageError):
        open_package(built.allocated.paths)
