"""Allocation, lifecycle transitions and outcomes — category A (spec §5, §12.1)."""

import uuid

import pytest
from pydantic import ValidationError

from consciousness_lab.session.allocator import allocate_session
from consciousness_lab.session.lifecycle import (
    LifecycleError,
    LifecycleLog,
    read_records,
    summarize,
)
from consciousness_lab.session.model import (
    Allocation,
    ClockReading,
    ClosureCondition,
    LifecycleState,
    RecordingOutcome,
    Run,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import verify_package
from tests.conftest import build_session


def test_allocation_is_durable_and_uuid4(data_root: DataRoot) -> None:
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    parsed = uuid.UUID(allocated.session_id)
    assert parsed.version == 4, "opaque identity, no embedded chronology (D9)"
    assert allocated.paths.allocation.is_file()
    assert allocated.paths.lifecycle.is_file()
    on_disk = Allocation.model_validate(
        canonical_json.loads(allocated.paths.allocation.read_bytes())
    )
    assert on_disk.session_id == allocated.session_id


def test_allocation_id_carries_no_timestamp(data_root: DataRoot) -> None:
    """Chronology lives in a field with provenance, never in the identifier."""
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    assert allocated.allocation.allocated_at.utc_ns > 0
    assert str(allocated.allocation.allocated_at.utc_ns) not in allocated.session_id


def test_existing_directory_cannot_be_merged_into(data_root: DataRoot) -> None:
    """A collision makes mkdir fail and a new id is generated; nothing merges."""
    first = allocate_session(data_root, participant_pseudonym="P001")
    second = allocate_session(data_root, participant_pseudonym="P002")
    assert first.session_id != second.session_id
    assert first.paths.root.exists() and second.paths.root.exists()

    # And an occupied path is never reused.
    taken = data_root.sessions / str(uuid.uuid4())
    taken.mkdir()
    (taken / "sentinel").write_text("not a session", encoding="utf-8")
    third = allocate_session(data_root, participant_pseudonym="P003")
    assert third.paths.root != taken
    assert (taken / "sentinel").read_text(encoding="utf-8") == "not a session"


@pytest.mark.parametrize("bad", ["Erandi", "p001", "P01", "P0000000", "", "P001 "])
def test_free_form_participant_identifiers_are_refused(data_root: DataRoot, bad: str) -> None:
    """A regex prevents a real name being typed in; an opaque string would not."""
    with pytest.raises((ValidationError, ValueError)):
        allocate_session(data_root, participant_pseudonym=bad)


def test_valid_transitions(data_root: DataRoot, tmp_path_factory: pytest.TempPathFactory) -> None:
    log = LifecycleLog(tmp_path_factory.mktemp("lc") / "lifecycle.jsonl")
    log.append(LifecycleState.ALLOCATED)
    log.append(LifecycleState.RECORDING)
    log.append(LifecycleState.FINALIZING)
    log.append(
        LifecycleState.CLOSED,
        closure_condition=ClosureCondition.CLEAN,
        recording_outcome=RecordingOutcome.COMPLETED,
    )
    assert log.state is LifecycleState.CLOSED


@pytest.mark.parametrize(
    "sequence",
    [
        [LifecycleState.RECORDING],
        [LifecycleState.ALLOCATED, LifecycleState.CLOSED],
        [LifecycleState.ALLOCATED, LifecycleState.RECORDING, LifecycleState.ALLOCATED],
    ],
)
def test_invalid_transitions_fail_loudly(
    tmp_path_factory: pytest.TempPathFactory, sequence: list[LifecycleState]
) -> None:
    log = LifecycleLog(tmp_path_factory.mktemp("lc") / "lifecycle.jsonl")
    with pytest.raises(LifecycleError):
        for state in sequence:
            log.append(state)


def test_closed_is_terminal(tmp_path_factory: pytest.TempPathFactory) -> None:
    log = LifecycleLog(tmp_path_factory.mktemp("lc") / "lifecycle.jsonl")
    log.append(LifecycleState.ALLOCATED)
    log.append(LifecycleState.FINALIZING)
    log.append(
        LifecycleState.CLOSED,
        closure_condition=ClosureCondition.CLEAN,
        recording_outcome=RecordingOutcome.ABORTED,
    )
    with pytest.raises(LifecycleError):
        log.append(LifecycleState.RECORDING)


def test_closed_requires_both_closure_and_outcome(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The three fields stay separate: CLOSED without them is meaningless."""
    log = LifecycleLog(tmp_path_factory.mktemp("lc") / "lifecycle.jsonl")
    log.append(LifecycleState.ALLOCATED)
    log.append(LifecycleState.FINALIZING)
    with pytest.raises(LifecycleError):
        log.append(LifecycleState.CLOSED)


def test_non_terminal_states_may_not_carry_an_outcome(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    log = LifecycleLog(tmp_path_factory.mktemp("lc") / "lifecycle.jsonl")
    with pytest.raises(LifecycleError):
        log.append(LifecycleState.ALLOCATED, recording_outcome=RecordingOutcome.COMPLETED)


@pytest.mark.parametrize(
    ("outcome", "closure"),
    [
        (RecordingOutcome.COMPLETED, ClosureCondition.CLEAN),
        (RecordingOutcome.ABORTED, ClosureCondition.CLEAN),
        (RecordingOutcome.TECHNICAL_FAILURE, ClosureCondition.CLEAN),
        (RecordingOutcome.UNCLASSIFIED, ClosureCondition.RECOVERED_UNCLEAN),
    ],
)
def test_every_outcome_is_preserved_and_distinguishable(
    data_root: DataRoot, outcome: RecordingOutcome, closure: ClosureCondition
) -> None:
    """R3: a failed session is a record, not a deletion."""
    built = build_session(data_root, finalize_outcome=outcome, closure=closure)
    summary = summarize(read_records(built.allocated.paths.lifecycle))
    assert summary.sealed_outcome is outcome
    assert summary.closure_condition is closure
    assert built.allocated.paths.root.is_dir(), "the package survives whatever happened"
    completed = verify_package(built.allocated.paths).is_completed
    assert completed is (outcome is RecordingOutcome.COMPLETED)


def test_run_streams_may_not_be_both_required_and_optional() -> None:
    with pytest.raises(ValidationError):
        Run(
            sealed_at=ClockReading(utc_ns=1, monotonic_ns=1),
            required_streams=["a"],
            optional_streams=["a"],
        )


def test_required_stream_sets_are_configuration_not_a_hardcoded_decision(
    data_root: DataRoot,
) -> None:
    """Which streams are required is a future protocol decision, per run."""
    from consciousness_lab.session.model import RawCaptureLevel
    from consciousness_lab.synthetic.source import SyntheticStreamSpec

    specs = [
        SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD),
        SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED),
    ]
    one = build_session(data_root, streams=specs, required=("synthetic.eeg",))
    both = build_session(data_root, streams=specs, required=("synthetic.eeg", "synthetic.ecg"))
    assert verify_package(one.allocated.paths).is_completed
    assert verify_package(both.allocated.paths).is_completed
    assert one.result is not None and both.result is not None
    assert {s.stream_id for s in one.result.manifest.streams if s.required} == {"synthetic.eeg"}
    assert {s.stream_id for s in both.result.manifest.streams if s.required} == {
        "synthetic.eeg",
        "synthetic.ecg",
    }
