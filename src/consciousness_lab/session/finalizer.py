"""Finalization and sealing (v2 §9.4; D21, D22, D23, D30, D33).

The ordering is the contract:

     1. finish and commit the final raw chunks
     2. durably write stream_close.json for every opened stream lacking one
     3. append FINALIZING to lifecycle.jsonl          (with record_sha256)
     4. append terminal CLOSED to lifecycle.jsonl     (with record_sha256)
     5. seal events/events.jsonl
     6. snapshot the referenced event schemas
     7. initialise annotations.head.json if absent
     8. verify raw physical conformance and every durable pre-manifest fact
     9. construct manifest.control_sha256
    10. write manifest.json atomically
    11. write manifest.sha256 atomically      <-- FINALIZATION MARKER
    12. registry upsert (derived)

**Step 2 precedes step 3 deliberately.** After the terminal lifecycle record is
durable, per-stream closure must already be on disk, or a crash between them
loses it — that was R1-1's window, and it is why closure is a file rather than a
manifest field.

Step 7 exists because a missing head file is treated as tampering: without it
every cleanly finalized session would resolve INDETERMINATE.

Step 11 is the last durable act. The manifest pair means FINALIZED / SEALED. It
does not mean COMPLETED: a cleanly aborted session produces an identical pair.
"""

import contextlib
import sqlite3
from dataclasses import dataclass

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session import registry
from consciousness_lab.session.lifecycle import now_reading
from consciousness_lab.session.model import (
    ClockReading,
    ClosureCondition,
    LifecycleState,
    Manifest,
    RecordingOutcome,
    Run,
    SealPointer,
    StreamCloseStatus,
)
from consciousness_lab.session.writer import SessionWriter, write_stream_close
from consciousness_lab.storage import canonical_json, package_layout
from consciousness_lab.storage.checksums import (
    atomic_write,
    atomic_write_new,
    find_incomplete,
    sha256_bytes,
    sha256_file,
)
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.safe_paths import find_symlinks
from consciousness_lab.storage.stream_state import (
    PhysicalStreamState,
    read_all_physical_streams,
)


class FinalizationError(RuntimeError):
    """Finalization could not complete. The package is not sealed."""


@dataclass(frozen=True)
class FinalizationResult:
    manifest: Manifest
    manifest_sha256: str


def _assert_sealable(
    writer: SessionWriter, run: Run, physical: dict[str, PhysicalStreamState]
) -> None:
    """Structural preconditions for sealing ANY outcome.

    These are not completion rules. A stream outside the run contract, or one
    with no durable closure record, makes the package structurally invalid
    whatever the session's scientific outcome was — V09's stream-set agreement
    is not outcome-specific, and a sealed ABORTED package that fails it is just
    as unverifiable as a sealed COMPLETED one.

    Checked before any terminal lifecycle record is written, because that record
    is immutable once appended.
    """
    declared = set(run.required_streams) | set(run.optional_streams)
    undeclared = sorted(set(physical) - declared)
    if undeclared:
        raise FinalizationError(
            f"stream(s) {undeclared} are on disk but declared in neither "
            "run.required_streams nor run.optional_streams"
        )
    for stream_id in sorted(physical):
        state = physical[stream_id]
        if state.close_error is not None:
            raise FinalizationError(f"stream {stream_id}: {state.close_error}")
        if not state.close_present:
            raise FinalizationError(f"stream {stream_id} has no durable stream_close.json")


def _assert_completable(
    writer: SessionWriter,
    run: Run,
    closure_condition: ClosureCondition,
    physical: dict[str, PhysicalStreamState],
) -> None:
    """Refuse to seal COMPLETED unless it is actually true (D18, D22).

    Completion-specific only — the structural rules live in ``_assert_sealable``
    and run for every outcome. Defence in depth: the verifier is authoritative
    after sealing, but a lifecycle record claiming COMPLETED is immutable, so it
    must not be written while the streams it describes are known to be missing
    or unclean.
    """
    if closure_condition is not ClosureCondition.CLEAN:
        raise FinalizationError(
            f"cannot seal COMPLETED with closure_condition={closure_condition.value}"
        )
    missing = [s for s in run.required_streams if s not in writer.streams]
    if missing:
        raise FinalizationError(f"required streams were never opened: {sorted(missing)}")
    incomplete = find_incomplete(writer.paths.root)
    if incomplete:
        raise FinalizationError(
            f"{len(incomplete)} incomplete write marker(s) present; the session is not complete"
        )
    for stream_id in run.required_streams:
        state = physical.get(stream_id)
        if state is None or not state.directory_exists:
            raise FinalizationError(f"required stream {stream_id} has no raw directory on disk")
        # The durable file, not the writer's memory: closure has exactly one
        # authority and this must read the same one the verifier will (D33).
        if state.close_status is not StreamCloseStatus.CLEAN:
            raise FinalizationError(
                f"required stream {stream_id} closed {state.close_status}, not CLEAN"
            )


def _assert_streams_on_disk(
    writer: SessionWriter, physical: dict[str, PhysicalStreamState]
) -> None:
    """Step 8: every durable pre-manifest fact verifies before anything is sealed.

    Comparison is by RECORD IDENTITY — the canonical bytes the writer committed
    against the canonical bytes on disk — not by parsed model. Rewriting the
    chain with the same chunk ids used to slip past a check that only looked at
    which ids were present.
    """
    for stream_id, open_stream in sorted(writer.streams.items()):
        state = physical.get(stream_id)
        if state is None or not state.structurally_complete:
            detail = "absent" if state is None else (state.chain_error or "incomplete structure")
            raise FinalizationError(f"stream {stream_id} is not intact on disk: {detail}")
        if state.descriptor_sha256 != open_stream.descriptor_sha256:
            raise FinalizationError(f"stream {stream_id} descriptor changed since it was opened")
        if state.close_error is not None:
            raise FinalizationError(f"stream {stream_id}: {state.close_error}")
        if not state.close_present:
            raise FinalizationError(f"stream {stream_id} has no durable stream_close.json")

        expected = open_stream.writer.committed_canonical
        actual = {chunk.chunk_id: chunk for chunk in state.chunks}
        missing = sorted(set(expected) - set(actual))
        if missing:
            raise FinalizationError(
                f"stream {stream_id}: committed chunk(s) {missing} are absent from chunks.jsonl"
            )
        extra = sorted(set(actual) - set(expected))
        if extra:
            raise FinalizationError(
                f"stream {stream_id}: chunks.jsonl holds chunk(s) {extra} this writer never "
                "committed"
            )
        for chunk_id, canonical in expected.items():
            if actual[chunk_id].record.canonical_bytes != canonical:
                raise FinalizationError(
                    f"stream {stream_id}: chunk {chunk_id} on disk is not the record this "
                    "writer committed"
                )
        if state.order_errors:
            raise FinalizationError(
                f"stream {stream_id}: chain ordering is invalid: {list(state.order_errors)}"
            )
        for chunk in state.chunks:
            problems = (
                list(chunk.artifact_errors)
                + list(chunk.schema_errors)
                + list(chunk.row_semantics_errors)
                + list(chunk.reference_errors)
                + list(chunk.payload_errors)
            )
            if chunk.packet_error is not None:
                problems.append(chunk.packet_error)
            if problems:
                raise FinalizationError(
                    f"stream {stream_id}: chunk {chunk.chunk_id} does not conform: {problems}"
                )
        if state.unexpected_files or state.orphan_artifacts:
            raise FinalizationError(
                f"stream {stream_id}: raw directory holds file(s) the layout does not define "
                f"or no commit names: {list(state.unexpected_files + state.orphan_artifacts)}"
            )


def _control_sha256(
    paths: PackagePaths, stream_ids: list[str], schema_ids: set[str]
) -> dict[str, str]:
    """Step 9: hash exactly the derived control set (V10, D30).

    Raw artifacts are deliberately absent: they are sealed transitively through
    each stream's ``chunks.jsonl``, so a raw artifact hash is persisted exactly
    once in the whole package.

    A symlink is refused rather than followed: sealing a link would record the
    hash of bytes that live outside the package, and the package would then
    verify without actually containing its own raw data.
    """
    links = find_symlinks(paths.root)
    if links:
        raise FinalizationError(
            f"package contains symlink(s) and cannot be sealed: "
            f"{[p.relative_to(paths.root).as_posix() for p in links]}"
        )
    control: dict[str, str] = {}
    for relative in package_layout.expected_control_paths(stream_ids, schema_ids):
        target = paths.root / relative
        if not target.is_file():
            raise FinalizationError(f"control file {relative} is absent and cannot be sealed")
        control[relative] = sha256_file(target)
    return control


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

    # Step 2 — durable closure BEFORE the terminal lifecycle record. A stream
    # the operator never closed explicitly is closed with its intended status
    # here; one that already has a file keeps it, because it is immutable.
    for stream_id, open_stream in sorted(writer.streams.items()):
        if open_stream.closed:
            continue
        close_path = writer.paths.stream(stream_id).stream_close
        if not close_path.exists():
            write_stream_close(close_path, open_stream.close_status)
        open_stream.closed = True
    # The same rule the fatal-error path enforces: no terminal lifecycle record
    # may be written while an opened stream lacks durable closure (D33).
    unclosed = [
        stream_id
        for stream_id in writer.streams
        if not writer.paths.stream(stream_id).stream_close.is_file()
    ]
    if unclosed:
        raise FinalizationError(
            f"stream(s) {sorted(unclosed)} have no durable stream_close.json; a terminal "
            "lifecycle record must not claim finalization reached a terminal state"
        )

    # Structural first, for EVERY outcome: the package must be sealable at all
    # before a terminal lifecycle record claims finalization reached one.
    preflight = read_all_physical_streams(writer.paths)
    _assert_sealable(writer, run, preflight)

    # A sealed COMPLETED must additionally be true at the moment it is written.
    # The verifier would reject the package later either way, but a lifecycle
    # log that says COMPLETED when it was not is a lie recorded in immutable
    # data.
    if outcome is RecordingOutcome.COMPLETED:
        _assert_completable(writer, run, closure_condition, preflight)

    # Steps 3, 4 — lifecycle, so the sealed prefix is complete before hashing.
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

    # Step 5 — events are sealed here; nothing appends to them afterwards.
    events_path = writer.paths.events
    if not events_path.exists():
        atomic_write(events_path, b"")
    events_bytes = events_path.read_bytes()

    # Step 6 — the schemas the sealed events actually reference travel with the
    # package. The set must equal those ids exactly, so it is derived from the
    # sealed bytes rather than from what the writer remembers emitting.
    schema_ids, schema_error = package_layout.referenced_schema_ids(events_bytes)
    if schema_error is not None:
        raise FinalizationError(f"events log cannot be sealed: {schema_error}")
    writer.snapshot_schemas(schema_ids)

    # Step 7 — zero-state annotation head. Required for every finalized package.
    if not writer.paths.annotations_head.exists():
        annotations_mod.write_initial_head(writer.paths.annotations_head)

    # Step 8 — verify every durable pre-manifest fact.
    physical = read_all_physical_streams(writer.paths)
    _assert_streams_on_disk(writer, physical)
    stream_ids = sorted(physical)

    # The manifest is an integrity root, so it must not be written over a
    # package the layout does not close — for ANY outcome, not only COMPLETED.
    # A sealed ABORTED package carrying an unexpected immutable file is just as
    # unverifiable as a sealed COMPLETED one.
    layout = package_layout.scan_layout(writer.paths, schema_ids)
    problems = (
        list(layout.unexpected)
        + [f"schemas/{s}.json (unreferenced)" for s in layout.unreferenced_schemas]
        + [f"schemas/{s}.json (missing)" for s in layout.missing_schemas]
    )
    if problems:
        raise FinalizationError(f"the package file set is not closed: {problems}")
    noncanonical = package_layout.noncanonical_documents(writer.paths, stream_ids)
    if noncanonical:
        raise FinalizationError(f"document(s) are not canonical on disk: {noncanonical}")

    # Step 9 — the integrity root.
    control = _control_sha256(writer.paths, stream_ids, schema_ids)

    lifecycle_bytes = writer.paths.lifecycle.read_bytes()
    utc_ns, monotonic_ns = now_reading()
    manifest = Manifest(
        sealed_at=ClockReading(utc_ns=utc_ns, monotonic_ns=monotonic_ns),
        lifecycle_seal=SealPointer(
            sealed_len=len(lifecycle_bytes),
            sealed_sha256=sha256_bytes(lifecycle_bytes),
        ),
        events_sha256=sha256_bytes(events_bytes),
        control_sha256=control,
    )

    # Steps 10, 11 — the manifest pair. Step 11 is the finalization marker.
    manifest_bytes = canonical_json.canonicalize(manifest.model_dump(mode="json"))
    atomic_write_new(writer.paths.manifest, manifest_bytes)
    digest = sha256_bytes(manifest_bytes)
    atomic_write_new(writer.paths.manifest_sha256, (digest + "\n").encode("utf-8"))

    # Step 12 — refresh the derived index. The package is already sealed, so a
    # failure here costs nothing but a stale row.
    if data_root is not None:
        with contextlib.suppress(sqlite3.Error):
            registry.upsert(data_root, writer.paths)

    return FinalizationResult(manifest=manifest, manifest_sha256=digest)
