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

import itertools
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa

from consciousness_lab.session.model import ChunkCommit, StreamDescriptor, load_on_disk
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import sha256_bytes
from consciousness_lab.storage.paths import PackagePaths

#: Structural files every raw stream directory must contain. ``chunks.jsonl``
#: is created empty at stream open, so a zero-chunk stream is structurally
#: identical to any other and "index absent" never has to be interpreted.
REQUIRED_STREAM_FILES = ("descriptor.json", "chunks.jsonl")


@dataclass(frozen=True)
class ChunkRecordOnDisk:
    """One chunk commit record, kept in BOTH representations.

    Parsed-model equality is not record identity. Two different on-disk JSON
    documents can normalize to the same ``ChunkCommit``: an unknown field that
    minor-version tolerance ignores (spec §17), or an explicitly-null key where
    the contract requires the key omitted (§12.2). Model equality therefore
    proves the known fields overlap; it does not prove the two files hold the
    same record.

    So the canonical bytes of the COMPLETE parsed document are retained
    alongside the model, and identity comparisons use those. They are
    canonicalized from the parsed JSON rather than from ``model_dump()``, which
    would discard exactly the fields the model ignored.
    """

    model: ChunkCommit
    canonical_bytes: bytes
    record_sha256: str
    #: Whether the ``payloads`` KEY is present in the document, regardless of
    #: its value. §12.2 requires the key omitted entirely at non-payload capture
    #: levels — "never written as a null" — and two records that both carry an
    #: explicit null agree with each other while both violating the contract.
    #: Pairwise equality cannot see that; key presence can (matrix row R12).
    payloads_key_present: bool

    @property
    def chunk_id(self) -> int:
        return self.model.chunk_id

    def same_record_as(self, other: "ChunkRecordOnDisk") -> bool:
        """True only when both files hold the identical logical record."""
        return self.canonical_bytes == other.canonical_bytes


@dataclass(frozen=True)
class PhysicalChunk:
    """A committed chunk reconciled against its physical artifacts."""

    record: ChunkRecordOnDisk
    #: First/last ``packet_seq`` actually present in ``packets/NNNNNN.arrow``.
    #: ``None`` when the artifact is missing or unreadable — never guessed.
    physical_first_packet_seq: int | None
    physical_last_packet_seq: int | None
    packet_rows: int
    packet_error: str | None
    #: ``packet_seq -> n_samples`` from the physical packets rows, for the
    #: foreign-key checks in matrix rows R20-R23.
    packet_sample_counts: dict[int, int]
    #: Structural foreign-key violations found between samples/observations and
    #: the packets of the same chunk.
    reference_errors: tuple[str, ...]

    @property
    def chunk_id(self) -> int:
        return self.record.chunk_id


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
    #: Committed chunks, each reconciled against its physical packets artifact.
    chunks: tuple[PhysicalChunk, ...]
    chain_error: str | None
    #: Per-chunk ``NNNNNN.commit.json`` sidecars, keyed by chunk id. A sidecar
    #: is a convenience copy for recovery tooling; ``chunks.jsonl`` remains the
    #: authoritative commit log, so a sidecar must never contradict it.
    sidecars: dict[int, ChunkRecordOnDisk]
    sidecar_errors: tuple[str, ...]
    #: Chain-level ordering violations (matrix rows R24, R25). Per-chunk
    #: validation does not imply chain validation: every chunk of a reversed
    #: chain still matches its own artifact.
    order_errors: tuple[str, ...]

    @property
    def commits(self) -> tuple[ChunkCommit, ...]:
        """The parsed commit models, in chain order."""
        return tuple(chunk.record.model for chunk in self.chunks)

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def chain_head_sha256(self) -> str | None:
        """Final record hash, or ``None`` for a stream with no committed chunks."""
        return self.chunks[-1].record.record_sha256 if self.chunks else None

    @property
    def first_packet_seq(self) -> int | None:
        """First packet sequence, derived from the PHYSICAL packet rows.

        Not from the chunk summaries. A summary checked against another summary
        proves only that two claims agree, never that either is true.
        """
        return self.chunks[0].physical_first_packet_seq if self.chunks else None

    @property
    def last_packet_seq(self) -> int | None:
        return self.chunks[-1].physical_last_packet_seq if self.chunks else None

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


def parse_chunk_record(raw: bytes) -> ChunkRecordOnDisk | str:
    """Parse one chunk record, keeping its canonical bytes. Returns an error string on failure.

    The canonical bytes come from the FULL parsed document, so any field present
    on disk participates in identity — including one the model ignores.
    """
    try:
        obj = canonical_json.loads(raw)
    except (canonical_json.CanonicalizationError, ValueError) as exc:
        return f"not parseable ({exc})"
    if not isinstance(obj, dict):
        return "record is not a JSON object"
    if not canonical_json.verify_record(obj):
        return "record_sha256 does not verify"
    try:
        model = load_on_disk(ChunkCommit, obj)
    except ValueError as exc:
        return f"malformed ({exc})"
    return ChunkRecordOnDisk(
        model=model,
        canonical_bytes=canonical_json.canonicalize(obj),
        record_sha256=str(obj[canonical_json.RECORD_HASH_KEY]),
        payloads_key_present="payloads" in obj,
    )


def read_chunk_chain(index: Path) -> tuple[tuple[ChunkRecordOnDisk, ...], str | None]:
    """Parse and hash-chain-verify a stream's ``chunks.jsonl``.

    A chunk is real if and only if its record appears here, so this is the only
    place that decides what "committed" means.
    """
    if not index.exists():
        return (), "chunks.jsonl is absent"
    records: list[ChunkRecordOnDisk] = []
    prev = canonical_json.ZERO_HASH
    for number, line in enumerate(index.read_bytes().split(b"\n")):
        if not line.strip():
            continue
        parsed = parse_chunk_record(line)
        if isinstance(parsed, str):
            return tuple(records), f"line {number} {parsed}"
        if parsed.model.prev_record_sha256 != prev:
            return tuple(records), f"line {number} breaks the hash chain"
        prev = parsed.record_sha256
        records.append(parsed)
    return tuple(records), None


SIDECAR_SUFFIX = ".commit.json"


def read_sidecars(stream_root: Path) -> tuple[dict[int, ChunkRecordOnDisk], tuple[str, ...]]:
    """Read every ``NNNNNN.commit.json`` sidecar, keeping its canonical bytes."""
    sidecars: dict[int, ChunkRecordOnDisk] = {}
    errors: list[str] = []
    if not stream_root.is_dir():
        return sidecars, ()
    for path in sorted(stream_root.glob(f"*{SIDECAR_SUFFIX}")):
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError as exc:
            errors.append(f"{path.name}: unreadable ({exc})")
            continue
        parsed = parse_chunk_record(raw)
        if isinstance(parsed, str):
            errors.append(f"{path.name}: {parsed}")
            continue
        expected_name = f"{parsed.chunk_id:06d}{SIDECAR_SUFFIX}"
        if path.name != expected_name:
            errors.append(
                f"{path.name}: declares chunk {parsed.chunk_id}, expected {expected_name}"
            )
            continue
        sidecars[parsed.chunk_id] = parsed
    return sidecars, tuple(errors)


def _read_column(path: Path, column: str) -> tuple[list[object] | None, str | None]:
    """Read one column from an Arrow IPC stream file, reporting rather than raising."""
    try:
        with path.open("rb") as handle:
            table = pa.ipc.open_stream(handle).read_all()
        return list(table.column(column).to_pylist()), None
    except (pa.ArrowException, OSError, KeyError, TypeError, ValueError) as exc:
        return None, f"{path.name} unreadable ({exc})"


def _read_packet_facts(path: Path) -> tuple[dict[int, int] | None, str | None]:
    """Return ``packet_seq -> n_samples`` from a physical packets artifact."""
    try:
        with path.open("rb") as handle:
            table = pa.ipc.open_stream(handle).read_all()
        rows = table.select(["packet_seq", "n_samples"]).to_pylist()
    except (pa.ArrowException, OSError, KeyError, TypeError, ValueError) as exc:
        return None, f"packets artifact unreadable ({exc})"
    return {int(r["packet_seq"]): int(r["n_samples"]) for r in rows}, None


def _check_references(
    stream_root: Path, record: ChunkRecordOnDisk, packet_counts: dict[int, int]
) -> tuple[str, ...]:
    """Structural foreign keys from samples/observations to this chunk's packets.

    Matrix rows R20-R23. These follow from §9.1's own statements — that
    ``(packet_seq, sample_index_in_packet)`` is the sample primary key and that
    ``n_samples`` is "samples carried in this packet" — and are structural, not
    scientific: no minimum count is implied or enforced.
    """
    errors: list[str] = []
    commit = record.model
    for label, artifact, index_required in (
        ("samples", commit.samples, True),
        ("observations", commit.observations, False),
    ):
        target = stream_root / artifact.path
        if not target.is_file():
            continue
        try:
            with target.open("rb") as handle:
                table = pa.ipc.open_stream(handle).read_all()
            rows = table.select(["packet_seq", "sample_index_in_packet"]).to_pylist()
        except (pa.ArrowException, OSError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"{label} artifact unreadable ({exc})")
            continue
        for position, row in enumerate(rows):
            seq = int(row["packet_seq"])
            if seq not in packet_counts:
                errors.append(
                    f"{label} row {position} references packet_seq {seq}, "
                    "absent from this chunk's packets"
                )
                break
            index = row["sample_index_in_packet"]
            if index is None:
                if index_required:
                    errors.append(f"{label} row {position} has a null sample index")
                    break
                continue
            if not 0 <= int(index) < packet_counts[seq]:
                errors.append(
                    f"{label} row {position} sample index {index} is outside "
                    f"n_samples={packet_counts[seq]} for packet {seq}"
                )
                break
    return tuple(errors)


def read_packet_range(path: Path) -> tuple[int | None, int | None, int, str | None]:
    """Read the actual ``packet_seq`` values from a committed packets artifact.

    Returns ``(first, last, row_count, error)``. This is the PHYSICAL authority
    for a chunk's packet range: a commit record only *claims* a range, and a
    claim checked against another claim proves nothing about the data.

    ``packet_seq`` is "strictly increasing at arrival" (spec §9.1), so that
    property is enforced — it is what makes "first" and "last" unambiguous.
    Consecutiveness is deliberately NOT required: a gap is a device fact for a
    later ticket, not a structural violation, and inventing that rule here
    would be a packet-loss criterion this ticket must not introduce.
    """
    try:
        with path.open("rb") as handle:
            table = pa.ipc.open_stream(handle).read_all()
        sequences = [int(value) for value in table.column("packet_seq").to_pylist()]
    except (pa.ArrowException, OSError, KeyError, TypeError, ValueError) as exc:
        return None, None, 0, f"packets artifact unreadable ({exc})"
    if not sequences:
        # A committed chunk always carries packets: the writer refuses to commit
        # an empty one, and ChunkCommit requires non-null first/last. An empty
        # artifact under a commit record is therefore a contradiction, reported
        # rather than resolved.
        return None, None, 0, "packets artifact holds no rows"
    for earlier, later in itertools.pairwise(sequences):
        if later <= earlier:
            return (
                None,
                None,
                len(sequences),
                f"packet_seq is not strictly increasing ({earlier} then {later})",
            )
    return sequences[0], sequences[-1], len(sequences), None


def _reconcile_chunk(stream_root: Path, record: ChunkRecordOnDisk) -> PhysicalChunk:
    """Reconcile a commit record against every physical artifact it names."""
    packets_path = stream_root / record.model.packets.path
    if not packets_path.is_file():
        return PhysicalChunk(record, None, None, 0, "packets artifact is absent", {}, ())
    first, last, rows, error = read_packet_range(packets_path)
    counts, counts_error = _read_packet_facts(packets_path)
    if counts is None:
        return PhysicalChunk(record, first, last, rows, error or counts_error, {}, ())
    references = _check_references(stream_root, record, counts)
    return PhysicalChunk(record, first, last, rows, error, counts, references)


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

    records: tuple[ChunkRecordOnDisk, ...] = ()
    chain_error: str | None = None
    chunks_index_present = stream_paths.chunks_index.is_file()
    if directory_exists:
        records, chain_error = read_chunk_chain(stream_paths.chunks_index)
    # Each committed chunk is reconciled against its physical packets artifact
    # here, once, so neither the verifier nor the finalizer re-derives it.
    chunks = tuple(_reconcile_chunk(stream_paths.root, record) for record in records)

    sidecars, sidecar_errors = read_sidecars(stream_paths.root)

    return PhysicalStreamState(
        stream_id=stream_id,
        directory_exists=directory_exists,
        descriptor_present=descriptor_present,
        chunks_index_present=chunks_index_present,
        descriptor=descriptor,
        descriptor_sha256=descriptor_sha256,
        descriptor_error=descriptor_error,
        chunks=chunks,
        chain_error=chain_error,
        order_errors=_check_chain_order(chunks),
        sidecars=sidecars,
        sidecar_errors=sidecar_errors,
    )


def _check_chain_order(chunks: tuple[PhysicalChunk, ...]) -> tuple[str, ...]:
    """Chain-level ordering across the whole authoritative sequence.

    ``chunk_id`` strictly increases because the index is append-only and chunks
    are sealed in order, and ``packet_seq`` is "strictly increasing at arrival"
    (§9.1) for the whole stream, so chunk N+1 must begin after chunk N ends.

    Contiguity is deliberately NOT required for either: a gap in ``packet_seq``
    is a device fact for a later ticket, and requiring consecutiveness here
    would be a packet-loss criterion.
    """
    errors: list[str] = []
    for previous, current in itertools.pairwise(chunks):
        if current.chunk_id <= previous.chunk_id:
            errors.append(
                f"chunk_id does not increase along the chain "
                f"({previous.chunk_id} then {current.chunk_id})"
            )
        previous_last = previous.physical_last_packet_seq
        current_first = current.physical_first_packet_seq
        if previous_last is None or current_first is None:
            continue
        if current_first <= previous_last:
            errors.append(
                f"chunk {current.chunk_id} begins at packet {current_first}, "
                f"which does not follow chunk {previous.chunk_id} ending at {previous_last}"
            )
    return tuple(errors)


def read_all_physical_streams(paths: PackagePaths) -> dict[str, PhysicalStreamState]:
    """Physical state for every raw stream directory present."""
    return {
        stream_id: read_physical_stream(paths, stream_id)
        for stream_id in list_physical_streams(paths)
    }
