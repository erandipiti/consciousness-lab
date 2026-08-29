"""Immutable raw chunk writing (spec §12.2; D10, D13).

A chunk is real if and only if its record appears in ``chunks.jsonl``. Files on
disk without such a record are orphans: recovery reports them and never adopts
them. One commit record covers every artifact the chunk produced, so a crash
can never leave committed packet metadata whose sample values are gone.

Write order per chunk:

    payloads (transport_payload only) -> packets -> observations -> samples
    each: .part -> flush -> fsync -> close -> rename -> fsync(dir)
    then: NNNNNN.commit.json (atomic)
    then: append the same record to chunks.jsonl (authoritative)

Arrow IPC *stream* format is used rather than Parquet: a truncated IPC stream
still yields every complete record batch, while a truncated Parquet file never
got its footer and is a total loss.
"""

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from consciousness_lab.session.model import ChunkArtifact, ChunkCommit, StreamDescriptor
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
    atomic_write,
    fsync_dir,
    sha256_file,
)
from consciousness_lab.storage.paths import StreamPaths

#: Stages a fault-injection test can interrupt. Named so tests describe the
#: failure they are simulating rather than patching internals.
STAGES = (
    "payloads_written",
    "packets_written",
    "observations_written",
    "samples_written",
    "sidecar_written",
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
    """Write one Arrow IPC stream file atomically."""
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
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + PART_SUFFIX)
    with part.open("wb") as handle:
        for packet_seq, blob in records:
            handle.write(payload_mod.frame(packet_seq, blob))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(part, path)
    fsync_dir(path.parent)


def _artifact(path: Path, relative: str) -> ChunkArtifact:
    return ChunkArtifact(path=relative, sha256=sha256_file(path), bytes=path.stat().st_size)


class ChunkWriter:
    """Writes immutable committed chunks for one raw stream."""

    def __init__(
        self,
        paths: StreamPaths,
        descriptor: StreamDescriptor,
        descriptor_sha256: str,
    ) -> None:
        self.paths = paths
        self.descriptor = descriptor
        self.descriptor_sha256 = descriptor_sha256
        self._next_chunk_id = 0
        self._prev_hash = canonical_json.ZERO_HASH
        self._committed: list[ChunkCommit] = []

    @property
    def committed(self) -> list[ChunkCommit]:
        return list(self._committed)

    @property
    def chain_head(self) -> str | None:
        return self._committed[-1].record_sha256 if self._committed else None

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
                "must not produce a payload artifact (spec 9.1)"
            )

        chunk_id = self._next_chunk_id

        payload_artifact: ChunkArtifact | None = None
        if expects_payload:
            rel = self.paths.relative_artifact("payloads", chunk_id)
            target = self.paths.artifact("payloads", chunk_id)
            _write_payloads(target, pending.payloads)
            payload_artifact = _artifact(target, rel)
            # The writer owns chunk naming, so it — not the producer — fills in
            # the file each reference points at. A producer cannot know the
            # chunk id, and a guessed path would be a broken reference.
            for row in pending.packets:
                ref = row.get("payload_ref")
                if isinstance(ref, dict):
                    ref["file"] = rel
        self._fire(fault, "payloads_written")

        packets_rel = self.paths.relative_artifact("packets", chunk_id)
        packets_path = self.paths.artifact("packets", chunk_id)
        _write_arrow(packets_path, PACKETS_SCHEMA, pending.packets)
        self._fire(fault, "packets_written")

        obs_rel = self.paths.relative_artifact("observations", chunk_id)
        obs_path = self.paths.artifact("observations", chunk_id)
        _write_arrow(obs_path, OBSERVATIONS_SCHEMA, pending.observations)
        self._fire(fault, "observations_written")

        samples_rel = self.paths.relative_artifact("samples", chunk_id)
        samples_path = self.paths.artifact("samples", chunk_id)
        _write_arrow(
            samples_path,
            samples_schema(self.descriptor.layout, self.descriptor.n_channels),
            pending.samples,
        )
        self._fire(fault, "samples_written")

        seqs = [int(row["packet_seq"]) for row in pending.packets]
        record = ChunkCommit(
            chunk_id=chunk_id,
            prev_record_sha256=self._prev_hash,
            payloads=payload_artifact,
            packets=_artifact(packets_path, packets_rel),
            observations=_artifact(obs_path, obs_rel),
            samples=_artifact(samples_path, samples_rel),
            first_packet_seq=min(seqs),
            last_packet_seq=max(seqs),
            descriptor_sha256=self.descriptor_sha256,
        )
        body = record.model_dump(mode="json", exclude_none=True, exclude={"record_sha256"})
        digest = canonical_json.record_hash(body)
        sealed = dict(body)
        sealed[canonical_json.RECORD_HASH_KEY] = digest

        # Sidecar first, then the authoritative index. A crash between the two
        # leaves an orphan sidecar, which recovery reports and never adopts:
        # a sidecar alone does not commit a chunk.
        atomic_write(self.paths.sidecar(chunk_id), canonical_json.canonicalize(sealed))
        self._fire(fault, "sidecar_written")

        append_line(self.paths.chunks_index, canonical_json.canonicalize(sealed) + b"\n")
        self._fire(fault, "index_appended")

        self._prev_hash = digest
        self._next_chunk_id += 1
        committed = record.model_copy(update={"record_sha256": digest})
        self._committed.append(committed)
        return committed

    @staticmethod
    def _fire(fault: FaultHook | None, stage: str) -> None:
        if fault is not None:
            fault(stage)
