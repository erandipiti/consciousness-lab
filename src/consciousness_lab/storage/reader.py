"""Package reading and replay (spec §16; D26).

Replay lives at the storage/read boundary, deliberately not behind a fake device
adapter: impersonating hardware is what makes a replay indistinguishable from a
recording downstream, and downstream is where the confusion would be permanent.

The reader reproduces ordering, nulls and provenance verbatim. It never
synthesizes a missing raw timing value, never renumbers ``packet_seq``, and
never presents a reconstructed time as raw.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from consciousness_lab.session.finalizer import read_manifest
from consciousness_lab.session.model import (
    Allocation,
    ChunkCommit,
    EventRecord,
    Manifest,
    Run,
    StreamDescriptor,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.paths import PackagePaths
from consciousness_lab.storage.payload import PayloadRef, read_at
from consciousness_lab.storage.verifier import read_chunk_index


class UnsupportedSchemaVersionError(RuntimeError):
    """A package declares a major schema version this reader does not implement."""


@dataclass(frozen=True)
class StreamReader:
    """Read one raw stream's committed chunks, in commit order."""

    paths: PackagePaths
    stream_id: str
    descriptor: StreamDescriptor
    commits: tuple[ChunkCommit, ...]

    def _table(self, relative: str) -> pa.Table:
        target = self.paths.stream(self.stream_id).root / relative
        with target.open("rb") as handle:
            return pa.ipc.open_stream(handle).read_all()

    def packets(self) -> Iterator[dict[str, Any]]:
        for commit in self.commits:
            yield from self._table(commit.packets.path).to_pylist()

    def samples(self) -> Iterator[dict[str, Any]]:
        for commit in self.commits:
            yield from self._table(commit.samples.path).to_pylist()

    def observations(self) -> Iterator[dict[str, Any]]:
        for commit in self.commits:
            yield from self._table(commit.observations.path).to_pylist()

    def payloads(self) -> Iterator[tuple[int, bytes]]:
        """Resolve each packet's transport bytes through its own reference.

        Yields nothing when the stream's capture level is not
        ``transport_payload``: there are no bytes, and none are invented.
        """
        if not self.descriptor.expects_payload_artifact:
            return
        for commit in self.commits:
            if commit.payloads is None:
                continue
            blob = (self.paths.stream(self.stream_id).root / commit.payloads.path).read_bytes()
            for row in self._table(commit.packets.path).to_pylist():
                ref = row["payload_ref"]
                yield read_at(blob, PayloadRef(ref["file"], int(ref["offset"]), int(ref["length"])))


@dataclass(frozen=True)
class PackageReader:
    """A loaded package. Construct with :func:`open_package`."""

    paths: PackagePaths
    allocation: Allocation
    manifest: Manifest | None
    run: Run | None
    streams: dict[str, StreamReader]

    def events(self) -> list[EventRecord]:
        if not self.paths.events.exists():
            return []
        records: list[EventRecord] = []
        for line in self.paths.events.read_bytes().split(b"\n"):
            if not line.strip():
                continue
            records.append(EventRecord.model_validate(canonical_json.loads(line)))
        return records

    def stream(self, stream_id: str) -> StreamReader:
        return self.streams[stream_id]


def open_package(paths: PackagePaths, *, supported_major: int = 1) -> PackageReader:
    """Load a package, refusing an unsupported major schema version.

    Minor-version tolerance is deliberate: an unknown *optional* field in a
    v1.x package is ignored, while an unknown major fails closed. Nothing is
    migrated and nothing is mutated (spec §17).
    """
    allocation = Allocation.model_validate(canonical_json.loads(paths.allocation.read_bytes()))
    major = int(allocation.schema_version.split(".")[0])
    if major != supported_major:
        raise UnsupportedSchemaVersionError(
            f"package declares schema major {major}; this reader implements "
            f"{supported_major} and will not guess"
        )

    run: Run | None = None
    if paths.run.is_file():
        run = Run.model_validate(canonical_json.loads(paths.run.read_bytes()))

    streams: dict[str, StreamReader] = {}
    if paths.raw.is_dir():
        for stream_dir in sorted(p for p in paths.raw.iterdir() if p.is_dir()):
            stream_id = stream_dir.name
            descriptor = StreamDescriptor.model_validate(
                canonical_json.loads(paths.stream(stream_id).descriptor.read_bytes())
            )
            commits, error = read_chunk_index(paths, stream_id)
            if error is not None:
                raise RuntimeError(f"stream {stream_id}: {error}")
            streams[stream_id] = StreamReader(
                paths=paths,
                stream_id=stream_id,
                descriptor=descriptor,
                commits=tuple(commits),
            )

    return PackageReader(
        paths=paths,
        allocation=allocation,
        manifest=read_manifest(paths.manifest),
        run=run,
        streams=streams,
    )
