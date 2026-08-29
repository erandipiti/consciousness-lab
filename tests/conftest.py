"""Shared fixtures for Session Package v1 tests.

Every test writes into a pytest ``tmp_path``. Nothing here ever touches the
repository's own ``data/`` directory.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from consciousness_lab.session.allocator import AllocatedSession, allocate_session
from consciousness_lab.session.finalizer import FinalizationResult, finalize
from consciousness_lab.session.model import (
    ClockReading,
    ClosureCondition,
    RawCaptureLevel,
    RecordingOutcome,
    Run,
    StreamCloseStatus,
)
from consciousness_lab.session.writer import SessionWriter
from consciousness_lab.storage.chunk_writer import FaultHook
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSpec,
    build_descriptor,
)


@dataclass
class BuiltSession:
    """A written session package plus the handles a test may want to poke."""

    data_root: DataRoot
    allocated: AllocatedSession
    writer: SessionWriter
    result: FinalizationResult | None


@pytest.fixture
def data_root(tmp_path: Path) -> DataRoot:
    return DataRoot(tmp_path / "data")


def now_reading() -> ClockReading:
    return ClockReading(utc_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())


def build_session(
    data_root: DataRoot,
    *,
    streams: Sequence[SyntheticStreamSpec] | None = None,
    required: Sequence[str] = ("synthetic.eeg",),
    optional: Sequence[str] = (),
    chunks: int = 2,
    packets_per_chunk: int = 3,
    seed: int = 7,
    finalize_outcome: RecordingOutcome | None = RecordingOutcome.COMPLETED,
    closure: ClosureCondition = ClosureCondition.CLEAN,
    close_statuses: dict[str, StreamCloseStatus] | None = None,
    fault: FaultHook | None = None,
) -> BuiltSession:
    """Write a deterministic synthetic session, optionally finalizing it."""
    specs = list(
        streams
        or [
            SyntheticStreamSpec(
                stream_id="synthetic.eeg", capture_level=RawCaptureLevel.TRANSPORT_PAYLOAD
            )
        ]
    )
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(
        Run(
            sealed_at=now_reading(),
            required_streams=list(required),
            optional_streams=list(optional),
        )
    )
    for spec in specs:
        writer.open_stream(build_descriptor(spec))
        source = SyntheticSource(spec, seed=seed)
        for _ in range(chunks):
            writer.commit_chunk(spec.stream_id, source.next_chunk(packets_per_chunk), fault=fault)
    for stream_id, status in (close_statuses or {}).items():
        writer.close_stream(stream_id, status)

    result = None
    if finalize_outcome is not None:
        result = finalize(writer, outcome=finalize_outcome, closure_condition=closure)
    return BuiltSession(data_root=data_root, allocated=allocated, writer=writer, result=result)
