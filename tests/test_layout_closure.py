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
