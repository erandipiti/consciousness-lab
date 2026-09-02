"""Session writer: the operator-facing handle that ties the pieces together.

This is deliberately synchronous and single-threaded, and it stays that way.
The generic asynchronous multi-stream recorder with fan-in orchestration is
**CL-003** (`consciousness_lab.session.recorder`); it sits *above* this class
and funnels every stream into a single thread, so nothing here needs a lock.
Concurrency that reaches this file is a defect in the caller.

What lives here is declaring streams, committing chunks, recording events,
closing streams durably, and finalizing.
"""

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from consciousness_lab.session.lifecycle import LifecycleError, LifecycleLog, now_reading
from consciousness_lab.session.model import (
    ClockReading,
    ClosureCondition,
    EventRecord,
    LifecycleState,
    RawRef,
    RecordingOutcome,
    Run,
    StreamClose,
    StreamCloseStatus,
    StreamDescriptor,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import (
    ImmutableFileError,
    append_line,
    atomic_write_new,
    sha256_bytes,
)
from consciousness_lab.storage.chunk_writer import ChunkWriter, FaultHook, PendingChunk
from consciousness_lab.storage.paths import PackagePaths

#: Minimal generic event payload schemas. Enough to exercise CL-002B and no
#: more: no Focus-specific event types exist, and none may be added here.
EVENT_SCHEMAS: dict[str, dict[str, Any]] = {
    "recording_start.v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "recording_start.v1",
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
    "recording_stop.v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "recording_stop.v1",
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
    "clock_snapshot.v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "clock_snapshot.v1",
        "type": "object",
        "additionalProperties": False,
        "required": ["monotonic_ns", "utc_ns"],
        "properties": {
            "monotonic_ns": {"type": "string"},
            "utc_ns": {"type": "string"},
            "monotonic_clock_id": {"type": "string"},
            "utc_clock_id": {"type": "string"},
            "sync_source": {"type": "string"},
            "sync_status": {"type": "string"},
            "utc_quality": {"type": "string"},
        },
    },
    "technical_error.v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "technical_error.v1",
        "type": "object",
        "additionalProperties": False,
        "required": ["message"],
        "properties": {"message": {"type": "string"}, "stream_id": {"type": "string"}},
    },
    "device_disconnected.v1": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "device_disconnected.v1",
        "type": "object",
        "additionalProperties": False,
        "required": ["stream_id"],
        "properties": {"stream_id": {"type": "string"}, "reason": {"type": "string"}},
    },
}


class SealedPackageError(RuntimeError):
    """A write was attempted against sealed or already-written package content."""


class FatalWriteError(RuntimeError):
    """A raw write failed fatally (ENOSPC, I/O error). The session was closed."""


def _validate_payload(schema_id: str, payload: dict[str, Any]) -> None:
    """Check a payload against its declared schema before it is written.

    A deliberately small subset of JSON Schema — required keys, and additional
    properties when the schema forbids them. Enough to stop a typo becoming a
    permanently unreadable event, without taking a validator dependency for the
    five schemas CL-002B defines.
    """
    schema = EVENT_SCHEMAS[schema_id]
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"{schema_id}: payload is missing required keys {missing}")
    if schema.get("additionalProperties") is False:
        extra = [key for key in payload if key not in properties]
        if extra:
            raise ValueError(f"{schema_id}: payload has undeclared keys {extra}")


@dataclass
class OpenStream:
    """One stream open for writing.

    ``close_status`` is the *intended* status held in memory; it becomes a fact
    only when ``close_stream`` writes ``stream_close.json``. Nothing downstream
    reads this field: the durable file is the sole authority (D33).
    """

    descriptor: StreamDescriptor
    descriptor_sha256: str
    writer: ChunkWriter
    close_status: StreamCloseStatus = StreamCloseStatus.CLEAN
    closed: bool = False


def write_stream_close(path: Path, status: StreamCloseStatus) -> None:
    """Durably record a stream's terminal closure (v2 §7.1, D33).

    ``tmp -> fsync -> rename -> fsync(dir)``, write-once. A stream that closed
    earlier already has its file and must not have it rewritten, so this refuses
    rather than clobbering: two closure records for one stream would be exactly
    the drifting-copies defect v2 exists to remove.
    """
    body = canonical_json.canonicalize(StreamClose(close_status=status).model_dump(mode="json"))
    atomic_write_new(path, body)


@dataclass
class SessionWriter:
    """A live, unfinalized session package."""

    paths: PackagePaths
    lifecycle: LifecycleLog
    run: Run | None = None
    streams: dict[str, OpenStream] = field(default_factory=dict)
    _event_seq: int = 0
    _used_schemas: set[str] = field(default_factory=set)
    _failed: bool = False

    def _assert_writable(self, what: str) -> None:
        """Refuse any mutation once the package is sealed or terminally closed.

        A sealed package is immutable; a CLOSED lifecycle is terminal. Writing
        after either produces a package the verifier rejects — and the write
        lands in immutable acquisition data, so there is nothing to undo. This
        reads the DURABLE state, not a flag, because a second writer or a
        resumed process must be stopped by the same rule.
        """
        if self.paths.manifest.exists() or self.paths.manifest_sha256.exists():
            raise SealedPackageError(
                f"{self.paths.root.name} is sealed; {what} would mutate a finalized package"
            )
        if self.lifecycle.state is LifecycleState.CLOSED:
            raise SealedPackageError(
                f"{self.paths.root.name} is CLOSED, which is terminal; {what} is refused"
            )
        if self.lifecycle.state is LifecycleState.FINALIZING:
            raise SealedPackageError(
                f"{self.paths.root.name} is FINALIZING; {what} would race the seal"
            )

    @classmethod
    def open(cls, paths: PackagePaths) -> "SessionWriter":
        """Open an unsealed package for writing.

        A sealed or closed package is refused outright. Without this guard a
        second writer could overwrite ``run.json`` and the stream descriptors
        before the lifecycle log got a chance to reject the transition, which
        would mutate sealed data (AGENTS.md §5, D23).
        """
        if paths.manifest.exists() or paths.manifest_sha256.exists():
            raise SealedPackageError(
                f"{paths.root.name} is sealed; a sealed package is never reopened for writing"
            )
        log = LifecycleLog(paths.lifecycle)
        if log.state is LifecycleState.CLOSED:
            raise SealedPackageError(f"{paths.root.name} is CLOSED; CLOSED is terminal")
        return cls(paths=paths, lifecycle=log)

    def start_recording(self, run: Run) -> None:
        """Seal ``run.json`` and enter RECORDING. Sealed once, never rewritten."""
        self._assert_writable("starting recording")
        if self.run is not None or self.paths.run.exists():
            raise SealedPackageError("run.json is sealed once and cannot be rewritten")
        atomic_write_new(
            self.paths.run,
            canonical_json.canonicalize(run.model_dump(mode="json", exclude_none=True)),
        )
        self.run = run
        self.lifecycle.append(LifecycleState.RECORDING)
        self.emit_event("RECORDING_START", "recording_start.v1", origin="system")
        self.emit_clock_snapshot()

    def open_stream(self, descriptor: StreamDescriptor) -> OpenStream:
        """Seal a stream descriptor and open its chunk writer."""
        self._assert_writable(f"opening stream {descriptor.stream_id}")
        if descriptor.stream_id in self.streams:
            raise SealedPackageError(f"stream {descriptor.stream_id} is already open")
        stream_paths = self.paths.stream(descriptor.stream_id)
        if stream_paths.descriptor.exists():
            raise SealedPackageError(
                f"stream {descriptor.stream_id} already has a sealed descriptor"
            )
        stream_paths.root.mkdir(parents=True, exist_ok=True)
        body = canonical_json.canonicalize(descriptor.model_dump(mode="json", exclude_none=True))
        atomic_write_new(stream_paths.descriptor, body)
        # Created empty at open so every stream directory has the same shape.
        # Otherwise a zero-chunk stream is indistinguishable from a stream whose
        # index was deleted, and the verifier would have to guess.
        stream_paths.chunks_index.touch()
        opened = OpenStream(
            descriptor=descriptor,
            descriptor_sha256=sha256_bytes(body),
            writer=ChunkWriter(stream_paths, descriptor),
        )
        self.streams[descriptor.stream_id] = opened
        return opened

    def commit_chunk(
        self, stream_id: str, pending: PendingChunk, *, fault: FaultHook | None = None
    ) -> None:
        """Commit a chunk, closing the session honestly if the write fails fatally.

        ``ENOSPC`` and I/O errors are *diagnosed* causes: the process is alive
        and knows why it is dying, so the session closes
        ``CLEAN / TECHNICAL_FAILURE`` with the error recorded, never
        ``UNCLASSIFIED`` (spec 12.3, D16).
        """
        self._assert_writable(f"committing a chunk to {stream_id}")
        stream = self.streams[stream_id]
        # Closure is terminal for a stream, exactly as CLOSED is for a session.
        # Committing after it would make the durable close record false, and
        # that record is immutable — so the commit is refused, not the record.
        if stream.closed or self.paths.stream(stream_id).stream_close.exists():
            raise SealedPackageError(
                f"stream {stream_id} has a durable stream_close.json; it accepts no more chunks"
            )
        try:
            stream.writer.commit(pending, fault=fault)
        except OSError as exc:
            self.fail_technical(f"{type(exc).__name__}: {exc}", stream_id=stream_id)
            raise FatalWriteError(
                f"raw write failed for {stream_id}; session closed TECHNICAL_FAILURE"
            ) from exc

    def close_all_streams(self, *, failed: str | None = None) -> list[str]:
        """Durably terminate every open stream. Returns the ids left unclosed.

        D33 applies on **every** terminal path, not only ``finalize``. The
        diagnosed failing stream gets ``FAILED``; the others get ``CLEAN``,
        because that is what was actually observed — they were healthy and are
        being closed as part of an orderly teardown. Nothing invents
        ``DISCONNECTED`` or ``FAILED`` for a stream that showed neither.

        Best-effort by necessity: the disk that just refused a chunk may refuse
        these much smaller writes too. Whatever cannot be made durable is
        returned, and the caller must not then claim a terminal state.
        """
        unclosed: list[str] = []
        for candidate, stream in sorted(self.streams.items()):
            if stream.closed or self.paths.stream(candidate).stream_close.exists():
                stream.closed = True
                continue
            status = StreamCloseStatus.FAILED if candidate == failed else StreamCloseStatus.CLEAN
            try:
                self.close_stream(candidate, status)
            except (OSError, ImmutableFileError, SealedPackageError):
                unclosed.append(candidate)
        return unclosed

    def fail_technical(self, message: str, *, stream_id: str | None = None) -> None:
        """Record a diagnosed fatal error and close the session.

        Best-effort by necessity: the disk that just refused a chunk may also
        refuse these much smaller writes. Anything that cannot be recorded is
        left to the recovery scanner, which reports an unclean close rather than
        inventing an outcome.

        **Every opened stream gets durable closure before CLOSED is written.**
        CLOSED is terminal, so a stream left without a close record after it can
        never be given one — recovery would be permanently blocked on a fact
        this path could still have recorded. If closure cannot be made durable,
        no CLOSED record is written at all: the session is left interrupted,
        which is true, rather than marked terminal, which would not be.
        """
        if self._failed:
            return
        self._failed = True
        unclosed = self.close_all_streams(failed=stream_id)
        with contextlib.suppress(OSError, ValueError, KeyError):
            payload = {"message": message}
            if stream_id:
                payload["stream_id"] = stream_id
            self.emit_event(
                "TECHNICAL_ERROR", "technical_error.v1", origin="system", payload=payload
            )
        if unclosed:
            return
        with contextlib.suppress(OSError, LifecycleError):
            if self.lifecycle.state in (LifecycleState.ALLOCATED, LifecycleState.RECORDING):
                self.lifecycle.append(LifecycleState.FINALIZING)
            self.lifecycle.append(
                LifecycleState.CLOSED,
                closure_condition=ClosureCondition.CLEAN,
                recording_outcome=RecordingOutcome.TECHNICAL_FAILURE,
                outcome_reason=message,
            )

    def close_stream(self, stream_id: str, status: StreamCloseStatus) -> None:
        """Durably record how a stream ended. Write-once (D33).

        The file is the fact. A crash after this point cannot lose the closure,
        which is the whole reason v2 moved it out of the manifest: terminal
        closure that lives only in RAM until the final seal is a fact recovery
        cannot reconstruct.
        """
        stream = self.streams[stream_id]
        if stream.closed:
            raise SealedPackageError(
                f"stream {stream_id} already has a durable stream_close.json; it is immutable"
            )
        try:
            write_stream_close(self.paths.stream(stream_id).stream_close, status)
        except ImmutableFileError as exc:
            raise SealedPackageError(str(exc)) from exc
        stream.close_status = status
        stream.closed = True

    def emit_event(
        self,
        name: str,
        payload_schema: str,
        *,
        origin: str = "system",
        payload: dict[str, Any] | None = None,
        raw_ref: RawRef | None = None,
    ) -> EventRecord:
        self._assert_writable(f"emitting event {name}")
        if payload_schema not in EVENT_SCHEMAS:
            raise KeyError(f"unknown event payload schema {payload_schema}")
        _validate_payload(payload_schema, payload or {})
        utc_ns, monotonic_ns = now_reading()
        record = EventRecord.model_validate(
            {
                "event_seq": self._event_seq,
                "event_name": name,
                "payload_schema": payload_schema,
                "origin": origin,
                "host_arrival_monotonic_ns": monotonic_ns,
                "host_arrival_utc_ns": utc_ns,
                "raw_ref": raw_ref.model_dump(mode="json") if raw_ref else None,
                "payload": payload or {},
            }
        )
        body = record.model_dump(mode="json", exclude_none=True, exclude={"record_sha256"})
        append_line(self.paths.events, canonical_json.dump_line(body))
        self._event_seq += 1
        self._used_schemas.add(payload_schema)
        return record

    def emit_clock_snapshot(self) -> EventRecord:
        """Pair the two clocks so a UTC step shows as a visible discontinuity."""
        utc_ns, monotonic_ns = now_reading()
        reading = ClockReading(utc_ns=utc_ns, monotonic_ns=monotonic_ns)
        payload = reading.model_dump(mode="json")
        payload["sync_source"] = "unknown"
        payload["sync_status"] = "unknown"
        return self.emit_event(
            "CLOCK_SNAPSHOT", "clock_snapshot.v1", origin="system", payload=payload
        )

    def snapshot_schemas(self, schema_ids: set[str] | None = None) -> list[tuple[str, Path]]:
        """Copy exactly the referenced schemas into ``schemas/`` (v2 §9.2).

        A package must be interpretable years later with no access to this
        repository, so the schemas travel with it. The set written is the set
        the **sealed events log references**, not what this writer happens to
        remember emitting: the physical ``schemas/`` set must equal that exactly,
        so no unreferenced snapshot can sit outside the integrity DAG.
        """
        wanted = sorted(self._used_schemas if schema_ids is None else schema_ids)
        out: list[tuple[str, Path]] = []
        for schema_id in wanted:
            if schema_id not in EVENT_SCHEMAS:
                raise KeyError(
                    f"the sealed events log references schema {schema_id!r}, which this "
                    "writer cannot snapshot"
                )
            body = canonical_json.canonicalize(EVENT_SCHEMAS[schema_id])
            target = self.paths.schemas / f"{schema_id}.json"
            if not target.exists():
                atomic_write_new(target, body)
            out.append((schema_id, target))
        return out
