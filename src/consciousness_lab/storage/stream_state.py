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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa

from consciousness_lab.session.model import (
    ChunkCommit,
    SampleLayout,
    StreamDescriptor,
    load_on_disk,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage import payload as payload_mod
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


def _check_artifact_leaves(stream_root: Path, record: ChunkRecordOnDisk) -> tuple[str, ...]:
    """Every artifact reference has three leaves: path, sha256 and bytes.

    A matching SHA does not validate the record — ``bytes`` is an independently
    falsifiable leaf, and the authority for it is the physical file's size, not
    the number the record happens to carry.
    """
    errors: list[str] = []
    commit = record.model
    artifacts = [
        ("packets", commit.packets),
        ("observations", commit.observations),
        ("samples", commit.samples),
    ]
    if commit.payloads is not None:
        artifacts.append(("payloads", commit.payloads))
    for label, artifact in artifacts:
        target = stream_root / artifact.path
        if not target.is_file():
            continue  # reported separately as a missing artifact
        actual = target.stat().st_size
        if artifact.bytes != actual:
            errors.append(f"{label} artifact declares {artifact.bytes} bytes, file holds {actual}")
    return tuple(errors)


def _check_payload_frames(
    stream_root: Path, record: ChunkRecordOnDisk, packets: list[dict[str, Any]]
) -> tuple[str, ...]:
    """Reconcile the byte-framed payload log against the packet rows.

    A valid CRC proves a frame is internally intact. It does NOT prove the frame
    belongs to the packet that references it — the frame carries its own
    ``packet_seq``, and that leaf must agree. §9.1 defines the log as records
    "each tagged with its ``packet_seq``", one per received packet, so the
    relation is a bijection.
    """
    commit = record.model
    if commit.payloads is None:
        return ()
    blob_path = stream_root / commit.payloads.path
    if not blob_path.is_file():
        return ()
    frames, walk_error = payload_mod.iter_frames(blob_path.read_bytes())
    errors: list[str] = []
    if walk_error is not None:
        errors.append(f"payload log: {walk_error}")
    by_offset = {frame.offset: frame for frame in frames}

    referenced: list[int] = []
    for row in packets:
        ref = row.get("payload_ref")
        seq = int(row["packet_seq"])
        if not isinstance(ref, dict):
            continue
        offset = int(ref["offset"])
        frame = by_offset.get(offset)
        if frame is None:
            errors.append(f"packet {seq}: payload_ref offset {offset} is not a frame boundary")
            continue
        referenced.append(offset)
        if frame.packet_seq != seq:
            errors.append(
                f"packet {seq}: payload frame at offset {offset} is tagged "
                f"packet_seq {frame.packet_seq}"
            )
        if frame.payload_len != int(ref["length"]):
            errors.append(
                f"packet {seq}: payload_ref length {ref['length']} disagrees with "
                f"framed length {frame.payload_len}"
            )
        if ref["file"] != commit.payloads.path:
            errors.append(
                f"packet {seq}: payload_ref.file {ref['file']!r} is not {commit.payloads.path!r}"
            )

    # One frame per received packet (§9.1): no frame unreferenced, none shared.
    if len(referenced) != len(set(referenced)):
        errors.append("two packet rows reference the same payload frame")
    unreferenced = sorted(set(by_offset) - set(referenced))
    if unreferenced:
        errors.append(f"payload log holds {len(unreferenced)} frame(s) no packet references")
    return tuple(errors)


def _check_references(
    stream_root: Path,
    record: ChunkRecordOnDisk,
    packet_counts: dict[int, int],
    descriptor: StreamDescriptor | None,
) -> tuple[str, ...]:
    """Structural relations from samples/observations to this chunk's packets.

    Matrix rows R20-R23 and the dense sample-key identity rule. These follow
    from §9.1's own statements - that ``(packet_seq, sample_index_in_packet)``
    is the sample primary key and that ``n_samples`` is "samples carried in this
    packet" - and are structural, not scientific. No minimum count is implied.
    """
    errors: list[str] = []
    commit = record.model

    samples_path = stream_root / commit.samples.path
    layout = descriptor.layout if descriptor is not None else None
    if samples_path.is_file():
        rows, error = _read_rows(samples_path, ["packet_seq", "sample_index_in_packet"])
        if error is not None:
            errors.append(f"samples artifact unreadable ({error})")
        elif layout is SampleLayout.DENSE_FIXED_LIST:
            errors.extend(_check_dense_sample_keys(rows, packet_counts))
        else:
            # sparse_long: cardinality is NOT enforced. See CHUNK_EQUIVALENCE.md
            # - the spec names one sample primary key while the sparse layout
            # adds channel_id, so its identity contract is unresolved and must
            # not be invented here. Only the structural references are checked.
            errors.extend(_check_row_references(rows, packet_counts, "samples", strict=False))

    observations_path = stream_root / commit.observations.path
    if observations_path.is_file():
        rows, error = _read_rows(observations_path, ["packet_seq", "sample_index_in_packet"])
        if error is not None:
            errors.append(f"observations artifact unreadable ({error})")
        else:
            # Observations are not a complete set by contract, so no cardinality
            # rule and no uniqueness rule is invented for them.
            errors.extend(_check_row_references(rows, packet_counts, "observations", strict=False))
    return tuple(errors)


def _read_rows(path: Path, columns: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    try:
        with path.open("rb") as handle:
            table = pa.ipc.open_stream(handle).read_all()
        return list(table.select(columns).to_pylist()), None
    except (pa.ArrowException, OSError, KeyError, TypeError, ValueError) as exc:
        return [], str(exc)


def _check_row_references(
    rows: list[dict[str, Any]],
    packet_counts: dict[int, int],
    label: str,
    *,
    strict: bool,
) -> list[str]:
    """Foreign key to a packet in the same chunk, and index within n_samples."""
    errors: list[str] = []
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
            if strict:
                errors.append(f"{label} row {position} has a null sample index")
                break
            continue
        if not 0 <= int(index) < packet_counts[seq]:
            errors.append(
                f"{label} row {position} sample index {index} is outside "
                f"n_samples={packet_counts[seq]} for packet {seq}"
            )
            break
    return errors


def _check_dense_sample_keys(
    rows: list[dict[str, Any]], packet_counts: dict[int, int]
) -> list[str]:
    """Exact key-set identity for a dense stream.

    For a packet declaring ``n_samples = N`` the sample keys must be exactly
    ``(packet_seq, 0) .. (packet_seq, N-1)``, each once. A multiset comparison
    is used deliberately: comparing sets alone would let a duplicated key hide a
    missing one, which is exactly how a deleted sample survived before.

    ``n_samples = 0`` expects an empty key set. This is storage integrity, not a
    minimum-sample threshold.
    """
    expected: Counter[tuple[int, int]] = Counter(
        (seq, index) for seq, count in packet_counts.items() for index in range(count)
    )
    actual: Counter[tuple[int, int]] = Counter()
    for row in rows:
        index = row["sample_index_in_packet"]
        if index is None:
            return ["samples row has a null sample index in a dense stream"]
        actual[(int(row["packet_seq"]), int(index))] += 1

    if actual == expected:
        return []

    errors: list[str] = []
    if sum(actual.values()) != sum(expected.values()):
        errors.append(
            f"samples hold {sum(actual.values())} rows, packets declare {sum(expected.values())}"
        )
    duplicates = sorted(key for key, count in actual.items() if count > 1)
    if duplicates:
        errors.append(f"duplicate sample key(s) {duplicates[:3]}")
    missing = sorted((expected - actual).elements())
    if missing:
        errors.append(f"missing sample key(s) {missing[:3]}")
    extra = sorted((actual - expected).elements())
    if extra:
        errors.append(f"unexpected sample key(s) {extra[:3]}")
    return errors


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


def _reconcile_chunk(
    stream_root: Path, record: ChunkRecordOnDisk, descriptor: StreamDescriptor | None
) -> PhysicalChunk:
    """Reconcile a commit record against every physical leaf it names."""
    # Artifact byte lengths are an independent leaf: a matching SHA does not
    # validate the record's own size claim.
    leaf_errors = list(_check_artifact_leaves(stream_root, record))

    packets_path = stream_root / record.model.packets.path
    if not packets_path.is_file():
        return PhysicalChunk(
            record, None, None, 0, "packets artifact is absent", {}, tuple(leaf_errors)
        )
    first, last, rows, error = read_packet_range(packets_path)
    counts, counts_error = _read_packet_facts(packets_path)
    if counts is None:
        return PhysicalChunk(
            record, first, last, rows, error or counts_error, {}, tuple(leaf_errors)
        )

    leaf_errors.extend(_check_references(stream_root, record, counts, descriptor))
    packet_rows, packet_read_error = _read_rows(packets_path, ["packet_seq", "payload_ref"])
    if packet_read_error is None:
        leaf_errors.extend(_check_payload_frames(stream_root, record, packet_rows))
    return PhysicalChunk(record, first, last, rows, error, counts, tuple(leaf_errors))


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
    chunks = tuple(_reconcile_chunk(stream_paths.root, record, descriptor) for record in records)

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
