"""Derived registry: rebuild, reconcile, package-wins (spec §6, §12.1; D8, D15)."""

import sqlite3
from typing import Any

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session import registry
from consciousness_lab.session.model import RawCaptureLevel, RecordingOutcome
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.synthetic.source import SyntheticStreamSpec
from tests.conftest import build_session


def test_deleting_the_registry_loses_nothing(data_root: DataRoot) -> None:
    """The registry is derived: a rebuild recovers every package-backed session."""
    for _ in range(3):
        build_session(data_root)
    assert registry.rebuild(data_root) == 3
    before = registry.read_sessions(data_root)

    data_root.registry.unlink()
    assert registry.rebuild(data_root) == 3
    after = registry.read_sessions(data_root)

    def strip(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop the scan timestamp, which is expected to differ between rebuilds."""
        return [{k: v for k, v in row.items() if k != "scanned_at_utc_ns"} for row in rows]

    assert strip(before) == strip(after)


def test_allocation_is_durable_before_the_registry_exists(data_root: DataRoot) -> None:
    """Ordering (spec §12.1): no state can exist only in the database."""
    built = build_session(data_root, finalize_outcome=None)
    assert built.allocated.paths.allocation.is_file()
    assert built.allocated.paths.lifecycle.is_file()
    assert not data_root.registry.exists(), "acquisition began without any registry row"
    assert registry.rebuild(data_root) == 1


def test_rebuild_records_its_own_provenance(data_root: DataRoot) -> None:
    build_session(data_root)
    registry.rebuild(data_root)
    with sqlite3.connect(data_root.registry) as conn:
        rows = conn.execute("SELECT rebuilt_at_utc_ns, package_count FROM registry_meta").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 1


def test_a_stale_registry_row_loses_to_the_package(data_root: DataRoot) -> None:
    """Package wins, and the disagreement is reported rather than reconciled away."""
    built = build_session(data_root)
    registry.rebuild(data_root)
    with sqlite3.connect(data_root.registry) as conn:
        conn.execute(
            "UPDATE sessions SET effective_outcome = ?, is_completed = 1",
            (str(RecordingOutcome.COMPLETED),),
        )
        conn.commit()

    annotations_mod.append_annotation(
        annotations_path=built.allocated.paths.annotations,
        head_path=built.allocated.paths.annotations_head,
        sealed_outcome=RecordingOutcome.COMPLETED,
        to_outcome=RecordingOutcome.ABORTED,
        actor="researcher",
        reason="downgraded after review",
    )

    discrepancies = registry.reconcile(data_root)
    assert discrepancies, "a stale row must be reported"
    fields = {d.field for d in discrepancies}
    assert {"effective_outcome", "is_completed"} <= fields
    for discrepancy in discrepancies:
        if discrepancy.field == "effective_outcome":
            assert discrepancy.package_value == str(RecordingOutcome.ABORTED)

    registry.rebuild(data_root)
    row = registry.read_sessions(data_root)[0]
    assert row["effective_outcome"] == str(RecordingOutcome.ABORTED)
    assert row["is_completed"] == 0


def test_a_registry_row_without_a_package_is_reported(data_root: DataRoot) -> None:
    built = build_session(data_root)
    registry.rebuild(data_root)
    import shutil

    shutil.rmtree(built.allocated.paths.root)
    discrepancies = registry.reconcile(data_root)
    assert any(d.field == "presence" for d in discrepancies)


def test_registry_records_outcomes_and_capture_levels(data_root: DataRoot) -> None:
    build_session(
        data_root,
        streams=[
            SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD),
            SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED),
        ],
        required=("synthetic.eeg",),
        optional=("synthetic.ecg",),
    )
    build_session(data_root, finalize_outcome=RecordingOutcome.ABORTED)
    registry.rebuild(data_root)
    rows = registry.read_sessions(data_root)
    outcomes = {row["effective_outcome"] for row in rows}
    assert outcomes == {str(RecordingOutcome.COMPLETED), str(RecordingOutcome.ABORTED)}

    with sqlite3.connect(data_root.registry) as conn:
        levels = {r[0] for r in conn.execute("SELECT raw_capture_level FROM streams").fetchall()}
    assert {"transport_payload", "library_decoded"} <= levels


def test_unfinalized_sessions_are_indexed_but_not_completed(data_root: DataRoot) -> None:
    build_session(data_root, finalize_outcome=None)
    registry.rebuild(data_root)
    row = registry.read_sessions(data_root)[0]
    assert row["is_completed"] == 0
    assert row["lifecycle_state"] == "RECORDING"
