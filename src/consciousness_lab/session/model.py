"""Typed models for Session Package v2 (`SESSION_SCHEMA_V2_PROPOSAL.md`; D8-D34).

These models are the on-disk contract. They encode structure and provenance
only: nothing here interprets a physiological signal, and no scientific
threshold appears in this file.
"""

import re
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from consciousness_lab.storage.integer_types import (
    ON_DISK_CONTEXT_KEY,
    Int64Decimal,
    UInt64Decimal,
)

_T = TypeVar("_T", bound=BaseModel)

SCHEMA_NAME = "session_package"
SCHEMA_VERSION = "2.0"
SCHEMA_MAJOR = 2

PSEUDONYM_PATTERN = r"^P[0-9]{3,6}$"


class Strict(BaseModel):
    """Base model for every on-disk record.

    ``extra="ignore"`` implements minor-version tolerance: a v2.1 package
    carrying a new optional field must still be readable by a v2.0 reader. This
    does not weaken integrity — hashes are computed over the raw parsed
    document, not the model, so an added field still changes the canonical bytes
    and is still detected. An unknown *major* version fails closed instead.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)


def load_on_disk(model: type[_T], data: object) -> _T:
    """Validate a document read from disk.

    The ``on_disk`` context makes int64/uint64 fields refuse JSON Numbers, so a
    value that already lost precision in a JSON parser cannot enter the system
    (spec §12.2.1, D25).
    """
    return model.model_validate(data, context={ON_DISK_CONTEXT_KEY: True})


class LifecycleState(StrEnum):
    ALLOCATED = "ALLOCATED"
    RECORDING = "RECORDING"
    FINALIZING = "FINALIZING"
    CLOSED = "CLOSED"


class ClosureCondition(StrEnum):
    CLEAN = "CLEAN"
    RECOVERED_UNCLEAN = "RECOVERED_UNCLEAN"


class RecordingOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    #: Not a fourth scientific outcome. It means no human has classified this
    #: session yet, and it is what recovery writes rather than guessing between
    #: an operator abort and a power failure.
    UNCLASSIFIED = "UNCLASSIFIED"


class RawCaptureLevel(StrEnum):
    TRANSPORT_PAYLOAD = "transport_payload"
    LIBRARY_DECODED = "library_decoded"
    SYNTHETIC = "synthetic"


class SampleLayout(StrEnum):
    DENSE_FIXED_LIST = "dense_fixed_list"
    SPARSE_LONG = "sparse_long"


class StreamCloseStatus(StrEnum):
    """Terminal closure of one raw stream (v2 §7.1, D33).

    ``RECOVERED_UNCLEAN`` is new in v2 and is an *operational* fact: recovery
    observed that the process disappeared while this stream had no durable
    terminal close record. It must never be silently mapped to ``FAILED`` or
    ``DISCONNECTED``, which would assert a device-specific cause nobody saw.
    """

    CLEAN = "CLEAN"
    DISCONNECTED = "DISCONNECTED"
    RECONFIGURED = "RECONFIGURED"
    FAILED = "FAILED"
    RECOVERED_UNCLEAN = "RECOVERED_UNCLEAN"


class ObservationKind(StrEnum):
    TIME = "time"
    COUNTER = "counter"


class AppliesTo(StrEnum):
    SAMPLE_ACQUISITION = "sample_acquisition"
    PACKET_ASSEMBLY = "packet_assembly"
    TRANSMISSION = "transmission"
    HOST_RECEIPT = "host_receipt"
    UNKNOWN = "unknown"


class ObservationProvenance(StrEnum):
    DEVICE_PROVIDED = "device_provided"
    LIBRARY_PROVIDED = "library_provided"


class ClaimStatus(StrEnum):
    VERIFIED = "verified"
    ASSUMED = "assumed"
    UNKNOWN = "unknown"


class HardwareClaim(Strict):
    """A hardware-dependent claim with its provenance (spec §8, D14).

    Every fact about a physical device is one of these. Today every ``status``
    in this repository reads ``assumed``: no device has been connected.
    """

    value: Any = None
    status: ClaimStatus = ClaimStatus.ASSUMED
    source: str | None = None
    observed_at: str | None = None


class VerificationStatus(StrEnum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


class HardwareVerification(Strict):
    """Whether a device has been tested on real hardware (spec §8, D14).

    Every device in this repository reads ``unverified``: none has ever been
    connected. Promoting this to ``verified`` requires a dated entry in
    ``docs/HARDWARE.md``, never a code change alone.
    """

    status: VerificationStatus = VerificationStatus.UNVERIFIED
    ref: str = "docs/HARDWARE.md"


class ClockReading(Strict):
    """A paired monotonic/UTC reading, each naming the clock that produced it."""

    utc_ns: Int64Decimal
    monotonic_ns: Int64Decimal
    utc_clock_id: str = "CLOCK_REALTIME"
    monotonic_clock_id: str = "CLOCK_MONOTONIC"
    utc_quality: str = "unknown"


class Origin(Strict):
    """How this package came to exist (spec §16).

    ``replay`` and ``synthetic`` packages must say so, so that neither can ever
    be mistaken for a live recording downstream.
    """

    kind: Literal["recording", "replay", "synthetic"] = "recording"
    source_session_id: str | None = None
    source_manifest_sha256: str | None = None
    replay_tool_version: str | None = None
    generator_seed: UInt64Decimal | None = None

    @model_validator(mode="after")
    def _origin_provenance(self) -> "Origin":
        if self.kind == "replay":
            missing = [
                name
                for name in ("source_session_id", "source_manifest_sha256", "replay_tool_version")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(
                    f"a replay origin must record {missing} so it can never be "
                    "mistaken for a recording (spec 16)"
                )
        if self.kind == "synthetic" and self.generator_seed is None:
            raise ValueError("a synthetic origin must record its generator seed (spec 16)")
        return self


class Protocol(Strict):
    """Protocol identity. ``UNSPECIFIED`` is honest: no Phase 0 protocol exists."""

    id: str = "UNSPECIFIED"
    version: str = "UNSPECIFIED"
    sha256: str | None = None


class HostInfo(Strict):
    hostname_alias: str
    os: str
    arch: str


class SoftwareInfo(Strict):
    """Code provenance. ``dirty`` is ``None`` when it could not be read.

    ``False`` would assert a clean tree we never observed; unknown provenance is
    recorded as unknown.
    """

    repo_commit: str | None = None
    dirty: bool | None = None


class EnvironmentInfo(Strict):
    python_version: str
    uv_lock_sha256: str | None = None


class Allocation(Strict):
    """``allocation.json`` — immutable after allocation (spec §7.1)."""

    schema_name: Literal["session_package"] = "session_package"
    schema_version: str = SCHEMA_VERSION
    session_id: str
    allocated_at: ClockReading
    origin: Origin = Field(default_factory=Origin)
    protocol: Protocol = Field(default_factory=Protocol)
    participant_pseudonym: Annotated[str, Field(pattern=PSEUDONYM_PATTERN)]
    host: HostInfo
    software: SoftwareInfo
    environment: EnvironmentInfo


class WriterConfig(Strict):
    """Writer configuration only. Not analysis epoching, not a scientific parameter.

    These stay JSON Numbers: their declared domain is a bounded int32 (§12.2.1).
    """

    chunk_max_seconds: int = 30
    chunk_max_rows: int = 100_000
    clock_snapshot_interval_seconds: int = 60
    note: str = "Writer configuration only. NOT analysis epoching and NOT a scientific parameter."


class DeviceRecord(Strict):
    """A device present in a run, identified by a study-local alias (D26).

    Raw serial numbers are never written into a session package.
    """

    device_alias: str
    device_kind: str
    firmware: HardwareClaim = Field(default_factory=HardwareClaim)
    library: HardwareClaim = Field(default_factory=HardwareClaim)


class Run(Strict):
    """``run.json`` — sealed at RECORDING_START (spec §7.2).

    ``required_streams`` is configuration, deliberately not a global constant:
    which real streams a protocol requires is a future protocol decision, and
    the finalizer evaluates whatever set the run declares.
    """

    sealed_at: ClockReading
    required_streams: list[str] = Field(default_factory=list)
    optional_streams: list[str] = Field(default_factory=list)
    devices: list[DeviceRecord] = Field(default_factory=list)
    writer_config: WriterConfig = Field(default_factory=WriterConfig)

    @model_validator(mode="after")
    def _disjoint(self) -> "Run":
        overlap = set(self.required_streams) & set(self.optional_streams)
        if overlap:
            raise ValueError(f"streams declared both required and optional: {sorted(overlap)}")
        return self


class Channel(Strict):
    """One channel. Identity lives here, never in a column name (spec §8)."""

    index: int
    channel_id: str
    unit: str | None = None
    dtype: str = "float32"
    physical_meaning: str | None = None
    status: ClaimStatus | None = None


class TimingCapabilities(Strict):
    """What a device is believed to provide. Every field is an unverified claim."""

    provides_device_time: HardwareClaim = Field(default_factory=HardwareClaim)
    provides_packet_counter: HardwareClaim = Field(default_factory=HardwareClaim)
    provides_sample_counter: HardwareClaim = Field(default_factory=HardwareClaim)
    packet_counter_width_bits: HardwareClaim = Field(default_factory=HardwareClaim)
    samples_per_packet: HardwareClaim = Field(default_factory=HardwareClaim)


class AcquisitionBackend(Strict):
    name: str
    version: str | None = None
    adapter_version: str | None = None


class Acquisition(Strict):
    """Acquisition provenance, and the v1 capture invariant (spec §9.0.1, §9.1; D13).

    For schema v1:

        transport_payload  <=>  transport_payload_preserved is True
        library_decoded     =>  transport_payload_preserved is False
        synthetic           =>  transport_payload_preserved is False

    The two fields are locked together so that no reader has to choose which
    one determines whether a payload artifact must exist.
    """

    backend: AcquisitionBackend
    raw_capture_level: RawCaptureLevel
    transport_payload_preserved: bool
    decode_boundary: str | None = None
    provenance: HardwareClaim = Field(default_factory=HardwareClaim)

    @model_validator(mode="after")
    def _capture_invariant(self) -> "Acquisition":
        expected = self.raw_capture_level is RawCaptureLevel.TRANSPORT_PAYLOAD
        if self.transport_payload_preserved is not expected:
            raise ValueError(
                f"raw_capture_level={self.raw_capture_level.value} requires "
                f"transport_payload_preserved={expected}, got "
                f"{self.transport_payload_preserved} (spec 9.1, DECISIONS D13)"
            )
        return self


class StreamDescriptor(Strict):
    """``raw/<stream_id>/descriptor.json`` — sealed at stream open (spec §8)."""

    stream_id: str
    descriptor_version: int = 1
    device_alias: str
    device_kind: str
    modality: str
    layout: SampleLayout
    channels: list[Channel]
    acquisition: Acquisition
    nominal_sample_rate_hz: HardwareClaim = Field(default_factory=HardwareClaim)
    actual_sample_rate_hz: None = None
    device_preset: HardwareClaim = Field(default_factory=HardwareClaim)
    timing_capabilities: TimingCapabilities = Field(default_factory=TimingCapabilities)
    channel_layout_provenance: HardwareClaim = Field(default_factory=HardwareClaim)
    hardware_verification: HardwareVerification = Field(default_factory=HardwareVerification)
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _channels_ordered(self) -> "StreamDescriptor":
        if not self.channels:
            raise ValueError("a stream descriptor must declare at least one channel")
        expected = list(range(len(self.channels)))
        if [c.index for c in self.channels] != expected:
            raise ValueError("channel indices must be 0..n-1 in order")
        ids = [c.channel_id for c in self.channels]
        if len(set(ids)) != len(ids):
            raise ValueError("channel ids must be unique within a stream")
        return self

    @property
    def n_channels(self) -> int:
        return len(self.channels)

    @property
    def expects_payload_artifact(self) -> bool:
        """Whether committed chunks for this stream must carry a payload file."""
        return self.acquisition.raw_capture_level is RawCaptureLevel.TRANSPORT_PAYLOAD


#: Artifact kinds a chunk may produce. ``payloads`` exists only at
#: ``transport_payload``; the other three are always required (v2 §6).
ARTIFACT_KINDS: tuple[str, ...] = ("packets", "observations", "samples", "payloads")
REQUIRED_ARTIFACT_KINDS: frozenset[str] = frozenset({"packets", "observations", "samples"})

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def artifact_relative_path(kind: str, chunk_id: int) -> str:
    """The one path an artifact may occupy, derived from its kind and chunk id.

    v2 persists no artifact path (D29): the path is a function of
    ``(kind, chunk_id)`` and every reader derives it the same way, so there is
    no stored second copy that could disagree with the file system.
    """
    if kind not in ARTIFACT_KINDS:
        raise ValueError(f"unknown artifact kind {kind!r}")
    suffix = "bin" if kind == "payloads" else "arrow"
    return f"{kind}/{chunk_id:06d}.{suffix}"


class ChunkCommit(Strict):
    """One chunk commit record (v2 §6; D28, D29, D34).

    A chunk is real iff its record appears in ``chunks.jsonl`` — there is no
    sidecar in v2. The record carries exactly three things: which chunk it is,
    what it chains to, and the hash of every artifact it produced.

    **No ``record_sha256``.** A record's own hash is derivable from its
    canonical bytes, and the next record's ``prev_record_sha256`` is precisely
    that value, so a reader computes it either way. Persisting it was a second
    copy of a derived value (D29). The chain's last record is covered by the
    whole-file hash in ``control_sha256``.

    **No artifact paths, sizes, packet ranges or descriptor hash.** Paths are
    deterministic, a SHA over a whole file already fixes its length, the
    physical ``packets`` rows are authoritative for the range, and a stream has
    exactly one immutable descriptor.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    chunk_id: UInt64Decimal
    prev_record_sha256: str
    artifact_sha256: dict[str, str]

    @model_validator(mode="after")
    def _artifacts(self) -> "ChunkCommit":
        keys = set(self.artifact_sha256)
        unknown = sorted(keys - set(ARTIFACT_KINDS))
        if unknown:
            raise ValueError(f"artifact_sha256 carries unknown kind(s) {unknown}")
        missing = sorted(REQUIRED_ARTIFACT_KINDS - keys)
        if missing:
            raise ValueError(f"artifact_sha256 is missing required kind(s) {missing}")
        for kind, digest in sorted(self.artifact_sha256.items()):
            if not _SHA256_PATTERN.match(digest):
                raise ValueError(f"artifact_sha256[{kind}] is not a lowercase hex SHA-256")
        if not _SHA256_PATTERN.match(self.prev_record_sha256):
            raise ValueError("prev_record_sha256 is not a lowercase hex SHA-256")
        return self

    @property
    def has_payload_artifact(self) -> bool:
        return "payloads" in self.artifact_sha256

    def artifact_path(self, kind: str) -> str:
        """Deterministic package-relative path of one artifact of this chunk."""
        return artifact_relative_path(kind, self.chunk_id)


class StreamClose(Strict):
    """``raw/<stream_id>/stream_close.json`` — the sole closure authority (D33).

    One immutable file per opened stream. It carries no ``stream_id``: the path
    already owns identity, and a second copy of an identity is exactly what v2
    removes. It carries no convenience summaries either.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    close_status: StreamCloseStatus


class LifecycleRecord(Strict):
    """One appended lifecycle transition (spec §5)."""

    seq: UInt64Decimal
    state: LifecycleState
    closure_condition: ClosureCondition | None = None
    recording_outcome: RecordingOutcome | None = None
    outcome_reason: str | None = None
    actor: str = "system"
    utc_ns: Int64Decimal
    monotonic_ns: Int64Decimal
    record_sha256: str | None = None


class AnnotationRecord(Strict):
    """One post-seal downgrade annotation (spec §5.1, D18, D19).

    Carries both ``from`` and ``to``: ``from`` must equal the effective outcome
    in force at that point in the file, or two conforming implementations could
    disagree about a log holding more than one downgrade.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    seq: UInt64Decimal
    from_outcome: RecordingOutcome = Field(alias="from")
    to_outcome: RecordingOutcome = Field(alias="to")
    actor: str
    reason: str
    utc_ns: Int64Decimal
    prev_record_sha256: str
    record_sha256: str | None = None


class AnnotationHead(Strict):
    """``annotations.head.json`` — the anti-deletion pointer (spec §5.1).

    A hash chain proves the records present are intact; it cannot prove that
    none was removed. This records the expected length and head of the chain
    outside the chain itself, so a deleted or boundary-truncated log is
    detectable instead of silently restoring a sealed ``COMPLETED``.
    """

    bytes: UInt64Decimal
    record_count: UInt64Decimal
    head_record_sha256: str | None = None


class RawRef(Strict):
    """A pointer from a semantic event to the raw row it refers to (spec §11).

    ``packet_seq`` is an int64 domain, so on disk it is a decimal string like
    every other 64-bit value; ``sample_index_in_packet`` is a bounded int32 and
    stays a JSON Number (§12.2.1).
    """

    stream_id: str
    packet_seq: Int64Decimal
    sample_index_in_packet: int | None = None


class EventRecord(Strict):
    """One entry in the shared ``events/events.jsonl`` (spec §11)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_seq: UInt64Decimal
    event_name: str
    payload_schema: str
    origin: Literal["system", "protocol", "operator", "device_link"]
    host_arrival_monotonic_ns: Int64Decimal
    host_arrival_utc_ns: Int64Decimal
    host_arrival_monotonic_clock_id: str = "CLOCK_MONOTONIC"
    host_arrival_utc_clock_id: str = "CLOCK_REALTIME"
    raw_ref: RawRef | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    record_sha256: str | None = None


class SealPointer(Strict):
    """A hashed prefix of an append-only file.

    ``sealed_len`` is kept because it is **semantic**: it marks the boundary
    beyond which annotations legitimately append. Its fixed ``path`` is dropped
    — ``lifecycle.jsonl`` is the only file this ever pointed at (v2 §12).
    """

    sealed_len: UInt64Decimal
    sealed_sha256: str


class Manifest(Strict):
    """``manifest.json`` — written once at finalization (v2 §7; D21, D30, D33).

    The v2 manifest is exactly two things: a **finalization marker** and an
    **integrity root**. It owns no semantic stream fact at all — closure lives
    in ``raw/<id>/stream_close.json``, requiredness in ``run.json``, chunk
    counts and chain heads in each ``chunks.jsonl``, and packet ranges in the
    physical ``packets`` artifacts.

    It carries **no outcome field**: the outcome belongs to the lifecycle log
    plus annotations. A valid manifest pair is a FINALIZATION marker, never a
    completion marker — a cleanly aborted session produces an identical pair.

    It carries **no ``session_id``**: identity is owned by ``allocation.json``,
    whose hash is in ``control_sha256``, and by the directory name. A manifest
    swapped between packages is caught because the allocation hash will not
    match.

    ``control_sha256`` is a **map**, path to SHA-256, so a duplicate path is
    structurally impossible. Raw artifacts are absent from it by design: they
    are sealed transitively through each stream's ``chunks.jsonl`` (D30).
    """

    schema_name: Literal["session_package"] = "session_package"
    schema_version: str = SCHEMA_VERSION
    sealed_at: ClockReading
    lifecycle_seal: SealPointer
    events_sha256: str
    control_sha256: dict[str, str]

    @model_validator(mode="after")
    def _hashes(self) -> "Manifest":
        if not _SHA256_PATTERN.match(self.events_sha256):
            raise ValueError("events_sha256 is not a lowercase hex SHA-256")
        for path, digest in sorted(self.control_sha256.items()):
            if not _SHA256_PATTERN.match(digest):
                raise ValueError(f"control_sha256[{path}] is not a lowercase hex SHA-256")
        return self
