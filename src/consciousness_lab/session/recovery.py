"""Recovery for incomplete packages (v2 §9.4.1, §9.4.2; D33, D34).

Recovery **reports**, and in exactly one narrow case it *resumes*. It never
repairs raw data, adopts orphans, deletes, infers intent, or promotes a session
to COMPLETED. The distinctions it draws are the ones only the system can draw
honestly:

* a *known* fatal writer error was diagnosed while the process was alive, so the
  session closed ``CLEAN`` / ``TECHNICAL_FAILURE`` with a reason;
* a crash left no transition at all, so cause and intent are genuinely unknown
  and it closes ``RECOVERED_UNCLEAN`` / ``UNCLASSIFIED``;
* a crash left a durable terminal lifecycle record but no manifest pair. That is
  ``INTERRUPTED_FINALIZATION``: the process already durably wrote what happened,
  and throwing that away would discard something it observed. The sealing
  transaction may be resumed **without altering the recorded outcome**.

Resuming an ``ABORTED`` package seals ``ABORTED``. Resuming a
``TECHNICAL_FAILURE`` package seals ``TECHNICAL_FAILURE``. Nothing is ever
upgraded to ``COMPLETED``, and lifecycle history is never rewritten.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session.lifecycle import (
    LifecycleLog,
    authoritative_records,
    now_reading,
    parse_records,
    summarize,
)
from consciousness_lab.session.model import (
    ClockReading,
    ClosureCondition,
    EventRecord,
    LifecycleRecord,
    LifecycleState,
    Manifest,
    RecordingOutcome,
    Run,
    SealPointer,
    StreamCloseStatus,
    load_on_disk,
)
from consciousness_lab.session.writer import EVENT_SCHEMAS, write_stream_close
from consciousness_lab.storage import canonical_json, package_layout
from consciousness_lab.storage.checksums import (
    atomic_write,
    atomic_write_new,
    find_incomplete,
    sha256_bytes,
    sha256_file,
)
from consciousness_lab.storage.paths import PackagePaths
from consciousness_lab.storage.safe_paths import find_symlinks
from consciousness_lab.storage.stream_state import read_all_physical_streams

__all__ = [
    "PackagePaths",
    "RecoveryReport",
    "ResumeError",
    "StructuralState",
    "close_unclean",
    "resume_finalization",
    "scan",
]

from consciousness_lab.storage.verifier import Finding, verify_package


class StructuralState(StrEnum):
    """What a package looks like on disk, independent of what it means."""

    SEALED = "sealed"
    ALLOCATED_ONLY = "allocated_only"
    INTERRUPTED_RECORDING = "interrupted_recording"
    INTERRUPTED_FINALIZATION = "interrupted_finalization"
    UNREADABLE = "unreadable"


class ResumeError(RuntimeError):
    """An interrupted finalization could not be resumed. Nothing was written."""


@dataclass
class RecoveryReport:
    """A description, not an action."""

    session_id: str
    state: StructuralState
    incomplete_files: list[str] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    unclosed_streams: list[str] = field(default_factory=list)
    can_close_unclean: bool = False
    can_resume_finalization: bool = False
    notes: list[str] = field(default_factory=list)


def scan(paths: PackagePaths) -> RecoveryReport:
    """Inspect a package and classify its structural state."""
    session_id = paths.root.name
    if not paths.allocation.is_file():
        return RecoveryReport(
            session_id,
            StructuralState.UNREADABLE,
            notes=["allocation.json is absent; this directory is not a session package"],
        )

    incomplete = [p.relative_to(paths.root).as_posix() for p in find_incomplete(paths.root)]
    verification = verify_package(paths)
    orphans = [
        issue.path or "" for issue in verification.issues if issue.finding is Finding.ORPHAN_FILE
    ]
    physical = read_all_physical_streams(paths)
    unclosed = sorted(
        stream_id
        for stream_id, state in physical.items()
        if state.directory_exists and not state.close_present
    )

    summary = summarize(authoritative_records(paths.lifecycle, verification.manifest))
    # Sealed means the manifest pair actually VERIFIES (condition 1), not that
    # two files happen to exist. A mutated manifest with a stale hash is not a
    # sealed package, and calling it one would hide the tampering.
    manifest_pair = verification.conditions.get(1, False)

    if manifest_pair:
        state = StructuralState.SEALED
    elif summary.terminal_state is LifecycleState.CLOSED:
        # The terminal fact is durable; only the seal is missing. This is NOT
        # UNREADABLE and NOT automatically RECOVERED_UNCLEAN (v2 §9.4.2).
        state = StructuralState.INTERRUPTED_FINALIZATION
    elif summary.terminal_state is LifecycleState.FINALIZING:
        state = StructuralState.INTERRUPTED_FINALIZATION
    elif summary.terminal_state is LifecycleState.RECORDING:
        state = StructuralState.INTERRUPTED_RECORDING
    elif summary.terminal_state is LifecycleState.ALLOCATED:
        state = StructuralState.ALLOCATED_ONLY
    else:
        state = StructuralState.UNREADABLE

    closed_terminal = summary.terminal_state is LifecycleState.CLOSED
    notes: list[str] = []
    if incomplete:
        notes.append(f"{len(incomplete)} incomplete write marker(s) present")
    if orphans:
        notes.append(
            f"{len(orphans)} orphan file(s) with no commit record; reported, never adopted"
        )
    if unclosed:
        notes.append(f"{len(unclosed)} stream(s) have no durable stream_close.json")
    if state is StructuralState.INTERRUPTED_FINALIZATION:
        notes.append(
            "finalization was interrupted; the terminal outcome is durable and the sealing "
            "transaction may be resumable"
            if closed_terminal
            else "finalization was interrupted before the terminal lifecycle record"
        )

    return RecoveryReport(
        session_id=session_id,
        state=state,
        incomplete_files=incomplete,
        orphan_files=[o for o in orphans if o],
        unclosed_streams=unclosed,
        # A CLOSED session is terminal: it is resumed, never re-closed.
        can_close_unclean=(not closed_terminal)
        and state
        in {
            StructuralState.ALLOCATED_ONLY,
            StructuralState.INTERRUPTED_RECORDING,
            StructuralState.INTERRUPTED_FINALIZATION,
        },
        can_resume_finalization=(
            closed_terminal and state is StructuralState.INTERRUPTED_FINALIZATION
        ),
        notes=notes,
    )


def _close_orphaned_streams(paths: PackagePaths, actor: str) -> list[str]:
    """Give every physical stream lacking a close record ``RECOVERED_UNCLEAN``.

    An operational observation, never a diagnosis: recovery saw that the process
    disappeared while this stream had no durable terminal close record, and says
    exactly that. Mapping it to ``FAILED`` or ``DISCONNECTED`` would assert a
    device-specific cause nobody observed (D33).
    """
    written: list[str] = []
    for stream_id, state in sorted(read_all_physical_streams(paths).items()):
        if state.directory_exists and not state.close_present:
            write_stream_close(
                paths.stream(stream_id).stream_close, StreamCloseStatus.RECOVERED_UNCLEAN
            )
            written.append(stream_id)
    return written


def close_unclean(paths: PackagePaths, *, actor: str = "recovery") -> RecoveryReport:
    """Close a crashed session as RECOVERED_UNCLEAN / UNCLASSIFIED.

    It never guesses between an operator abort and a power failure: those are
    different facts and only a human knows which. Classification, if it ever
    happens, is a later downgrade annotation.

    Per-stream closure is written **first**, so the durable closure records
    exist before the terminal lifecycle record that depends on them — the same
    ordering finalization uses, for the same reason.
    """
    report = scan(paths)
    if not report.can_close_unclean:
        return report
    written = _close_orphaned_streams(paths, actor)
    log = LifecycleLog(paths.lifecycle)
    if log.state in (LifecycleState.ALLOCATED, LifecycleState.RECORDING):
        log.append(LifecycleState.FINALIZING, actor=actor)
    log.append(
        LifecycleState.CLOSED,
        closure_condition=ClosureCondition.RECOVERED_UNCLEAN,
        recording_outcome=RecordingOutcome.UNCLASSIFIED,
        outcome_reason="process did not close the session; cause and intent unknown",
        actor=actor,
    )
    if written:
        report.notes.append(f"wrote RECOVERED_UNCLEAN closure for stream(s) {written}")
    report.notes.append("closed RECOVERED_UNCLEAN / UNCLASSIFIED; no outcome was inferred")
    return report


def resume_finalization(paths: PackagePaths) -> Manifest:
    """Complete an interrupted sealing transaction (v2 §9.4.2).

    This is **not** promotion. The outcome was already durably decided and is
    left exactly as written; what is completed is the transaction that was
    supposed to seal it. Every durable input is verified first — the lifecycle
    records and their record hashes, the event records and theirs, every
    ``stream_close.json``, the raw structural integrity and the control files.
    If any of that is contradictory or insufficient, this raises and writes
    nothing: recovery does not guess, and it never rewrites history.
    """
    report = scan(paths)
    if not report.can_resume_finalization:
        raise ResumeError(
            f"{paths.root.name} is {report.state.value}; only an interrupted finalization "
            "with a durable terminal lifecycle record can be resumed"
        )
    if report.incomplete_files:
        raise ResumeError(
            f"incomplete write marker(s) present: {report.incomplete_files}; the durable state "
            "is insufficient to seal"
        )
    links = find_symlinks(paths.root)
    if links:
        raise ResumeError("package contains symlink(s) and cannot be sealed")

    lifecycle_bytes = paths.lifecycle.read_bytes()
    error = package_layout.verify_jsonl_region(
        lifecycle_bytes, LifecycleRecord, require_record_hash=True
    )
    if error is not None:
        raise ResumeError(f"lifecycle.jsonl {error}")

    events_bytes = paths.events.read_bytes() if paths.events.is_file() else b""
    error = package_layout.verify_jsonl_region(events_bytes, EventRecord, require_record_hash=True)
    if error is not None:
        raise ResumeError(f"events/events.jsonl {error}")
    schema_ids, schema_error = package_layout.referenced_schema_ids(events_bytes)
    if schema_error is not None:
        raise ResumeError(schema_error)

    # Typed, not merely canonical: a resumable transaction must never create a
    # manifest pair over a structurally invalid control document.
    allocation_problem = package_layout.allocation_error(paths)
    if allocation_problem is not None:
        raise ResumeError(allocation_problem)
    run_obj, run_error = package_layout.read_canonical_json(paths.run)
    if run_error is not None:
        raise ResumeError(f"run.json {run_error}")
    try:
        run = load_on_disk(Run, run_obj)
    except ValueError as exc:
        raise ResumeError(f"run.json is invalid on disk ({exc})") from exc

    physical = read_all_physical_streams(paths)
    declared = set(run.required_streams) | set(run.optional_streams)
    undeclared = sorted(set(physical) - declared)
    if undeclared:
        raise ResumeError(f"stream(s) {undeclared} are not declared in the run contract")
    for stream_id, state in sorted(physical.items()):
        if not state.structurally_complete:
            raise ResumeError(
                f"stream {stream_id} is not intact: {state.chain_error or 'incomplete structure'}"
            )
        if state.close_error is not None or not state.close_present:
            raise ResumeError(
                f"stream {stream_id}: {state.close_error or 'no durable stream_close.json'}"
            )
        if state.order_errors or state.unexpected_files or state.orphan_artifacts:
            raise ResumeError(f"stream {stream_id} does not conform and cannot be sealed")
        for chunk in state.chunks:
            if not chunk.intact:
                raise ResumeError(
                    f"stream {stream_id}: chunk {chunk.chunk_id} does not conform to v2"
                )

    # "Write any remaining deterministically recoverable structures" (§9.4.2).
    # A schema snapshot is exactly that: its bytes come from the registry in
    # code, not from anything the crashed process held in memory. A referenced
    # id this build cannot produce is genuinely unrecoverable, and stops here.
    if not paths.annotations_head.exists():
        annotations_mod.write_initial_head(paths.annotations_head)
    if not paths.events.exists():
        atomic_write(paths.events, b"")
    for schema_id in sorted(schema_ids):
        target = paths.schemas / f"{schema_id}.json"
        if target.exists():
            continue
        body = EVENT_SCHEMAS.get(schema_id)
        if body is None:
            raise ResumeError(
                f"the sealed events log references schema {schema_id!r}, whose snapshot is "
                "absent and cannot be reconstructed"
            )
        atomic_write_new(target, canonical_json.canonicalize(body))

    noncanonical = package_layout.noncanonical_documents(paths, sorted(physical))
    if noncanonical:
        raise ResumeError(f"document(s) are not canonical on disk: {noncanonical}")

    scan_result = package_layout.scan_layout(paths, schema_ids)
    if scan_result.unexpected or scan_result.unreferenced_schemas or scan_result.missing_schemas:
        raise ResumeError(
            "the package file set is not closed; resuming would seal an unexpected or "
            "unreferenced file"
        )

    control: dict[str, str] = {}
    for relative in package_layout.expected_control_paths(sorted(physical), schema_ids):
        target = paths.root / relative
        if not target.is_file():
            raise ResumeError(f"control file {relative} is absent and cannot be sealed")
        control[relative] = sha256_file(target)

    derived_seal = SealPointer(
        sealed_len=len(lifecycle_bytes), sealed_sha256=sha256_bytes(lifecycle_bytes)
    )
    derived_events = sha256_bytes(events_bytes)

    # The four states the writer's step 10 / step 11 boundary can leave behind.
    if paths.manifest.is_file():
        return _resume_over_existing_manifest(paths, derived_seal, derived_events, control)
    if paths.manifest_sha256.exists():
        # State C. The specified order writes manifest.json first, so this pair
        # cannot arise from a crash — and a lone digest names bytes that no
        # longer exist. Inventing a manifest to match it would be fabricating
        # the very thing the digest is supposed to attest.
        raise ResumeError(
            "manifest.sha256 exists with no manifest.json; this cannot arise from the "
            "specified write order and no manifest may be invented to match it"
        )

    # State A. Neither file exists: reconstruct and write the pair.
    utc_ns, monotonic_ns = now_reading()
    manifest = Manifest(
        sealed_at=ClockReading(utc_ns=utc_ns, monotonic_ns=monotonic_ns),
        lifecycle_seal=derived_seal,
        events_sha256=derived_events,
        control_sha256=control,
    )
    manifest_bytes = canonical_json.canonicalize(manifest.model_dump(mode="json"))
    atomic_write_new(paths.manifest, manifest_bytes)
    atomic_write_new(paths.manifest_sha256, (sha256_bytes(manifest_bytes) + "\n").encode("utf-8"))
    return manifest


def _resume_over_existing_manifest(
    paths: PackagePaths,
    derived_seal: SealPointer,
    derived_events: str,
    derived_control: dict[str, str],
) -> Manifest:
    """States B and D: ``manifest.json`` already exists.

    **The existing manifest is never replaced.** It is immutable the moment it
    lands, and rewriting it would discard bytes the process durably wrote and
    substitute our reconstruction of them — the same mistake as guessing an
    outcome, one level down.

    So it is *checked* instead: parsed, required to be canonical on disk, and
    required to agree exactly with the state recovery independently derives. If
    it agrees, only ``manifest.sha256`` is written, over those existing bytes.
    If it disagrees, this blocks and neither file is touched.
    """
    raw = paths.manifest.read_bytes()
    error = package_layout.canonical_document_error(raw)
    if error is not None:
        raise ResumeError(f"manifest.json {error}")
    manifest = package_layout.read_manifest(paths.manifest)
    if manifest is None:
        raise ResumeError("manifest.json exists but cannot be parsed as a v2 manifest")

    disagreements: list[str] = []
    if (
        manifest.lifecycle_seal.sealed_len != derived_seal.sealed_len
        or manifest.lifecycle_seal.sealed_sha256 != derived_seal.sealed_sha256
    ):
        disagreements.append("lifecycle_seal")
    if manifest.events_sha256 != derived_events:
        disagreements.append("events_sha256")
    if manifest.control_sha256 != derived_control:
        disagreements.append("control_sha256")
    if disagreements:
        raise ResumeError(
            f"the existing manifest.json disagrees with the durable state on "
            f"{disagreements}; it will not be replaced"
        )

    digest = sha256_bytes(raw)
    if paths.manifest_sha256.is_file():
        # State D: both exist and the pair does not verify, or scan() would have
        # called this package SEALED. Neither file is rewritten.
        recorded = paths.manifest_sha256.read_text(encoding="utf-8").strip()
        raise ResumeError(
            "manifest.sha256 is present and does not match manifest.json "
            f"({recorded[:12]}... vs {digest[:12]}...); neither file is rewritten"
        )
    # State B: complete the transaction over the bytes already on disk.
    atomic_write_new(paths.manifest_sha256, (digest + "\n").encode("utf-8"))
    return manifest


def sealed_outcome(paths: PackagePaths) -> RecordingOutcome | None:
    """The outcome durably recorded in the lifecycle log, whatever it is."""
    return summarize(parse_records(paths.lifecycle.read_bytes())).sealed_outcome
