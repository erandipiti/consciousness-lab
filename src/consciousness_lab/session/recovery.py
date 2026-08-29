"""Recovery inspection for incomplete packages (spec §19).

Recovery **reports**. It does not repair, adopt, delete, infer intent, or
promote a session to COMPLETED. The distinction it draws is the one only the
system can draw honestly:

* a *known* fatal writer error was diagnosed while the process was alive, so it
  closes ``CLEAN`` / ``TECHNICAL_FAILURE`` with a reason;
* a crash left no transition at all, so cause and intent are genuinely unknown
  and it closes ``RECOVERED_UNCLEAN`` / ``UNCLASSIFIED``.

Collapsing those two would throw away a fact the system actually had.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from consciousness_lab.session.lifecycle import LifecycleLog, read_records, summarize
from consciousness_lab.session.model import (
    ClosureCondition,
    LifecycleState,
    RecordingOutcome,
)
from consciousness_lab.storage.checksums import find_incomplete
from consciousness_lab.storage.paths import PackagePaths

__all__ = [
    "PackagePaths",
    "RecoveryReport",
    "StructuralState",
    "close_unclean",
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


@dataclass
class RecoveryReport:
    """A description, not an action."""

    session_id: str
    state: StructuralState
    incomplete_files: list[str] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    can_close_unclean: bool = False
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

    summary = summarize(read_records(paths.lifecycle))
    # Sealed means the manifest pair actually VERIFIES (condition 1), not that
    # two files happen to exist. A mutated manifest with a stale hash is not a
    # sealed package, and calling it one would hide the tampering.
    manifest_pair = verification.conditions.get(1, False)

    if manifest_pair:
        state = StructuralState.SEALED
    elif summary.terminal_state is LifecycleState.FINALIZING:
        state = StructuralState.INTERRUPTED_FINALIZATION
    elif summary.terminal_state is LifecycleState.RECORDING:
        state = StructuralState.INTERRUPTED_RECORDING
    elif summary.terminal_state is LifecycleState.ALLOCATED:
        state = StructuralState.ALLOCATED_ONLY
    else:
        state = StructuralState.UNREADABLE

    notes: list[str] = []
    if incomplete:
        notes.append(f"{len(incomplete)} incomplete write marker(s) present")
    if orphans:
        notes.append(
            f"{len(orphans)} orphan file(s) with no commit record; reported, never adopted"
        )
    if state is StructuralState.INTERRUPTED_FINALIZATION:
        notes.append("finalization was interrupted; the package is not sealed")

    return RecoveryReport(
        session_id=session_id,
        state=state,
        incomplete_files=incomplete,
        orphan_files=[o for o in orphans if o],
        can_close_unclean=state
        in {
            StructuralState.ALLOCATED_ONLY,
            StructuralState.INTERRUPTED_RECORDING,
            StructuralState.INTERRUPTED_FINALIZATION,
        },
        notes=notes,
    )


def close_unclean(paths: PackagePaths, *, actor: str = "recovery") -> RecoveryReport:
    """Close a crashed session as RECOVERED_UNCLEAN / UNCLASSIFIED.

    It never guesses between an operator abort and a power failure: those are
    different facts and only a human knows which. Classification, if it ever
    happens, is a later downgrade annotation.
    """
    report = scan(paths)
    if not report.can_close_unclean:
        return report
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
    report.notes.append("closed RECOVERED_UNCLEAN / UNCLASSIFIED; no outcome was inferred")
    return report
