"""Lifecycle log: append-only state transitions (spec §5; D16, D18).

Three orthogonal fields, deliberately not collapsed:

* ``lifecycle_state``    — where the machinery is
* ``closure_condition``  — how it closed
* ``recording_outcome``  — what it means scientifically

A crash writes no transition at all. That absence is the signal, and recovery
never fabricates one.
"""

import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from consciousness_lab.session.model import (
    ClosureCondition,
    LifecycleRecord,
    LifecycleState,
    RecordingOutcome,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import append_line

#: Valid transitions (spec §5). Anything absent is rejected loudly.
VALID_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.ALLOCATED: frozenset({LifecycleState.RECORDING, LifecycleState.FINALIZING}),
    LifecycleState.RECORDING: frozenset({LifecycleState.FINALIZING}),
    LifecycleState.FINALIZING: frozenset({LifecycleState.CLOSED}),
    LifecycleState.CLOSED: frozenset(),
}


class LifecycleError(RuntimeError):
    """An illegal lifecycle transition was attempted."""


@dataclass(frozen=True)
class SealedLifecycle:
    """The result of reading a lifecycle log prefix."""

    records: tuple[LifecycleRecord, ...]
    terminal_state: LifecycleState | None
    closure_condition: ClosureCondition | None
    sealed_outcome: RecordingOutcome | None
    outcome_reason: str | None


def now_reading() -> tuple[int, int]:
    """A paired (utc_ns, monotonic_ns) reading.

    ``monotonic_ns`` is the session spine because it is immune to a UTC step;
    ``utc_ns`` is recorded alongside so a step is visible as a discontinuity
    rather than silent corruption.
    """
    return time.time_ns(), time.monotonic_ns()


class LifecycleLog:
    """Append-only writer over ``lifecycle.jsonl``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._records: list[LifecycleRecord] = []
        if path.exists():
            self._records = list(read_records(path))

    @property
    def records(self) -> list[LifecycleRecord]:
        return list(self._records)

    @property
    def state(self) -> LifecycleState | None:
        return self._records[-1].state if self._records else None

    def append(
        self,
        state: LifecycleState,
        *,
        closure_condition: ClosureCondition | None = None,
        recording_outcome: RecordingOutcome | None = None,
        outcome_reason: str | None = None,
        actor: str = "system",
    ) -> LifecycleRecord:
        current = self.state
        if current is None:
            if state is not LifecycleState.ALLOCATED:
                raise LifecycleError(f"a session must open at ALLOCATED, not {state}")
        elif state not in VALID_TRANSITIONS[current]:
            raise LifecycleError(f"invalid lifecycle transition {current} -> {state}")

        if state is LifecycleState.CLOSED:
            if closure_condition is None or recording_outcome is None:
                raise LifecycleError(
                    "CLOSED must carry both closure_condition and recording_outcome"
                )
        elif closure_condition is not None or recording_outcome is not None:
            raise LifecycleError("only CLOSED may carry a closure condition or outcome")

        utc_ns, monotonic_ns = now_reading()
        record = LifecycleRecord(
            seq=len(self._records),
            state=state,
            closure_condition=closure_condition,
            recording_outcome=recording_outcome,
            outcome_reason=outcome_reason,
            actor=actor,
            utc_ns=utc_ns,
            monotonic_ns=monotonic_ns,
        )
        body = record.model_dump(mode="json", exclude_none=True, exclude={"record_sha256"})
        append_line(self.path, canonical_json.dump_line(body))
        sealed = record.model_copy(update={"record_sha256": canonical_json.record_hash(body)})
        self._records.append(sealed)
        return sealed


def read_records(path: Path) -> Iterable[LifecycleRecord]:
    """Parse a lifecycle log. A torn final line is dropped, not guessed at."""
    if not path.exists():
        return []
    records: list[LifecycleRecord] = []
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            obj = canonical_json.loads(line)
        except (canonical_json.CanonicalizationError, ValueError):
            break
        records.append(LifecycleRecord.model_validate(obj))
    return records


def summarize(records: Iterable[LifecycleRecord]) -> SealedLifecycle:
    """Reduce a lifecycle prefix to its terminal state and sealed outcome."""
    ordered = tuple(records)
    if not ordered:
        return SealedLifecycle((), None, None, None, None)
    last = ordered[-1]
    return SealedLifecycle(
        records=ordered,
        terminal_state=last.state,
        closure_condition=last.closure_condition,
        sealed_outcome=last.recording_outcome,
        outcome_reason=last.outcome_reason,
    )
