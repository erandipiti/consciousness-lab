"""Physical stream state — the single source of truth about what is on disk.

Both the finalizer preflight and the package verifier derive everything they
know about a raw stream from here, so there is exactly one definition of each
fact. That is the same reason this module existed in v1; what changed in v2 is
how little there is to reconcile. There is no sidecar to compare against the
chain, no manifest summary to compare against the chain, and no persisted path,
size, packet range or descriptor hash to compare against the artifacts. What
remains is a record checked against the physical bytes it names, which is the
only kind of check that does not compound (D29, D30).
"""

import hashlib
import itertools
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa

from consciousness_lab.session.model import (
    ARTIFACT_KINDS,
    REQUIRED_ARTIFACT_KINDS,
    ChunkCommit,
    SampleLayout,
    StreamClose,
    StreamCloseStatus,
    StreamDescriptor,
    load_on_disk,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage import payload as payload_mod
from consciousness_lab.storage.arrow_schema import (
    schema_conformance_error,
    schema_for_kind,
)
from consciousness_lab.storage.checksums import sha256_bytes, sha256_file
from consciousness_lab.storage.observations import ObservationError, validate_observation
from consciousness_lab.storage.paths import PackagePaths
from consciousness_lab.storage.safe_paths import UnsafePathError, resolve_within

#: Structural files every raw stream directory must contain once it is closed.
#: ``chunks.jsonl`` is created empty at stream open, so a zero-chunk stream is
#: structurally identical to any other and "index absent" never has to be
#: interpreted. ``stream_close.json`` appears when the stream terminates and is
#: the sole persisted closure authority (D33).
REQUIRED_STREAM_FILES = ("descriptor.json", "chunks.jsonl")
CLOSED_STREAM_FILES = ("descriptor.json", "chunks.jsonl", "stream_close.json")


@dataclass(frozen=True)
class ChunkRecordOnDisk:
    """One chunk commit record, kept in BOTH representations.

    Parsed-model equality is not record identity: two different on-disk
    documents can normalize to the same ``ChunkCommit`` — an unknown field that
    minor-version tolerance ignores, or an explicitly-null key where the
    contract requires the key omitted. So the canonical bytes of the COMPLETE
    parsed document are retained alongside the model, and identity comparisons
    use those.

    ``record_sha256`` is **derived** here, never read: v2 does not persist it in
    ``chunks.jsonl`` (D34). It is the digest of these canonical bytes, which is
    exactly what the next record carries as ``prev_record_sha256``.
    """

    model: ChunkCommit
    canonical_bytes: bytes
    #: Whether the ``payloads`` KEY is present in ``artifact_sha256``, which is
    #: the one key-presence relation v2 keeps: the payload artifact's hash has
    #: to live somewhere, and its authority is the descriptor's capture level.
    payloads_key_present: bool

    @property
    def record_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @property
    def chunk_id(self) -> int:
        return self.model.chunk_id


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
    #: ``packet_seq -> n_samples`` from the physical packets rows.
    packet_sample_counts: dict[int, int]
    #: Committed artifact missing at its deterministic path, or hashing wrong.
    artifact_errors: tuple[str, ...]
    #: Physical Arrow schema does not equal the v2 contract schema (D31).
    schema_errors: tuple[str, ...]
    #: Observation rows that fail the shared validator on READ (D31).
    row_semantics_errors: tuple[str, ...]
    #: Structural foreign-key and key-identity violations.
    reference_errors: tuple[str, ...]
    #: Payload framing and the frame <-> packets bijection.
    payload_errors: tuple[str, ...]

    @property
    def chunk_id(self) -> int:
        return self.record.chunk_id

    @property
    def intact(self) -> bool:
        return not (
            self.artifact_errors
            or self.schema_errors
            or self.row_semantics_errors
            or self.reference_errors
            or self.payload_errors
            or self.packet_error
        )


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
    chunks: tuple[PhysicalChunk, ...]
    chain_error: str | None
    #: Chain-level ordering violations. Per-chunk validation does not imply
    #: chain validation: every chunk of a reversed chain still matches its own
    #: artifact.
    order_errors: tuple[str, ...]
    #: The stream's durable closure record (D33). ``None`` when the file is
    #: absent, which for a finalized package is itself a violation.
    close_status: StreamCloseStatus | None
    close_present: bool
    close_error: str | None
    #: Files under the stream directory that the v2 layout does not define, and
    #: artifacts no commit record names.
    unexpected_files: tuple[str, ...]
    orphan_artifacts: tuple[str, ...]

    @property
    def commits(self) -> tuple[ChunkCommit, ...]:
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
        """First packet sequence, derived from the PHYSICAL packet rows."""
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
    """Parse one chunk record, keeping its canonical bytes.

    Returns an error string rather than raising. Three v2 rules are enforced
    here because they are properties of the bytes, not of the model:

    * the record is **canonical on disk** — re-canonicalizing the parsed
      document reproduces the physical bytes exactly (§9.3);
    * it carries **no ``record_sha256``** — that key was removed from
      ``chunks.jsonl`` in v2, and a document carrying one is not a v2 record;
    * the canonical bytes come from the FULL parsed document, so any field
      present on disk participates in identity, including one the model ignores.
    """
    try:
        obj = canonical_json.loads(raw)
    except (canonical_json.CanonicalizationError, ValueError) as exc:
        return f"not parseable ({exc})"
    if not isinstance(obj, dict):
        return "record is not a JSON object"
    if canonical_json.RECORD_HASH_KEY in obj:
        return "carries record_sha256, which chunks.jsonl does not persist in v2"
    canonical = canonical_json.canonicalize(obj)
    if canonical != raw:
        return "is not canonical on disk"
    try:
        model = load_on_disk(ChunkCommit, obj)
    except ValueError as exc:
        return f"malformed ({exc})"
    artifacts = obj.get("artifact_sha256")
    return ChunkRecordOnDisk(
        model=model,
        canonical_bytes=canonical,
        payloads_key_present=isinstance(artifacts, dict) and "payloads" in artifacts,
    )


def read_chunk_chain(index: Path) -> tuple[tuple[ChunkRecordOnDisk, ...], str | None]:
    """Parse and hash-chain-verify a stream's ``chunks.jsonl``.

    A chunk is real if and only if its record appears here, so this is the only
    place that decides what "committed" means (D28).

    A record is one complete canonical JSON object terminated by ``\\n`` (§4.1).
    Non-empty bytes after the last newline are a torn tail from an interrupted
    append: they are reported, and the chunk they would have described is not
    committed.
    """
    if not index.exists():
        return (), "chunks.jsonl is absent"
    raw = index.read_bytes()
    records: list[ChunkRecordOnDisk] = []
    prev = canonical_json.ZERO_HASH
    lines = raw.split(b"\n")
    tail = lines.pop() if lines else b""
    for number, line in enumerate(lines):
        if not line:
            return tuple(records), f"line {number} is empty; a record is one object per line"
        parsed = parse_chunk_record(line)
        if isinstance(parsed, str):
            return tuple(records), f"line {number} {parsed}"
        if parsed.model.prev_record_sha256 != prev:
            return tuple(records), f"line {number} breaks the hash chain"
        prev = parsed.record_sha256
        records.append(parsed)
    if tail:
        return tuple(records), "trailing bytes after the last newline (torn tail)"
    return tuple(records), None


def read_stream_close(path: Path) -> tuple[StreamCloseStatus | None, str | None]:
    """Read ``stream_close.json``, the sole persisted closure authority (D33)."""
    if not path.is_file():
        return None, None
    try:
        raw = path.read_bytes()
        obj = canonical_json.loads(raw)
    except (OSError, canonical_json.CanonicalizationError, ValueError) as exc:
        return None, f"stream_close.json is unreadable ({exc})"
    if canonical_json.canonicalize(obj) != raw:
        return None, "stream_close.json is not canonical on disk"
    try:
        model = load_on_disk(StreamClose, obj)
    except ValueError as exc:
        return None, f"stream_close.json is malformed ({exc})"
    return model.close_status, None


def _open_table(path: Path) -> tuple[pa.Table | None, str | None]:
    try:
        with path.open("rb") as handle:
            return pa.ipc.open_stream(handle).read_all(), None
    except (pa.ArrowException, OSError, TypeError, ValueError) as exc:
        return None, f"{path.name} unreadable ({exc})"


def _check_artifacts(
    stream_root: Path, record: ChunkRecordOnDisk, descriptor: StreamDescriptor | None
) -> tuple[str, ...]:
    """Every committed artifact exists at its deterministic path and hashes right.

    The path is derived from ``(kind, chunk_id)``, never read from the record —
    that is what removed four v1 relations at once (§5).
    """
    errors: list[str] = []
    commit = record.model

    if descriptor is not None:
        expects = descriptor.expects_payload_artifact
        if expects and not commit.has_payload_artifact:
            errors.append("no payloads hash at raw_capture_level=transport_payload")
        if not expects and commit.has_payload_artifact:
            errors.append(
                "carries a payloads hash at raw_capture_level="
                f"{descriptor.acquisition.raw_capture_level.value}"
            )

    for kind in ARTIFACT_KINDS:
        digest = commit.artifact_sha256.get(kind)
        relative = commit.artifact_path(kind)
        try:
            target = resolve_within(stream_root, relative)
        except UnsafePathError as exc:  # pragma: no cover - derived paths are safe
            errors.append(f"{relative}: {exc}")
            continue
        if digest is None:
            if target.exists():
                errors.append(f"{relative} exists but no commit hash names it")
            continue
        if not target.is_file():
            errors.append(f"{relative} is missing")
            continue
        if sha256_file(target) != digest:
            errors.append(f"{relative} does not hash to its committed value")
    return tuple(errors)


def _check_schemas(
    stream_root: Path, record: ChunkRecordOnDisk, descriptor: StreamDescriptor | None
) -> tuple[str, ...]:
    """Physical Arrow schema equality per artifact kind (D31, §10.1)."""
    if descriptor is None:
        return ()
    errors: list[str] = []
    for kind in ("packets", "observations", "samples"):
        if kind not in record.model.artifact_sha256:
            continue
        path = stream_root / record.model.artifact_path(kind)
        if not path.is_file():
            continue  # reported as a missing artifact
        table, error = _open_table(path)
        if table is None:
            errors.append(f"{kind}: {error}")
            continue
        expected = schema_for_kind(kind, descriptor.layout, descriptor.n_channels)
        mismatch = schema_conformance_error(table.schema, expected)
        if mismatch is not None:
            errors.append(f"{kind}: {mismatch}")
    return tuple(errors)


def _check_observation_semantics(stream_root: Path, record: ChunkRecordOnDisk) -> tuple[str, ...]:
    """Observation row semantics, validated on READ by the shared validator.

    v1 enforced this only in the writer, which is how a ``uint64`` observation
    carrying ``value_f64`` verified clean. Two definitions of "valid
    observation" is the defect class v2 removes, so this calls exactly the
    function the writer calls (D31, §10.2).
    """
    path = stream_root / record.model.artifact_path("observations")
    if not path.is_file():
        return ()
    table, error = _open_table(path)
    if table is None:
        return (str(error),)
    errors: list[str] = []
    for index, row in enumerate(table.to_pylist()):
        try:
            validate_observation(row)
        except ObservationError as exc:
            errors.append(f"observation row {index}: {exc}")
            if len(errors) >= 3:
                break
    return tuple(errors)


def _check_payload_frames(
    stream_root: Path, record: ChunkRecordOnDisk, packet_seqs: list[int]
) -> tuple[str, ...]:
    """Frame <-> packets bijection by identity (§13).

    A valid CRC proves a frame is internally intact. It does NOT prove the frame
    belongs to a packet of this chunk. v2 removed the persisted pointer, so the
    relation is the multiset of frame ``packet_seq`` values against the multiset
    of ``packets.packet_seq`` values — nothing to keep in step, only to compare.
    """
    if not record.model.has_payload_artifact:
        return ()
    blob_path = stream_root / record.model.artifact_path("payloads")
    if not blob_path.is_file():
        return ()
    frames, walk_error = payload_mod.iter_frames(blob_path.read_bytes())
    errors: list[str] = []
    if walk_error is not None:
        errors.append(f"payload log: {walk_error}")
    framed = Counter(frame.packet_seq for frame in frames)
    expected = Counter(packet_seqs)
    if framed != expected:
        missing = sorted((expected - framed).elements())
        extra = sorted((framed - expected).elements())
        if missing:
            errors.append(f"packet(s) {missing[:3]} have no payload frame")
        if extra:
            errors.append(f"payload frame(s) for packet(s) {extra[:3]} have no packet row")
    return tuple(errors)


def _read_rows(path: Path, columns: list[str]) -> tuple[list[dict[str, Any]], str | None]:
    try:
        with path.open("rb") as handle:
            table = pa.ipc.open_stream(handle).read_all()
        return list(table.select(columns).to_pylist()), None
    except (pa.ArrowException, OSError, KeyError, TypeError, ValueError) as exc:
        return [], str(exc)


def _check_references(
    stream_root: Path,
    record: ChunkRecordOnDisk,
    packet_counts: dict[int, int],
    descriptor: StreamDescriptor | None,
) -> tuple[str, ...]:
    """Structural relations from samples/observations to this chunk's packets.

    These follow from the schema's own statements — that ``n_samples`` is the
    logical sample positions carried in a packet, and what the primary key is
    for each layout (§10.3, §11, D32). They are structural, not scientific: no
    minimum count is implied anywhere.
    """
    errors: list[str] = []
    commit = record.model
    layout = descriptor.layout if descriptor is not None else None

    samples_path = stream_root / commit.artifact_path("samples")
    if samples_path.is_file():
        if layout is SampleLayout.SPARSE_LONG:
            rows, error = _read_rows(
                samples_path, ["packet_seq", "sample_index_in_packet", "channel_id"]
            )
            if error is not None:
                errors.append(f"samples artifact unreadable ({error})")
            else:
                channel_ids = (
                    {c.channel_id for c in descriptor.channels} if descriptor is not None else set()
                )
                errors.extend(_check_sparse_sample_keys(rows, packet_counts, channel_ids))
        else:
            rows, error = _read_rows(samples_path, ["packet_seq", "sample_index_in_packet"])
            if error is not None:
                errors.append(f"samples artifact unreadable ({error})")
            else:
                errors.extend(_check_dense_sample_keys(rows, packet_counts))

    observations_path = stream_root / commit.artifact_path("observations")
    if observations_path.is_file():
        rows, error = _read_rows(observations_path, ["packet_seq", "sample_index_in_packet"])
        if error is not None:
            errors.append(f"observations artifact unreadable ({error})")
        else:
            # Observations name no primary key, so no cardinality and no
            # uniqueness rule is invented for them (§10.3).
            errors.extend(_check_row_references(rows, packet_counts, "observations"))
    return tuple(errors)


def _check_row_references(
    rows: list[dict[str, Any]], packet_counts: dict[int, int], label: str
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
    """Exact key-set identity for a dense stream (§10.3).

    For a packet declaring ``n_samples = N`` the sample keys must be exactly
    ``(packet_seq, 0) .. (packet_seq, N-1)``, each once. A multiset comparison
    is used deliberately: comparing sets alone would let a duplicated key hide a
    missing one. ``n_samples = 0`` expects an empty key set. This is storage
    integrity, not a minimum-sample threshold.
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


def _check_sparse_sample_keys(
    rows: list[dict[str, Any]], packet_counts: dict[int, int], channel_ids: set[str]
) -> list[str]:
    """The v2 sparse identity contract (D32, §11).

    Primary key ``(packet_seq, sample_index_in_packet, channel_id)``, unique.
    ``packet_seq`` references a packet of this chunk, the index is within that
    packet's ``n_samples``, and the channel exists in the descriptor.

    **Complete channel coverage is deliberately not required.** No rule demands
    ``n_channels`` rows per sample position: sparse means absence may be
    meaningful, and inventing a coverage rule here would be a data-quality
    criterion this layer must not hold.
    """
    errors: list[str] = []
    seen: Counter[tuple[int, int, str]] = Counter()
    for position, row in enumerate(rows):
        seq = int(row["packet_seq"])
        if seq not in packet_counts:
            errors.append(
                f"samples row {position} references packet_seq {seq}, "
                "absent from this chunk's packets"
            )
            break
        index = row["sample_index_in_packet"]
        if index is None:
            errors.append(f"samples row {position} has a null sample index")
            break
        if not 0 <= int(index) < packet_counts[seq]:
            errors.append(
                f"samples row {position} sample index {index} is outside "
                f"n_samples={packet_counts[seq]} for packet {seq}"
            )
            break
        channel = row["channel_id"]
        if channel_ids and channel not in channel_ids:
            errors.append(
                f"samples row {position} names channel {channel!r}, not in the descriptor"
            )
            break
        seen[(seq, int(index), str(channel))] += 1
    duplicates = sorted(key for key, count in seen.items() if count > 1)
    if duplicates:
        errors.append(f"duplicate sparse sample key(s) {duplicates[:3]}")
    return errors


def read_packet_range(path: Path) -> tuple[int | None, int | None, int, str | None]:
    """Read the actual ``packet_seq`` values from a committed packets artifact.

    Returns ``(first, last, row_count, error)``. This is the PHYSICAL authority
    for a chunk's packet range — v2 persists no claimed range at all, so there
    is nothing to compare it against and nothing that can disagree with it.

    ``packet_seq`` is strictly increasing at arrival, so that property is
    enforced. Consecutiveness is deliberately NOT required: a gap is a device
    fact for a later ticket, and requiring it here would be a packet-loss
    criterion this ticket must not introduce.
    """
    try:
        with path.open("rb") as handle:
            table = pa.ipc.open_stream(handle).read_all()
        sequences = [int(value) for value in table.column("packet_seq").to_pylist()]
    except (pa.ArrowException, OSError, KeyError, TypeError, ValueError) as exc:
        return None, None, 0, f"packets artifact unreadable ({exc})"
    if not sequences:
        # The writer refuses to commit an empty chunk, so an empty artifact
        # under a commit record is a contradiction — reported, not resolved.
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


def _read_packet_facts(path: Path) -> tuple[dict[int, int] | None, str | None]:
    """Return ``packet_seq -> n_samples`` from a physical packets artifact."""
    try:
        with path.open("rb") as handle:
            table = pa.ipc.open_stream(handle).read_all()
        rows = table.select(["packet_seq", "n_samples"]).to_pylist()
    except (pa.ArrowException, OSError, KeyError, TypeError, ValueError) as exc:
        return None, f"packets artifact unreadable ({exc})"
    return {int(r["packet_seq"]): int(r["n_samples"]) for r in rows}, None


def _reconcile_chunk(
    stream_root: Path, record: ChunkRecordOnDisk, descriptor: StreamDescriptor | None
) -> PhysicalChunk:
    """Reconcile a commit record against every physical fact it names."""
    artifact_errors = _check_artifacts(stream_root, record, descriptor)
    schema_errors = _check_schemas(stream_root, record, descriptor)
    row_errors = _check_observation_semantics(stream_root, record)

    packets_path = stream_root / record.model.artifact_path("packets")
    if not packets_path.is_file():
        return PhysicalChunk(
            record,
            None,
            None,
            0,
            "packets artifact is absent",
            {},
            artifact_errors,
            schema_errors,
            row_errors,
            (),
            (),
        )
    first, last, rows, error = read_packet_range(packets_path)
    counts, counts_error = _read_packet_facts(packets_path)
    if counts is None:
        return PhysicalChunk(
            record,
            first,
            last,
            rows,
            error or counts_error,
            {},
            artifact_errors,
            schema_errors,
            row_errors,
            (),
            (),
        )

    reference_errors = _check_references(stream_root, record, counts, descriptor)
    payload_errors = _check_payload_frames(stream_root, record, sorted(counts))
    return PhysicalChunk(
        record,
        first,
        last,
        rows,
        error,
        counts,
        artifact_errors,
        schema_errors,
        row_errors,
        reference_errors,
        payload_errors,
    )


def _scan_stream_files(
    stream_root: Path, chunks: tuple[PhysicalChunk, ...], descriptor: StreamDescriptor | None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The stream directory holds exactly the v2 layout, and nothing else (§9.2).

    Two directions, deliberately. Missing committed artifacts are reported by
    ``_check_artifacts``; this reports the reverse — a file the layout does not
    define, or an artifact no commit record names. Without the reverse
    direction an unexpected immutable file simply sits outside the integrity
    DAG, unhashed and unnoticed.
    """
    unexpected: list[str] = []
    orphans: list[str] = []
    if not stream_root.is_dir():
        return (), ()

    committed: set[str] = set()
    for chunk in chunks:
        for kind in chunk.record.model.artifact_sha256:
            committed.add(chunk.record.model.artifact_path(kind))

    allowed_dirs = {"packets", "observations", "samples"}
    if descriptor is None or descriptor.expects_payload_artifact:
        allowed_dirs.add("payloads")

    for entry in sorted(stream_root.iterdir()):
        name = entry.name
        if entry.is_dir():
            if name not in allowed_dirs:
                unexpected.append(name)
                continue
            for artifact in sorted(entry.iterdir()):
                relative = f"{name}/{artifact.name}"
                if not artifact.is_file():
                    unexpected.append(relative)
                elif relative not in committed:
                    orphans.append(relative)
            continue
        if name not in CLOSED_STREAM_FILES:
            unexpected.append(name)
    return tuple(unexpected), tuple(orphans)


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
            obj = canonical_json.loads(raw)
            if canonical_json.canonicalize(obj) != raw:
                descriptor_error = "descriptor.json is not canonical on disk"
            else:
                descriptor = load_on_disk(StreamDescriptor, obj)
        except (OSError, canonical_json.CanonicalizationError, ValueError) as exc:
            descriptor_error = str(exc)

    records: tuple[ChunkRecordOnDisk, ...] = ()
    chain_error: str | None = None
    chunks_index_present = stream_paths.chunks_index.is_file()
    if directory_exists:
        records, chain_error = read_chunk_chain(stream_paths.chunks_index)
    chunks = tuple(_reconcile_chunk(stream_paths.root, record, descriptor) for record in records)

    close_status, close_error = read_stream_close(stream_paths.stream_close)
    unexpected, orphans = _scan_stream_files(stream_paths.root, chunks, descriptor)

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
        close_status=close_status,
        close_present=stream_paths.stream_close.is_file(),
        close_error=close_error,
        unexpected_files=unexpected,
        orphan_artifacts=orphans,
    )


def _check_chain_order(chunks: tuple[PhysicalChunk, ...]) -> tuple[str, ...]:
    """Chain-level ordering across the whole authoritative sequence.

    ``chunk_id`` strictly increases because the index is append-only and chunks
    are sealed in order, and ``packet_seq`` is strictly increasing at arrival
    for the whole stream, so chunk N+1 must begin after chunk N ends.

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


__all__ = [
    "CLOSED_STREAM_FILES",
    "REQUIRED_ARTIFACT_KINDS",
    "REQUIRED_STREAM_FILES",
    "ChunkRecordOnDisk",
    "PhysicalChunk",
    "PhysicalStreamState",
    "list_physical_streams",
    "parse_chunk_record",
    "read_all_physical_streams",
    "read_chunk_chain",
    "read_packet_range",
    "read_physical_stream",
    "read_stream_close",
]
