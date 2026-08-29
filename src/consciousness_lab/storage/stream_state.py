"""Physical stream state — the single source of truth about what is on disk.

CL-002B-R1 exists because two valid-looking views of a stream drifted apart:
the manifest summary was built from the writer's in-memory state, while
verification iterated the directories that actually existed. Neither view
proved anything about the other, so a manifest could describe a stream whose
raw directory had been deleted and the package still satisfied all eight
completion conditions.

The fix is not a third view. It is this module: **both** the finalizer preflight
and the package verifier derive chunk count, chain head, packet range and
descriptor hash from here, so there is exactly one definition of each.
"""

from dataclasses import dataclass
from pathlib import Path

from consciousness_lab.session.model import ChunkCommit, StreamDescriptor, load_on_disk
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import sha256_bytes
from consciousness_lab.storage.paths import PackagePaths

#: Structural files every raw stream directory must contain. ``chunks.jsonl``
#: is created empty at stream open, so a zero-chunk stream is structurally
#: identical to any other and "index absent" never has to be interpreted.
REQUIRED_STREAM_FILES = ("descriptor.json", "chunks.jsonl")


@dataclass(frozen=True)
class PhysicalStreamState:
    """What a raw stream directory actually contains, read from disk."""

    stream_id: str
    directory_exists: bool
    descriptor_present: bool
    chunks_index_present: bool
    descriptor: StreamDescriptor | None
    descriptor_sha256: str | None
    descriptor_error: str | None
    commits: tuple[ChunkCommit, ...]
    chain_error: str | None

    @property
    def chunk_count(self) -> int:
        return len(self.commits)

    @property
    def chain_head_sha256(self) -> str | None:
        """Final record hash, or ``None`` for a stream with no committed chunks."""
        return self.commits[-1].record_sha256 if self.commits else None

    @property
    def first_packet_seq(self) -> int | None:
        return self.commits[0].first_packet_seq if self.commits else None

    @property
    def last_packet_seq(self) -> int | None:
        return self.commits[-1].last_packet_seq if self.commits else None

    @property
    def structurally_complete(self) -> bool:
        """True when the directory holds its required files and a valid chain."""
        return (
            self.directory_exists
            and self.descriptor_present
            and self.chunks_index_present
            and self.descriptor is not None
            and self.chain_error is None
        )


def list_physical_streams(paths: PackagePaths) -> list[str]:
    """Every raw stream directory present on disk, sorted."""
    if not paths.raw.is_dir():
        return []
    return sorted(p.name for p in paths.raw.iterdir() if p.is_dir() and not p.is_symlink())


def read_chunk_chain(index: Path) -> tuple[tuple[ChunkCommit, ...], str | None]:
    """Parse and hash-chain-verify a stream's ``chunks.jsonl``.

    A chunk is real if and only if its record appears here, so this is the only
    place that decides what "committed" means.
    """
    if not index.exists():
        return (), "chunks.jsonl is absent"
    commits: list[ChunkCommit] = []
    prev = canonical_json.ZERO_HASH
    for number, line in enumerate(index.read_bytes().split(b"\n")):
        if not line.strip():
            continue
        try:
            obj = canonical_json.loads(line)
        except (canonical_json.CanonicalizationError, ValueError):
            return tuple(commits), f"line {number} is not parseable"
        if not isinstance(obj, dict) or not canonical_json.verify_record(obj):
            return tuple(commits), f"line {number} record_sha256 does not verify"
        try:
            commit = load_on_disk(ChunkCommit, obj)
        except ValueError as exc:
            return tuple(commits), f"line {number} is malformed ({exc})"
        if commit.prev_record_sha256 != prev:
            return tuple(commits), f"line {number} breaks the hash chain"
        prev = str(commit.record_sha256)
        commits.append(commit)
    return tuple(commits), None


def read_physical_stream(paths: PackagePaths, stream_id: str) -> PhysicalStreamState:
    """Read one raw stream directory. Never raises on malformed content."""
    stream_paths = paths.stream(stream_id)
    directory_exists = stream_paths.root.is_dir()

    descriptor: StreamDescriptor | None = None
    descriptor_sha256: str | None = None
    descriptor_error: str | None = None
    descriptor_present = stream_paths.descriptor.is_file()
    if descriptor_present:
        try:
            raw = stream_paths.descriptor.read_bytes()
            # Hash the STORED BYTES. Reserializing a parsed model and hashing
            # that would compare our reconstruction, not what is on disk.
            descriptor_sha256 = sha256_bytes(raw)
            descriptor = load_on_disk(StreamDescriptor, canonical_json.loads(raw))
        except (OSError, canonical_json.CanonicalizationError, ValueError) as exc:
            descriptor_error = str(exc)

    commits: tuple[ChunkCommit, ...] = ()
    chain_error: str | None = None
    chunks_index_present = stream_paths.chunks_index.is_file()
    if directory_exists:
        commits, chain_error = read_chunk_chain(stream_paths.chunks_index)

    return PhysicalStreamState(
        stream_id=stream_id,
        directory_exists=directory_exists,
        descriptor_present=descriptor_present,
        chunks_index_present=chunks_index_present,
        descriptor=descriptor,
        descriptor_sha256=descriptor_sha256,
        descriptor_error=descriptor_error,
        commits=commits,
        chain_error=chain_error,
    )


def read_all_physical_streams(paths: PackagePaths) -> dict[str, PhysicalStreamState]:
    """Physical state for every raw stream directory present."""
    return {
        stream_id: read_physical_stream(paths, stream_id)
        for stream_id in list_physical_streams(paths)
    }
