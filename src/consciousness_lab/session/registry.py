"""Derived SQLite registry (spec §6; D8, D15).

The registry is an index, never a truth. It can be deleted and rebuilt from the
packages at any time, and where it disagrees with a package the package wins and
the discrepancy is reported.

SQLite comes from the standard library, so this adds no dependency.
"""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session.finalizer import read_manifest
from consciousness_lab.session.lifecycle import SealedLifecycle as LifecycleSummary
from consciousness_lab.session.lifecycle import read_records, summarize
from consciousness_lab.session.model import (
    Allocation,
    LifecycleState,
    Manifest,
    RecordingOutcome,
    Run,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.verifier import verify_package

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id            TEXT PRIMARY KEY,
  package_path          TEXT NOT NULL,
  allocated_at_utc_ns   TEXT NOT NULL,
  protocol_id           TEXT,
  protocol_version      TEXT,
  participant_pseudonym TEXT,
  lifecycle_state       TEXT NOT NULL,
  closure_condition     TEXT,
  sealed_outcome        TEXT,
  effective_outcome     TEXT,
  annotation_status     TEXT NOT NULL,
  is_completed          INTEGER NOT NULL,
  manifest_sha256       TEXT,
  scanned_at_utc_ns     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_transitions (
  session_id  TEXT NOT NULL,
  record_kind TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  state       TEXT,
  outcome     TEXT,
  actor       TEXT,
  reason      TEXT,
  utc_ns      TEXT NOT NULL,
  PRIMARY KEY (session_id, record_kind, seq)
);
CREATE TABLE IF NOT EXISTS streams (
  session_id        TEXT NOT NULL,
  stream_id         TEXT NOT NULL,
  device_kind       TEXT,
  modality          TEXT,
  raw_capture_level TEXT,
  required          INTEGER NOT NULL,
  close_status      TEXT,
  descriptor_sha256 TEXT,
  PRIMARY KEY (session_id, stream_id)
);
CREATE TABLE IF NOT EXISTS registry_meta (
  rebuilt_at_utc_ns TEXT NOT NULL,
  scanned_root      TEXT NOT NULL,
  package_count     INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class ScannedPackage:
    """One package, read into the shape the derived index stores."""

    paths: PackagePaths
    allocation: Allocation
    summary: LifecycleSummary
    manifest: Manifest | None
    effective: annotations_mod.EffectiveOutcome
    run: Run | None
    is_completed: bool


@dataclass(frozen=True)
class Discrepancy:
    """A registry row that disagrees with its package. The package wins."""

    session_id: str
    field: str
    registry_value: str | None
    package_value: str | None


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        conn.close()


def _scan_package(paths: PackagePaths) -> ScannedPackage | None:
    """Read one package into the row shape the registry stores."""
    if not paths.allocation.is_file():
        return None
    try:
        allocation = Allocation.model_validate(canonical_json.loads(paths.allocation.read_bytes()))
    except (canonical_json.CanonicalizationError, ValueError):
        return None

    summary = summarize(read_records(paths.lifecycle))
    manifest = read_manifest(paths.manifest)
    effective = annotations_mod.resolve_effective_outcome(
        sealed_outcome=summary.sealed_outcome,
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
    )
    verification = verify_package(paths)

    run: Run | None = None
    if paths.run.is_file():
        try:
            run = Run.model_validate(canonical_json.loads(paths.run.read_bytes()))
        except (canonical_json.CanonicalizationError, ValueError):
            run = None

    return ScannedPackage(
        paths=paths,
        allocation=allocation,
        summary=summary,
        manifest=manifest,
        effective=effective,
        run=run,
        is_completed=verification.is_completed,
    )


def rebuild(data_root: DataRoot) -> int:
    """Drop the index and rebuild it by scanning every package.

    This is the operation that proves the registry is derived: deleting the
    database loses nothing that a package still holds.
    """
    now = str(time.time_ns())
    packages = data_root.iter_packages()
    with _connect(data_root.registry) as conn, closing(conn.cursor()) as cur:
        cur.executescript(SCHEMA)
        cur.execute("DELETE FROM sessions")
        cur.execute("DELETE FROM session_transitions")
        cur.execute("DELETE FROM streams")
        cur.execute("DELETE FROM registry_meta")
        count = 0
        for package in packages:
            scanned = _scan_package(package)
            if scanned is None:
                continue
            count += 1
            _insert(cur, scanned, now)
        cur.execute(
            "INSERT INTO registry_meta VALUES (?, ?, ?)",
            (now, str(data_root.sessions), count),
        )
        conn.commit()
    return count


def upsert(data_root: DataRoot, paths: PackagePaths) -> None:
    """Refresh one session's derived row. Failure here is recoverable by rebuild."""
    scanned = _scan_package(paths)
    if scanned is None:
        return
    now = str(time.time_ns())
    with _connect(data_root.registry) as conn, closing(conn.cursor()) as cur:
        cur.executescript(SCHEMA)
        cur.execute("DELETE FROM sessions WHERE session_id = ?", (paths.root.name,))
        cur.execute("DELETE FROM session_transitions WHERE session_id = ?", (paths.root.name,))
        cur.execute("DELETE FROM streams WHERE session_id = ?", (paths.root.name,))
        _insert(cur, scanned, now)
        conn.commit()


def _insert(cur: sqlite3.Cursor, scanned: ScannedPackage, now: str) -> None:
    allocation = scanned.allocation
    summary = scanned.summary
    manifest = scanned.manifest
    effective = scanned.effective
    run = scanned.run
    paths = scanned.paths

    state = summary.terminal_state or LifecycleState.ALLOCATED
    closure = summary.closure_condition
    sealed = summary.sealed_outcome
    cur.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            allocation.session_id,
            str(paths.root),
            str(allocation.allocated_at.utc_ns),
            allocation.protocol.id,
            allocation.protocol.version,
            allocation.participant_pseudonym,
            str(state),
            str(closure) if closure else None,
            str(sealed) if sealed else None,
            str(effective.outcome) if effective.outcome else None,
            str(effective.status),
            int(scanned.is_completed),
            _manifest_hash(paths) if manifest is not None else None,
            now,
        ),
    )
    for record in summary.records:
        cur.execute(
            "INSERT OR REPLACE INTO session_transitions VALUES (?,?,?,?,?,?,?,?)",
            (
                allocation.session_id,
                "lifecycle",
                record.seq,
                str(record.state),
                str(record.recording_outcome) if record.recording_outcome else None,
                record.actor,
                record.outcome_reason,
                str(record.utc_ns),
            ),
        )
    required = set(run.required_streams) if run is not None else set()
    if manifest is not None:
        for stream in manifest.streams:
            descriptor_level = _capture_level(paths, stream.stream_id)
            cur.execute(
                "INSERT OR REPLACE INTO streams VALUES (?,?,?,?,?,?,?,?)",
                (
                    allocation.session_id,
                    stream.stream_id,
                    None,
                    None,
                    descriptor_level,
                    int(stream.stream_id in required),
                    str(stream.close_status),
                    stream.descriptor_sha256,
                ),
            )


def _manifest_hash(paths: PackagePaths) -> str | None:
    if not paths.manifest_sha256.is_file():
        return None
    return paths.manifest_sha256.read_text(encoding="utf-8").strip()


def _capture_level(paths: PackagePaths, stream_id: str) -> str | None:
    descriptor = paths.stream(stream_id).descriptor
    if not descriptor.is_file():
        return None
    try:
        obj = canonical_json.loads(descriptor.read_bytes())
    except (canonical_json.CanonicalizationError, ValueError):
        return None
    acquisition = obj.get("acquisition") if isinstance(obj, dict) else None
    if isinstance(acquisition, dict):
        level = acquisition.get("raw_capture_level")
        return str(level) if level is not None else None
    return None


def read_sessions(data_root: DataRoot) -> list[dict[str, Any]]:
    if not data_root.registry.exists():
        return []
    with _connect(data_root.registry) as conn, closing(conn.cursor()) as cur:
        cur.executescript(SCHEMA)
        cur.execute("SELECT * FROM sessions ORDER BY session_id")
        return [dict(row) for row in cur.fetchall()]


def reconcile(data_root: DataRoot) -> list[Discrepancy]:
    """Compare every registry row with its package. The package always wins."""
    rows: dict[str, dict[str, Any]] = {
        str(row["session_id"]): row for row in read_sessions(data_root)
    }
    discrepancies: list[Discrepancy] = []
    for package in data_root.iter_packages():
        scanned = _scan_package(package)
        if scanned is None:
            continue
        session_id = package.root.name
        row = rows.pop(session_id, None)
        if row is None:
            discrepancies.append(Discrepancy(session_id, "presence", None, "package exists"))
            continue
        package_outcome = str(scanned.effective.outcome) if scanned.effective.outcome else None
        if row["effective_outcome"] != package_outcome:
            discrepancies.append(
                Discrepancy(
                    session_id, "effective_outcome", row["effective_outcome"], package_outcome
                )
            )
        package_completed = int(scanned.is_completed)
        if int(row["is_completed"]) != package_completed:
            discrepancies.append(
                Discrepancy(
                    session_id,
                    "is_completed",
                    str(row["is_completed"]),
                    str(package_completed),
                )
            )
    discrepancies.extend(
        Discrepancy(orphan_id, "presence", "registry row", None) for orphan_id in rows
    )
    return discrepancies


__all__ = [
    "Discrepancy",
    "RecordingOutcome",
    "read_sessions",
    "rebuild",
    "reconcile",
    "upsert",
]
