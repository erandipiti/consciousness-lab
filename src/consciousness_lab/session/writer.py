"""Session writer: the operator-facing handle that ties the pieces together.

This is deliberately synchronous and single-threaded. The generic asynchronous
multi-stream recorder with fan-in orchestration is **CL-003**, not this ticket.
What lives here is only enough to exercise Session Package v1 end to end:
declare streams, commit chunks, record events, finalize.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from consciousness_lab.session.lifecycle import LifecycleLog, now_reading
from consciousness_lab.session.model import (
    ClockReading,
    EventRecord,
    LifecycleState,
    Run,
    StreamCloseStatus,
    StreamDescriptor,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import append_line, atomic_write, sha256_bytes
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


@dataclass
class OpenStream:
    descriptor: StreamDescriptor
    descriptor_sha256: str
    writer: ChunkWriter
    close_status: StreamCloseStatus = StreamCloseStatus.CLEAN


@dataclass
class SessionWriter:
    """A live, unfinalized session package."""

    paths: PackagePaths
    lifecycle: LifecycleLog
    run: Run | None = None
    streams: dict[str, OpenStream] = field(default_factory=dict)
    _event_seq: int = 0
    _used_schemas: set[str] = field(default_factory=set)

    @classmethod
    def open(cls, paths: PackagePaths) -> "SessionWriter":
        return cls(paths=paths, lifecycle=LifecycleLog(paths.lifecycle))

    def start_recording(self, run: Run) -> None:
        """Seal ``run.json`` and enter RECORDING."""
        if self.run is not None:
            raise RuntimeError("run.json is sealed once and cannot be rewritten")
        self.run = run
        atomic_write(
            self.paths.run,
            canonical_json.canonicalize(run.model_dump(mode="json", exclude_none=True)),
        )
        self.lifecycle.append(LifecycleState.RECORDING)
        self.emit_event("RECORDING_START", "recording_start.v1", origin="system")
        self.emit_clock_snapshot()

    def open_stream(self, descriptor: StreamDescriptor) -> OpenStream:
        """Seal a stream descriptor and open its chunk writer."""
        if descriptor.stream_id in self.streams:
            raise RuntimeError(f"stream {descriptor.stream_id} is already open")
        stream_paths = self.paths.stream(descriptor.stream_id)
        stream_paths.root.mkdir(parents=True, exist_ok=True)
        body = canonical_json.canonicalize(descriptor.model_dump(mode="json", exclude_none=True))
        atomic_write(stream_paths.descriptor, body)
        opened = OpenStream(
            descriptor=descriptor,
            descriptor_sha256=sha256_bytes(body),
            writer=ChunkWriter(stream_paths, descriptor, sha256_bytes(body)),
        )
        self.streams[descriptor.stream_id] = opened
        return opened

    def commit_chunk(
        self, stream_id: str, pending: PendingChunk, *, fault: FaultHook | None = None
    ) -> None:
        self.streams[stream_id].writer.commit(pending, fault=fault)

    def close_stream(self, stream_id: str, status: StreamCloseStatus) -> None:
        """Record how a stream ended. A non-CLEAN required stream blocks completion."""
        self.streams[stream_id].close_status = status

    def emit_event(
        self,
        name: str,
        payload_schema: str,
        *,
        origin: str = "system",
        payload: dict[str, Any] | None = None,
        raw_ref: dict[str, Any] | None = None,
    ) -> EventRecord:
        if payload_schema not in EVENT_SCHEMAS:
            raise KeyError(f"unknown event payload schema {payload_schema}")
        utc_ns, monotonic_ns = now_reading()
        record = EventRecord.model_validate(
            {
                "event_seq": self._event_seq,
                "event_name": name,
                "payload_schema": payload_schema,
                "origin": origin,
                "host_arrival_monotonic_ns": monotonic_ns,
                "host_arrival_utc_ns": utc_ns,
                "raw_ref": raw_ref,
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

    def snapshot_schemas(self) -> list[tuple[str, Path, bytes]]:
        """Copy every schema actually used into ``schemas/`` (spec §11).

        A package must be interpretable years later with no access to this
        repository, so the schemas travel with it.
        """
        out: list[tuple[str, Path, bytes]] = []
        for schema_id in sorted(self._used_schemas):
            body = canonical_json.canonicalize(EVENT_SCHEMAS[schema_id])
            target = self.paths.schemas / f"{schema_id}.json"
            atomic_write(target, body)
            out.append((schema_id, target, body))
        return out
