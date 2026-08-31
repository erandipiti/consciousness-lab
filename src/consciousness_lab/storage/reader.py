"""Package reading and replay (v2 §15; D26, D27).

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

from consciousness_lab.session.model import (
    SCHEMA_MAJOR,
    Allocation,
    ChunkCommit,
    EventRecord,
    Manifest,
    Run,
    StreamDescriptor,
    load_on_disk,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.package_layout import read_manifest
from consciousness_lab.storage.paths import PackagePaths
from consciousness_lab.storage.payload import PayloadFramingError, payloads_by_packet
from consciousness_lab.storage.verifier import read_chunk_index


class UnsupportedSchemaVersionError(RuntimeError):
    """A package declares a major schema version this reader does not implement.

    A v2 reader **fails closed** on a v1 package rather than guessing (D27).
    There is no in-place migration and no acquisition package is ever mutated.
    If a v1 development fixture ever needs inspecting, that is a separate,
    explicitly versioned legacy reader; it must not constrain v2.
    """


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
            yield from self._table(commit.artifact_path("packets")).to_pylist()

    def samples(self) -> Iterator[dict[str, Any]]:
        for commit in self.commits:
            yield from self._table(commit.artifact_path("samples")).to_pylist()

    def observations(self) -> Iterator[dict[str, Any]]:
        for commit in self.commits:
            yield from self._table(commit.artifact_path("observations")).to_pylist()

    def payloads(self) -> Iterator[tuple[int, bytes]]:
        """Yield each packet's transport bytes, in packet order.

        v2 has no ``payload_ref``: the packet -> bytes index is produced by
        walking the chunk's payload file, keyed by the ``packet_seq`` each frame
        carries. Yields nothing when the capture level is not
        ``transport_payload``: there are no bytes, and none are invented.
        """
        if not self.descriptor.expects_payload_artifact:
            return
        root = self.paths.stream(self.stream_id).root
        for commit in self.commits:
            if not commit.has_payload_artifact:
                continue
            blob = (root / commit.artifact_path("payloads")).read_bytes()
            mapping, error = payloads_by_packet(blob)
            if error is not None:
                raise PayloadFramingError(f"chunk {commit.chunk_id}: {error}")
            for row in self._table(commit.artifact_path("packets")).to_pylist():
                seq = int(row["packet_seq"])
                if seq not in mapping:
                    raise PayloadFramingError(
                        f"chunk {commit.chunk_id}: packet {seq} has no payload frame"
                    )
                yield seq, mapping[seq]


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
            records.append(load_on_disk(EventRecord, canonical_json.loads(line)))
        return records

    def stream(self, stream_id: str) -> StreamReader:
        return self.streams[stream_id]


class UnverifiedPackageError(RuntimeError):
    """A package failed verification and was not opened for reading."""

    def __init__(self, session_id: str, findings: list[str]) -> None:
        super().__init__(f"{session_id} failed verification: {', '.join(findings)}")
        self.findings = findings


def open_package(
    paths: PackagePaths, *, supported_major: int = SCHEMA_MAJOR, verify: bool = True
) -> PackageReader:
    """Load a package, refusing an unsupported major schema version.

    ``verify`` defaults to True and checks integrity before any raw data is
    handed out, so a caller cannot consume corrupted bytes by forgetting to ask.
    It is only turned off deliberately — by recovery and inspection tooling,
    whose whole job is to look at packages that do not verify.

    Minor-version tolerance is deliberate: an unknown *optional* field in a
    v2.x package is ignored, while an unknown major fails closed. Nothing is
    migrated and nothing is mutated (D27).
    """
    allocation = load_on_disk(Allocation, canonical_json.loads(paths.allocation.read_bytes()))
    major = int(allocation.schema_version.split(".")[0])
    if major != supported_major:
        raise UnsupportedSchemaVersionError(
            f"package declares schema major {major}; this reader implements "
            f"{supported_major} and will not guess"
        )

    run: Run | None = None
    if paths.run.is_file():
        run = load_on_disk(Run, canonical_json.loads(paths.run.read_bytes()))

    streams: dict[str, StreamReader] = {}
    if paths.raw.is_dir():
        for stream_dir in sorted(p for p in paths.raw.iterdir() if p.is_dir()):
            stream_id = stream_dir.name
            descriptor = load_on_disk(
                StreamDescriptor,
                canonical_json.loads(paths.stream(stream_id).descriptor.read_bytes()),
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

    if verify:
        from consciousness_lab.storage.verifier import Finding, verify_package

        result = verify_package(paths)
        # Integrity only: a correctly sealed ABORTED package is perfectly
        # readable, so the outcome conditions (4 and 8) are not gates here.
        #
        # Condition 5 is split deliberately. Its STRUCTURAL half gates reading:
        # an undeclared stream directory, or a stream with no valid closure
        # record, means the reader cannot say what it is handing back. Its
        # OUTCOME half does not: a required stream that closed DISCONNECTED is
        # exactly what a legitimately aborted package looks like, and refusing
        # to read it would make the failure case the unreadable one.
        structural = {
            Finding.STREAM_NOT_DECLARED,
            Finding.MISSING_STREAM_CLOSE,
            Finding.INVALID_STREAM_CLOSE,
            Finding.UNREADABLE_RUN,
        }
        integrity = [n for n in (1, 2, 3, 6, 7) if not result.conditions.get(n, False)]
        if result.findings() & structural:
            integrity.append(5)
        if integrity:
            raise UnverifiedPackageError(
                paths.root.name,
                sorted({issue.finding.value for issue in result.issues}),
            )

    return PackageReader(
        paths=paths,
        allocation=allocation,
        manifest=read_manifest(paths.manifest),
        run=run,
        streams=streams,
    )
