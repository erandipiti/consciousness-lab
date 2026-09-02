"""Immutable raw chunk writing (v2 §4.1, §6; D10, D13, D28).

A chunk is real if and only if its record appears in ``chunks.jsonl``. There is
no sidecar in v2: ``chunks.jsonl`` is the sole persisted commit authority, and
files on disk without a record in it are orphans that recovery reports and never
adopts. One commit record covers every artifact the chunk produced, so a crash
can never leave committed packet metadata whose sample values are gone.

Write order per chunk (v2 §4.1):

    payloads (transport_payload only) -> packets -> observations -> samples
    each: .part -> flush -> fsync -> close -> rename -> fsync(dir)
    then: append the canonical record to chunks.jsonl -> fsync

Arrow IPC *stream* format is used rather than Parquet: a truncated IPC stream
still yields every complete record batch, while a truncated Parquet file never
got its footer and is a total loss.
"""

import hashlib
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from consciousness_lab.session.model import ChunkCommit, StreamDescriptor
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage import payload as payload_mod
from consciousness_lab.storage.arrow_schema import (
    OBSERVATIONS_SCHEMA,
    PACKETS_SCHEMA,
    samples_schema,
)
from consciousness_lab.storage.checksums import (
    PART_SUFFIX,
    append_line,
    fsync_dir,
    sha256_file,
)
from consciousness_lab.storage.observations import validate_observations
from consciousness_lab.storage.paths import StreamPaths

#: Stages a fault-injection test can interrupt. Named so tests describe the
#: failure they are simulating rather than patching internals. ``sidecar_written``
#: is gone with the sidecar itself (D28).
STAGES = (
    "payloads_written",
    "packets_written",
    "observations_written",
    "samples_written",
    "index_appended",
)

FaultHook = Callable[[str], None]


class ChunkWriteError(RuntimeError):
    """A chunk could not be committed. The chunk is not real."""


@dataclass
class PendingChunk:
    """Rows accumulated for one chunk, before any of it touches disk."""

    packets: list[dict[str, Any]] = field(default_factory=list)
    samples: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    payloads: list[tuple[int, bytes]] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.packets)


def _write_arrow(path: Path, schema: pa.Schema, rows: Sequence[dict[str, Any]]) -> None:
    """Write one Arrow IPC stream file atomically, never over an existing one."""
    if path.exists():
        raise ChunkWriteError(f"{path} already exists; committed raw chunks are immutable")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + PART_SUFFIX)
    table = pa.Table.from_pylist(list(rows), schema=schema)
    with part.open("wb") as handle:
        with pa.ipc.new_stream(handle, schema) as writer:
            writer.write_table(table)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(part, path)
    fsync_dir(path.parent)


def _write_payloads(path: Path, records: Sequence[tuple[int, bytes]]) -> None:
    if path.exists():
        raise ChunkWriteError(f"{path} already exists; committed raw chunks are immutable")
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + PART_SUFFIX)
    with part.open("wb") as handle:
        for packet_seq, blob in records:
            handle.write(payload_mod.frame(packet_seq, blob))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(part, path)
    fsync_dir(path.parent)


class ChunkWriter:
    """Writes immutable committed chunks for one raw stream."""

    def __init__(self, paths: StreamPaths, descriptor: StreamDescriptor) -> None:
        self.paths = paths
        self.descriptor = descriptor
        # Refuse to reuse a stream directory that already holds chunks: a fresh
        # writer would restart at chunk 0 and overwrite immutable raw files.
        index_has_commits = paths.chunks_index.is_file() and bool(
            paths.chunks_index.read_bytes().strip()
        )
        if index_has_commits or any(
            (paths.root / kind).exists()
            for kind in ("payloads", "packets", "observations", "samples")
        ):
            raise ChunkWriteError(
                f"{paths.root} already holds raw artifacts; committed chunks are immutable"
            )
        self._next_chunk_id = 0
        self._prev_hash = canonical_json.ZERO_HASH
        #: Last packet_seq committed to this stream, for the chain-level
        #: ordering rule that per-chunk validation cannot see.
        self._last_packet_seq: int | None = None
        self._pending_last_packet_seq: int | None = None
        self._committed: list[ChunkCommit] = []
        #: Canonical bytes of each record as written, so the finalizer can
        #: compare what the writer committed against what is on disk by RECORD
        #: IDENTITY rather than by parsed-model equality.
        self._committed_canonical: dict[int, bytes] = {}

    @property
    def committed(self) -> list[ChunkCommit]:
        return list(self._committed)

    @property
    def chain_head(self) -> str | None:
        """SHA-256 of the last record's canonical bytes, derived not stored."""
        if not self._committed_canonical:
            return None
        last = max(self._committed_canonical)
        return hashlib.sha256(self._committed_canonical[last]).hexdigest()

    @property
    def committed_canonical(self) -> dict[int, bytes]:
        """Canonical bytes of every record this writer committed, by chunk id."""
        return dict(self._committed_canonical)

    def commit(self, pending: PendingChunk, *, fault: FaultHook | None = None) -> ChunkCommit:
        """Seal one chunk. Raises without committing if anything goes wrong.

        ``fault`` is invoked after each named stage so tests can interrupt the
        writer deterministically, without patching internals and without
        depending on a timing race.
        """
        if not pending.packets:
            raise ChunkWriteError("refusing to commit an empty chunk")

        expects_payload = self.descriptor.expects_payload_artifact
        if expects_payload and len(pending.payloads) != len(pending.packets):
            raise ChunkWriteError("transport_payload stream requires one payload record per packet")
        if not expects_payload and pending.payloads:
            raise ChunkWriteError(
                f"raw_capture_level={self.descriptor.acquisition.raw_capture_level.value} "
                "must not produce a payload artifact (v2 §13)"
            )

        # Arrow types cannot express "exactly one value column matches
        # value_type", so the rule is enforced before anything reaches disk —
        # by the same validator read-time verification uses (D31).
        validate_observations(pending.observations)

        chunk_id = self._next_chunk_id
        artifact_sha256: dict[str, str] = {}

        if expects_payload:
            target = self.paths.artifact("payloads", chunk_id)
            _write_payloads(target, pending.payloads)
            artifact_sha256["payloads"] = sha256_file(target)
        self._fire(fault, "payloads_written")

        packets_path = self.paths.artifact("packets", chunk_id)
        _write_arrow(packets_path, PACKETS_SCHEMA, pending.packets)
        artifact_sha256["packets"] = sha256_file(packets_path)
        self._fire(fault, "packets_written")

        obs_path = self.paths.artifact("observations", chunk_id)
        _write_arrow(obs_path, OBSERVATIONS_SCHEMA, pending.observations)
        artifact_sha256["observations"] = sha256_file(obs_path)
        self._fire(fault, "observations_written")

        samples_path = self.paths.artifact("samples", chunk_id)
        _write_arrow(
            samples_path,
            samples_schema(self.descriptor.layout, self.descriptor.n_channels),
            pending.samples,
        )
        artifact_sha256["samples"] = sha256_file(samples_path)
        self._fire(fault, "samples_written")

        # Nothing is committed until the artifacts just written satisfy the same
        # rules the verifier will apply to them. Checked against the PHYSICAL
        # bytes, by the same reconciler verification uses, so there is one
        # definition of a conforming chunk rather than a writer-side copy that
        # can drift from it.
        #
        # The artifacts stay on disk if this fails. They are orphans — files
        # with no commit record — which is exactly what a crash at this point
        # leaves, and recovery already reports that shape without adopting it.
        problem = self._conformance_error(chunk_id, artifact_sha256)
        if problem is not None:
            raise ChunkWriteError(
                f"chunk {chunk_id} does not conform and was not committed: {problem}"
            )

        record = ChunkCommit(
            chunk_id=chunk_id,
            prev_record_sha256=self._prev_hash,
            artifact_sha256=artifact_sha256,
        )
        # No record_sha256: the record's own hash is its canonical bytes'
        # digest, which the next record carries as prev_record_sha256 and any
        # reader recomputes (v2 §6, D29).
        body = record.model_dump(mode="json", exclude_none=True)
        canonical = canonical_json.canonicalize(body)
        digest = hashlib.sha256(canonical).hexdigest()

        append_line(self.paths.chunks_index, canonical + b"\n")
        self._fire(fault, "index_appended")

        self._prev_hash = digest
        self._next_chunk_id += 1
        self._last_packet_seq = self._pending_last_packet_seq
        self._committed.append(record)
        self._committed_canonical[chunk_id] = canonical
        return record

    def _conformance_error(self, chunk_id: int, artifact_sha256: dict[str, str]) -> str | None:
        """Why the artifacts just written are not a conforming chunk, or ``None``.

        Imported here rather than at module scope: ``stream_state`` reads chunk
        records, which this module writes, so a top-level import would be
        circular. The point of reusing it is that a chunk cannot be committed
        under rules different from the ones it will later be judged by.
        """
        from consciousness_lab.storage.stream_state import (
            ChunkRecordOnDisk,
            reconcile_chunk,
        )

        probe = ChunkCommit(
            chunk_id=chunk_id,
            prev_record_sha256=self._prev_hash,
            artifact_sha256=artifact_sha256,
        )
        record = ChunkRecordOnDisk(
            model=probe,
            canonical_bytes=canonical_json.canonicalize(probe.model_dump(mode="json")),
            payloads_key_present="payloads" in artifact_sha256,
        )
        chunk = reconcile_chunk(self.paths.root, record, self.descriptor)
        problems = (
            list(chunk.artifact_errors)
            + list(chunk.schema_errors)
            + list(chunk.row_semantics_errors)
            + list(chunk.reference_errors)
            + list(chunk.payload_errors)
        )
        if chunk.packet_error is not None:
            problems.append(chunk.packet_error)
        # Chain-level ordering: packet_seq is strictly increasing for the whole
        # stream, so this chunk must begin after the previous one ended. Per
        # chunk validation cannot see that, and the verifier checks it later.
        first = chunk.physical_first_packet_seq
        if (
            self._last_packet_seq is not None
            and first is not None
            and first <= self._last_packet_seq
        ):
            problems.append(
                f"begins at packet {first}, which does not follow the previous chunk "
                f"ending at {self._last_packet_seq}"
            )
        if problems:
            return "; ".join(problems)
        self._pending_last_packet_seq = chunk.physical_last_packet_seq
        return None

    @staticmethod
    def _fire(fault: FaultHook | None, stage: str) -> None:
        if fault is not None:
            fault(stage)
