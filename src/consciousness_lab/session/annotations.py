"""Post-seal annotations and the effective outcome (spec §5.1; D17, D18, D19, D20).

`COMPLETED` may be created only inside the sealed lifecycle prefix, at a clean
finalization. Annotations may only downgrade, and the permitted set is exactly
four transitions. Nothing may create or restore `COMPLETED` — including from
`UNCLASSIFIED`, because every crashed session closes `UNCLASSIFIED` and an
upgrade path from there would bypass the sealed-prefix rule on every crash.

The resolver FAILS CLOSED: when annotation integrity cannot be established the
effective outcome is INDETERMINATE and `is_completed` is false. It never falls
back to a sealed `COMPLETED`, because "the downgrade record is unreadable" and
"there was no downgrade" are indistinguishable from the bytes and only one of
them is safe to assume.
"""

import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from consciousness_lab.session.model import AnnotationHead, AnnotationRecord, RecordingOutcome
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import append_line, atomic_write

#: The complete permitted transition set for schema v1 (D18, D19).
#: Lateral ABORTED <-> TECHNICAL_FAILURE is deliberately absent (D19).
PERMITTED_TRANSITIONS: frozenset[tuple[RecordingOutcome, RecordingOutcome]] = frozenset(
    {
        (RecordingOutcome.COMPLETED, RecordingOutcome.ABORTED),
        (RecordingOutcome.COMPLETED, RecordingOutcome.TECHNICAL_FAILURE),
        (RecordingOutcome.UNCLASSIFIED, RecordingOutcome.ABORTED),
        (RecordingOutcome.UNCLASSIFIED, RecordingOutcome.TECHNICAL_FAILURE),
    }
)


class AnnotationStatus(StrEnum):
    """Integrity status of a package's annotation log."""

    OK = "OK"
    INDETERMINATE = "INDETERMINATE"


class RejectReason(StrEnum):
    TARGET_COMPLETED = "target_completed"
    STALE_FROM = "stale_from"
    TRANSITION_NOT_PERMITTED = "transition_not_permitted"


@dataclass(frozen=True)
class RejectedAnnotation:
    seq: int
    reason: RejectReason
    detail: str


@dataclass(frozen=True)
class EffectiveOutcome:
    """Result of resolving the effective outcome.

    ``outcome`` is ``None`` exactly when ``status`` is INDETERMINATE: the
    outcome is undefined, not merely unknown, and callers must not substitute
    the sealed value.
    """

    status: AnnotationStatus
    outcome: RecordingOutcome | None
    sealed_outcome: RecordingOutcome | None
    rejected: tuple[RejectedAnnotation, ...] = ()
    accepted_count: int = 0
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status is AnnotationStatus.OK and not self.rejected


class AnnotationError(RuntimeError):
    """An annotation could not be appended."""


@dataclass
class _ParsedLog:
    records: list[AnnotationRecord] = field(default_factory=list)
    chain_ok: bool = True
    detail: str = ""


def _parse_log(path: Path) -> _ParsedLog:
    """Parse and hash-chain-verify the annotation log."""
    parsed = _ParsedLog()
    prev = canonical_json.ZERO_HASH
    raw = path.read_bytes()
    for index, line in enumerate(raw.split(b"\n")):
        if not line.strip():
            continue
        try:
            obj = canonical_json.loads(line)
        except (canonical_json.CanonicalizationError, ValueError) as exc:
            parsed.chain_ok = False
            parsed.detail = f"line {index}: not parseable ({exc})"
            return parsed
        if not isinstance(obj, dict) or not canonical_json.verify_record(obj):
            parsed.chain_ok = False
            parsed.detail = f"line {index}: record_sha256 does not verify"
            return parsed
        try:
            record = AnnotationRecord.model_validate(obj)
        except ValueError as exc:
            parsed.chain_ok = False
            parsed.detail = f"line {index}: malformed annotation ({exc})"
            return parsed
        if record.prev_record_sha256 != prev:
            parsed.chain_ok = False
            parsed.detail = f"line {index}: hash chain broken"
            return parsed
        prev = str(record.record_sha256)
        parsed.records.append(record)
    return parsed


def read_head(path: Path) -> AnnotationHead | None:
    if not path.exists():
        return None
    try:
        return AnnotationHead.model_validate(canonical_json.loads(path.read_bytes()))
    except (canonical_json.CanonicalizationError, ValueError):
        return None


def resolve_effective_outcome(
    *,
    sealed_outcome: RecordingOutcome | None,
    annotations_path: Path,
    head_path: Path,
) -> EffectiveOutcome:
    """Compute the effective outcome exactly as specified in §5.1."""

    def indeterminate(detail: str) -> EffectiveOutcome:
        return EffectiveOutcome(
            status=AnnotationStatus.INDETERMINATE,
            outcome=None,
            sealed_outcome=sealed_outcome,
            detail=detail,
        )

    # Step 0. Finalization always writes the head file, so its absence means
    # tampering or loss, never "no annotations".
    head = read_head(head_path)
    if head is None:
        return indeterminate("annotations.head.json is missing or unreadable")

    log_exists = annotations_path.exists()
    log_size = annotations_path.stat().st_size if log_exists else 0

    # Step 1. Zero records: the log must be absent or empty.
    if head.record_count == 0:
        if log_exists and log_size > 0:
            return indeterminate("head reports zero records but annotations.jsonl is not empty")
        if head.bytes != 0 or head.head_record_sha256 is not None:
            return indeterminate("head is internally inconsistent at zero records")
        return EffectiveOutcome(AnnotationStatus.OK, sealed_outcome, sealed_outcome)

    # Step 2. The log must exist, match the recorded length, and end at the
    # recorded head hash. This is what makes a deleted or boundary-truncated
    # log detectable, which a hash chain alone cannot do.
    if not log_exists:
        return indeterminate("head reports records but annotations.jsonl is missing")
    if log_size != head.bytes:
        return indeterminate(f"annotations.jsonl is {log_size} bytes, head expects {head.bytes}")

    parsed = _parse_log(annotations_path)
    if not parsed.chain_ok:
        return indeterminate(f"annotation chain failed: {parsed.detail}")
    if len(parsed.records) != head.record_count:
        return indeterminate(
            f"annotations.jsonl holds {len(parsed.records)} records, "
            f"head expects {head.record_count}"
        )
    if parsed.records[-1].record_sha256 != head.head_record_sha256:
        return indeterminate("head_record_sha256 does not match the last record")

    # Step 2b. Apply in file order against a running effective outcome.
    running = sealed_outcome
    rejected: list[RejectedAnnotation] = []
    accepted = 0
    for record in parsed.records:
        if record.to_outcome is RecordingOutcome.COMPLETED:
            rejected.append(
                RejectedAnnotation(
                    record.seq, RejectReason.TARGET_COMPLETED, "cannot create COMPLETED"
                )
            )
            continue
        if record.from_outcome is not running:
            rejected.append(
                RejectedAnnotation(
                    record.seq,
                    RejectReason.STALE_FROM,
                    f"from={record.from_outcome} but effective outcome is {running}",
                )
            )
            continue
        if (record.from_outcome, record.to_outcome) not in PERMITTED_TRANSITIONS:
            rejected.append(
                RejectedAnnotation(
                    record.seq,
                    RejectReason.TRANSITION_NOT_PERMITTED,
                    f"{record.from_outcome} -> {record.to_outcome} is not permitted in v1",
                )
            )
            continue
        running = record.to_outcome
        accepted += 1

    return EffectiveOutcome(
        status=AnnotationStatus.OK,
        outcome=running,
        sealed_outcome=sealed_outcome,
        rejected=tuple(rejected),
        accepted_count=accepted,
    )


def write_initial_head(head_path: Path) -> AnnotationHead:
    """Write the zero-state head file. Finalization always does this."""
    head = AnnotationHead(bytes=0, record_count=0, head_record_sha256=None)
    atomic_write(head_path, canonical_json.canonicalize(head.model_dump(mode="json")))
    return head


def append_annotation(
    *,
    annotations_path: Path,
    head_path: Path,
    sealed_outcome: RecordingOutcome | None,
    to_outcome: RecordingOutcome,
    actor: str,
    reason: str,
) -> AnnotationRecord:
    """Append one downgrade annotation, then atomically update the head.

    The head is updated *after* the append it describes lands, so a crash
    between the two leaves a detectable mismatch rather than a silent gap.
    """
    current = resolve_effective_outcome(
        sealed_outcome=sealed_outcome, annotations_path=annotations_path, head_path=head_path
    )
    if current.status is not AnnotationStatus.OK or current.outcome is None:
        raise AnnotationError(
            f"cannot annotate a package with indeterminate history: {current.detail}"
        )
    if to_outcome is RecordingOutcome.COMPLETED:
        raise AnnotationError("no annotation may create or restore COMPLETED (D18)")
    if (current.outcome, to_outcome) not in PERMITTED_TRANSITIONS:
        raise AnnotationError(
            f"{current.outcome} -> {to_outcome} is not a permitted v1 transition (D18, D19)"
        )

    head = read_head(head_path)
    assert head is not None  # resolve_effective_outcome would have failed otherwise
    prev = head.head_record_sha256 or canonical_json.ZERO_HASH

    record = AnnotationRecord.model_validate(
        {
            "seq": head.record_count,
            "from": current.outcome.value,
            "to": to_outcome.value,
            "actor": actor,
            "reason": reason,
            "utc_ns": time.time_ns(),
            "prev_record_sha256": prev,
        }
    )
    body = record.model_dump(
        mode="json", by_alias=True, exclude_none=True, exclude={"record_sha256"}
    )
    digest = canonical_json.record_hash(body)
    append_line(annotations_path, canonical_json.dump_line(body))

    new_head = AnnotationHead(
        bytes=annotations_path.stat().st_size,
        record_count=head.record_count + 1,
        head_record_sha256=digest,
    )
    atomic_write(head_path, canonical_json.canonicalize(new_head.model_dump(mode="json")))
    return record.model_copy(update={"record_sha256": digest})
