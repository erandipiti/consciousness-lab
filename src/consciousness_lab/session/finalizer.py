"""Finalization and sealing (spec §14; D21, D22, D23).

The ordering is the contract:

    1. seal open chunks
    2. append FINALIZING
    3. append terminal CLOSED (closure_condition + recording_outcome)
    4. seal events/events.jsonl
    5. snapshot schemas
    6. write annotations.head.json (zero state)
    7. hash in-scope files, verify chunk chains
    8. write manifest.json
    9. write manifest.sha256          <- FINALIZATION marker
   10. update the derived registry

Step 6 exists because §5.1 treats a missing head file as tampering: without it
every cleanly finalized session would resolve INDETERMINATE. It is written
before the manifest so a finalized package is never missing it, and excluded
from the inventory because it is mutable by design (§14.1).

Step 9 is the last durable act. The manifest pair means FINALIZED / SEALED. It
does not mean COMPLETED: a cleanly aborted session produces an identical pair.
"""

import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session import registry
from consciousness_lab.session.lifecycle import now_reading
from consciousness_lab.session.model import (
    ClockReading,
    ClosureCondition,
    FileEntry,
    FileSeal,
    LifecycleState,
    Manifest,
    ManifestStream,
    RecordingOutcome,
    Run,
    SchemaSnapshot,
    SealPointer,
    StreamCloseStatus,
    load_on_disk,
)
from consciousness_lab.session.writer import SessionWriter
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import (
    atomic_write,
    atomic_write_new,
    find_incomplete,
    sha256_bytes,
    sha256_file,
)
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.safe_paths import find_symlinks

#: Never inventoried: post-seal mutable objects (§14.1) and operational logs.
#: Hashing these would invalidate manifest.sha256 on the first annotation.
INVENTORY_EXCLUDED = {
    "annotations.jsonl",
    "annotations.head.json",
    "manifest.json",
    "manifest.sha256",
}
EXCLUDED_DIRS = {"logs"}


class FinalizationError(RuntimeError):
    """Finalization could not complete. The package is not sealed."""


@dataclass(frozen=True)
class FinalizationResult:
    manifest: Manifest
    manifest_sha256: str


def _assert_completable(
    writer: SessionWriter, run: Run, closure_condition: ClosureCondition
) -> None:
    """Refuse to seal COMPLETED unless it is actually true (spec §5, §14; D18)."""
    if closure_condition is not ClosureCondition.CLEAN:
        raise FinalizationError(
            f"cannot seal COMPLETED with closure_condition={closure_condition.value}"
        )
    missing = [s for s in run.required_streams if s not in writer.streams]
    if missing:
        raise FinalizationError(f"required streams were never opened: {sorted(missing)}")
    unclean = [
        stream_id
        for stream_id in run.required_streams
        if writer.streams[stream_id].close_status is not StreamCloseStatus.CLEAN
    ]
    if unclean:
        raise FinalizationError(f"required streams did not close CLEAN: {sorted(unclean)}")
    incomplete = find_incomplete(writer.paths.root)
    if incomplete:
        raise FinalizationError(
            f"{len(incomplete)} incomplete write marker(s) present; the session is not complete"
        )


def _inventory(paths: PackagePaths) -> list[FileEntry]:
    """Every immutable in-scope file, with its size and hash.

    A symlink is refused rather than followed: inventorying a link would record
    the hash of bytes that live outside the package, and the package would then
    verify without actually containing its own raw data.
    """
    links = find_symlinks(paths.root)
    if links:
        raise FinalizationError(
            f"package contains symlink(s) and cannot be sealed: "
            f"{[p.relative_to(paths.root).as_posix() for p in links]}"
        )
    entries: list[FileEntry] = []
    for path in sorted(paths.root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(paths.root)
        if rel.parts[0] in EXCLUDED_DIRS or rel.as_posix() in INVENTORY_EXCLUDED:
            continue
        entries.append(
            FileEntry(path=rel.as_posix(), bytes=path.stat().st_size, sha256=sha256_file(path))
        )
    return entries


def finalize(
    writer: SessionWriter,
    *,
    outcome: RecordingOutcome,
    closure_condition: ClosureCondition = ClosureCondition.CLEAN,
    outcome_reason: str | None = None,
    data_root: DataRoot | None = None,
) -> FinalizationResult:
    """Seal the package. ``outcome`` is the sealed recording outcome."""
    run = writer.run
    if run is None:
        raise FinalizationError("cannot finalize a session that never sealed run.json")

    # A sealed COMPLETED must be true at the moment it is written. The verifier
    # would reject the package later either way, but a lifecycle log that says
    # COMPLETED when it was not is a lie recorded in immutable data.
    if outcome is RecordingOutcome.COMPLETED:
        _assert_completable(writer, run, closure_condition)

    # 2, 3 — lifecycle first, so the sealed prefix is complete before hashing.
    if writer.lifecycle.state is LifecycleState.ALLOCATED:
        writer.lifecycle.append(LifecycleState.FINALIZING)
    elif writer.lifecycle.state is LifecycleState.RECORDING:
        writer.emit_event("RECORDING_STOP", "recording_stop.v1", origin="system")
        writer.emit_clock_snapshot()
        writer.lifecycle.append(LifecycleState.FINALIZING)
    writer.lifecycle.append(
        LifecycleState.CLOSED,
        closure_condition=closure_condition,
        recording_outcome=outcome,
        outcome_reason=outcome_reason,
    )

    # 4 — events are sealed here; nothing appends to them afterwards.
    events_path = writer.paths.events
    if not events_path.exists():
        atomic_write(events_path, b"")
    events_bytes = events_path.read_bytes()

    # 5 — schemas travel with the package.
    snapshots = [
        SchemaSnapshot(
            schema_id=schema_id,
            path=path.relative_to(writer.paths.root).as_posix(),
            sha256=sha256_bytes(body),
        )
        for schema_id, path, body in writer.snapshot_schemas()
    ]

    # 6 — zero-state annotation head. Required for every finalized package.
    annotations_mod.write_initial_head(writer.paths.annotations_head)

    # 7 — seal pointers and stream summaries.
    lifecycle_bytes = writer.paths.lifecycle.read_bytes()
    required = set(run.required_streams)
    streams: list[ManifestStream] = []
    for stream_id, stream in sorted(writer.streams.items()):
        commits = stream.writer.committed
        streams.append(
            ManifestStream(
                stream_id=stream_id,
                required=stream_id in required,
                close_status=stream.close_status,
                descriptor_sha256=stream.descriptor_sha256,
                chunk_count=len(commits),
                chunk_chain_head_sha256=stream.writer.chain_head,
                first_packet_seq=commits[0].first_packet_seq if commits else None,
                last_packet_seq=commits[-1].last_packet_seq if commits else None,
            )
        )

    utc_ns, monotonic_ns = now_reading()
    manifest = Manifest(
        session_id=writer.paths.root.name,
        sealed_at=ClockReading(utc_ns=utc_ns, monotonic_ns=monotonic_ns),
        lifecycle_seal=SealPointer(
            path="lifecycle.jsonl",
            sealed_len=len(lifecycle_bytes),
            sealed_sha256=sha256_bytes(lifecycle_bytes),
        ),
        events_seal=FileSeal(
            path="events/events.jsonl",
            bytes=len(events_bytes),
            sha256=sha256_bytes(events_bytes),
        ),
        streams=streams,
        inventory=_inventory(writer.paths),
        schemas=snapshots,
    )

    # 8, 9 — the manifest pair. Step 9 is the finalization marker.
    manifest_bytes = canonical_json.canonicalize(manifest.model_dump(mode="json"))
    atomic_write_new(writer.paths.manifest, manifest_bytes)
    digest = sha256_bytes(manifest_bytes)
    atomic_write_new(writer.paths.manifest_sha256, (digest + "\n").encode("utf-8"))

    # Step 10 (spec §14): refresh the derived index. The package is already
    # sealed, so a failure here costs nothing but a stale row.
    if data_root is not None:
        with contextlib.suppress(sqlite3.Error):
            registry.upsert(data_root, writer.paths)

    return FinalizationResult(manifest=manifest, manifest_sha256=digest)


def read_manifest(path: Path) -> Manifest | None:
    if not path.exists():
        return None
    try:
        return load_on_disk(Manifest, canonical_json.loads(path.read_bytes()))
    except (canonical_json.CanonicalizationError, ValueError):
        return None
