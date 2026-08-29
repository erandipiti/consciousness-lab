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
from consciousness_lab.storage.stream_state import read_all_physical_streams

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
    _assert_streams_on_disk(writer, run)


def _assert_streams_on_disk(writer: SessionWriter, run: Run) -> None:
    """Refuse to seal COMPLETED when writer memory contradicts the disk.

    Defence in depth. The verifier is authoritative after sealing, but a
    lifecycle record claiming COMPLETED is immutable, so it must not be written
    while the streams it describes are known to be missing (CL-002B-R1).
    """
    physical = read_all_physical_streams(writer.paths)

    for stream_id in run.required_streams:
        state = physical.get(stream_id)
        if state is None or not state.directory_exists:
            raise FinalizationError(f"required stream {stream_id} has no raw directory on disk")

    for stream_id, open_stream in sorted(writer.streams.items()):
        state = physical.get(stream_id)
        if state is None or not state.structurally_complete:
            detail = "absent" if state is None else (state.chain_error or "incomplete structure")
            raise FinalizationError(f"stream {stream_id} is not intact on disk: {detail}")
        if state.descriptor_sha256 != open_stream.descriptor_sha256:
            raise FinalizationError(f"stream {stream_id} descriptor changed since it was opened")

        # Every chunk the writer believes it committed must still be in the
        # index, and its artifacts must still exist.
        expected = {commit.chunk_id: commit for commit in open_stream.writer.committed}
        actual = {commit.chunk_id: commit for commit in state.commits}
        missing = sorted(set(expected) - set(actual))
        if missing:
            raise FinalizationError(
                f"stream {stream_id}: committed chunk(s) {missing} are absent from chunks.jsonl"
            )
        for chunk_id, commit in actual.items():
            artifacts = [commit.packets, commit.observations, commit.samples]
            if commit.payloads is not None:
                artifacts.append(commit.payloads)
            for artifact in artifacts:
                if not (writer.paths.stream(stream_id).root / artifact.path).is_file():
                    raise FinalizationError(
                        f"stream {stream_id}: chunk {chunk_id} artifact {artifact.path} is missing"
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
    # Stream summaries are derived from what is ON DISK, not from writer memory.
    # Building them from memory is what let a manifest describe a stream whose
    # raw directory had been deleted (CL-002B-R1). ``close_status`` is the one
    # field with no on-disk representation, so it still comes from the writer;
    # ``required`` comes from run.json, which is the declaration of record.
    required = set(run.required_streams)
    physical = read_all_physical_streams(writer.paths)
    streams: list[ManifestStream] = []
    for stream_id in sorted(physical):
        state = physical[stream_id]
        if state.descriptor_sha256 is None:
            raise FinalizationError(
                f"stream {stream_id} has no readable descriptor and cannot be summarised"
            )
        open_stream = writer.streams.get(stream_id)
        streams.append(
            ManifestStream(
                stream_id=stream_id,
                required=stream_id in required,
                close_status=(
                    open_stream.close_status if open_stream else StreamCloseStatus.FAILED
                ),
                descriptor_sha256=state.descriptor_sha256,
                chunk_count=state.chunk_count,
                chunk_chain_head_sha256=state.chain_head_sha256,
                first_packet_seq=state.first_packet_seq,
                last_packet_seq=state.last_packet_seq,
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
