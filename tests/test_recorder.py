"""CL-003 — the asynchronous multi-stream recorder with fan-in orchestration.

Every test here pins a property of the recovered CL-003 scope rather than a
detail of the implementation: asynchrony, fan-in onto exactly one writer thread,
scheduling from ``run.json.writer_config``, back-pressure that never drops, and
honest termination on every path.
"""

import threading
import time
from pathlib import Path

import pytest

from consciousness_lab.session.allocator import allocate_session
from consciousness_lab.session.finalizer import finalize
from consciousness_lab.session.model import (
    LifecycleState,
    RawCaptureLevel,
    RecordingOutcome,
    Run,
    StreamCloseStatus,
    WriterConfig,
)
from consciousness_lab.session.recorder import (
    PacketSink,
    Recorder,
    RecorderError,
    RecorderFatalError,
    SourcePacket,
)
from consciousness_lab.session.writer import SessionWriter
from consciousness_lab.storage.chunk_writer import ChunkWriter
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.reader import open_package
from consciousness_lab.storage.verifier import verify_package
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSource,
    SyntheticStreamSpec,
    as_source_packet,
)
from tests.conftest import now_reading

EEG = "synthetic.eeg"
ECG = "synthetic.ecg"
MARK = "synthetic.marker"


def spec_for(
    stream_id: str, level: RawCaptureLevel = RawCaptureLevel.SYNTHETIC
) -> SyntheticStreamSpec:
    return SyntheticStreamSpec(stream_id=stream_id, capture_level=level)


def open_writer(
    data_root: DataRoot,
    *,
    required: tuple[str, ...] = (EEG,),
    optional: tuple[str, ...] = (),
    config: WriterConfig | None = None,
) -> SessionWriter:
    """A session in RECORDING, ready for a recorder to drive."""
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(
        Run(
            sealed_at=now_reading(),
            required_streams=list(required),
            optional_streams=list(optional),
            writer_config=config or WriterConfig(),
        )
    )
    return writer


class FakeClock:
    """A monotonic clock a test drives, so scheduling is deterministic.

    It is a *scheduling* seam only. Every timestamp that reaches disk still
    comes from the writer's real clock reading.
    """

    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            return self._value

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._value += int(seconds * 1_000_000_000)


class AdvancingClock:
    """A monotonic clock that jumps a fixed amount on every reading.

    Threads make "advance the clock at the right moment" a race. Advancing on
    every reading removes the race entirely: the recorder always observes more
    than the configured window between any two readings, so the time-based rule
    fires deterministically however the threads interleaved.
    """

    def __init__(self, step_seconds: float) -> None:
        self._step = int(step_seconds * 1_000_000_000)
        self._value = 0
        self._lock = threading.Lock()

    def __call__(self) -> int:
        with self._lock:
            self._value += self._step
            return self._value


class ForeverSource:
    """Produces until told to stop. Exercises the operator stop path."""

    def __init__(self, spec: SyntheticStreamSpec) -> None:
        self.spec = spec
        self._source = SyntheticSource(spec, seed=1)
        self.produced = 0
        self.started = threading.Event()

    @property
    def descriptor(self):  # type: ignore[no-untyped-def]
        from consciousness_lab.synthetic.source import build_descriptor

        return build_descriptor(self.spec)

    def run(self, sink: PacketSink, stop: threading.Event) -> None:
        while not stop.is_set():
            sink.submit(as_source_packet(self._source.next_chunk(1)))
            self.produced += 1
            self.started.set()


# --------------------------------------------------------------------- basics


def test_records_several_streams_concurrently_into_one_verifiable_package(
    data_root: DataRoot,
) -> None:
    writer = open_writer(data_root, required=(EEG, ECG), optional=(MARK,))
    sources = [
        SyntheticStreamSource(spec_for(EEG), n_packets=9, seed=1),
        SyntheticStreamSource(spec_for(ECG), n_packets=7, seed=2),
        SyntheticStreamSource(spec_for(MARK), n_packets=5, seed=3),
    ]
    report = Recorder(writer, sources, monotonic_ns=FakeClock()).run()

    assert report.ok
    assert {s: r.packets for s, r in report.streams.items()} == {EEG: 9, ECG: 7, MARK: 5}
    assert all(r.close_status is StreamCloseStatus.CLEAN for r in report.streams.values())

    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)
    assert verify_package(writer.paths).is_completed

    package = open_package(writer.paths)
    assert len(list(package.stream(EEG).packets())) == 9
    assert len(list(package.stream(ECG).packets())) == 7
    assert len(list(package.stream(MARK).packets())) == 5


def test_the_recorder_never_finalizes_and_never_chooses_an_outcome(data_root: DataRoot) -> None:
    """Which outcome a session had is a human judgement (AGENTS.md §6)."""
    writer = open_writer(data_root)
    Recorder(writer, [SyntheticStreamSource(spec_for(EEG), n_packets=3)]).run()

    assert writer.lifecycle.state is LifecycleState.RECORDING
    assert not writer.paths.manifest.exists()
    assert not writer.paths.manifest_sha256.exists()


def test_transport_payload_streams_round_trip_through_the_recorder(data_root: DataRoot) -> None:
    writer = open_writer(data_root)
    source = SyntheticStreamSource(
        spec_for(EEG, RawCaptureLevel.TRANSPORT_PAYLOAD), n_packets=6, seed=4
    )
    Recorder(writer, [source]).run()
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)

    package = open_package(writer.paths)
    payloads = list(package.stream(EEG).payloads())
    assert [seq for seq, _ in payloads] == list(range(6))
    assert all(blob for _, blob in payloads)


# ------------------------------------------------------------------- fan-in


def test_exactly_one_thread_ever_touches_the_session_writer(data_root: DataRoot) -> None:
    """The writer's single-threaded promise is preserved, not deleted."""
    writer = open_writer(data_root, required=(EEG, ECG, MARK))
    seen: set[int] = set()
    lock = threading.Lock()

    for name in (
        "commit_chunk",
        "emit_event",
        "emit_clock_snapshot",
        "close_stream",
        "open_stream",
    ):
        original = getattr(writer, name)

        def wrapped(*args, _original=original, **kwargs):  # type: ignore[no-untyped-def]
            with lock:
                seen.add(threading.get_ident())
            return _original(*args, **kwargs)

        setattr(writer, name, wrapped)

    sources = [
        SyntheticStreamSource(spec_for(EEG), n_packets=20, seed=1),
        SyntheticStreamSource(spec_for(ECG), n_packets=20, seed=2),
        SyntheticStreamSource(spec_for(MARK), n_packets=20, seed=3),
    ]
    Recorder(writer, sources, queue_capacity=2).run()

    assert seen == {threading.get_ident()}


def test_the_recorder_never_restamps_a_host_arrival_time(data_root: DataRoot) -> None:
    """Host arrival is captured at the boundary; a queue must not shift it."""
    writer = open_writer(data_root)
    source = SyntheticStreamSource(spec_for(EEG), n_packets=12, seed=11)
    Recorder(writer, [source], queue_capacity=1).run()
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)

    expected = SyntheticSource(spec_for(EEG), seed=11)
    reference = [as_source_packet(expected.next_chunk(1)).packet for _ in range(12)]
    on_disk = list(open_package(writer.paths).stream(EEG).packets())

    for want, got in zip(reference, on_disk, strict=True):
        assert got["host_arrival_monotonic_ns"] == want["host_arrival_monotonic_ns"]
        assert got["host_arrival_utc_ns"] == want["host_arrival_utc_ns"]
        assert got["packet_seq"] == want["packet_seq"]


# ---------------------------------------------------------------- scheduling


def test_chunk_boundaries_follow_writer_config_chunk_max_rows(data_root: DataRoot) -> None:
    """The already-frozen writer_config is the scheduling contract, not a new one."""
    # 4 samples per packet, so a 4-row limit cuts after every single packet.
    writer = open_writer(data_root, config=WriterConfig(chunk_max_rows=4, chunk_max_seconds=10_000))
    source = SyntheticStreamSource(spec_for(EEG), n_packets=5, seed=1)
    report = Recorder(writer, [source], monotonic_ns=FakeClock()).run()

    assert report.streams[EEG].chunks == 5
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)
    assert verify_package(writer.paths).is_completed


def test_a_larger_chunk_max_rows_produces_fewer_larger_chunks(data_root: DataRoot) -> None:
    writer = open_writer(
        data_root, config=WriterConfig(chunk_max_rows=1_000, chunk_max_seconds=10_000)
    )
    source = SyntheticStreamSource(spec_for(EEG), n_packets=5, seed=1)
    report = Recorder(writer, [source], monotonic_ns=FakeClock()).run()

    # Nothing reached the limit, so the whole stream is one chunk cut at close.
    assert report.streams[EEG].chunks == 1


def test_chunk_boundaries_follow_writer_config_chunk_max_seconds(data_root: DataRoot) -> None:
    """A chunk that has aged past chunk_max_seconds is cut even if it is small."""
    writer = open_writer(
        data_root,
        config=WriterConfig(
            chunk_max_rows=1_000_000,
            chunk_max_seconds=30,
            clock_snapshot_interval_seconds=1_000_000,
        ),
    )
    source = SyntheticStreamSource(spec_for(EEG), n_packets=4, seed=1)
    report = Recorder(writer, [source], monotonic_ns=AdvancingClock(31)).run()

    # chunk_max_rows was never approached; every cut came from the clock.
    assert report.streams[EEG].chunks == 4
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)
    assert verify_package(writer.paths).is_completed


def test_periodic_clock_snapshots_follow_writer_config(data_root: DataRoot) -> None:
    """v1 spec §10.3 requires periodic snapshots. Nothing ran a loop until CL-003."""
    writer = open_writer(
        data_root,
        config=WriterConfig(
            chunk_max_rows=1_000_000,
            chunk_max_seconds=1_000_000,
            clock_snapshot_interval_seconds=60,
        ),
    )
    source = SyntheticStreamSource(spec_for(EEG), n_packets=4, seed=1)
    Recorder(writer, [source], monotonic_ns=AdvancingClock(61)).run()
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)

    events = open_package(writer.paths).events()
    snapshots = [e for e in events if e.event_name == "CLOCK_SNAPSHOT"]
    # One at RECORDING_START and one at FINALIZE_START are emitted by CL-002B
    # code; anything beyond them is the periodic one CL-003 added.
    assert len(snapshots) > 2


def test_no_periodic_snapshot_when_the_interval_has_not_elapsed(data_root: DataRoot) -> None:
    writer = open_writer(data_root, config=WriterConfig(clock_snapshot_interval_seconds=1_000_000))
    Recorder(
        writer, [SyntheticStreamSource(spec_for(EEG), n_packets=3)], monotonic_ns=FakeClock()
    ).run()
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)

    events = open_package(writer.paths).events()
    assert len([e for e in events if e.event_name == "CLOCK_SNAPSHOT"]) == 2


# ------------------------------------------------------------- back-pressure


def test_back_pressure_never_drops_a_packet(data_root: DataRoot) -> None:
    """A full queue makes producers wait. It never makes the recorder discard."""
    writer = open_writer(
        data_root, required=(EEG, ECG, MARK), config=WriterConfig(chunk_max_rows=8)
    )
    counts = {EEG: 40, ECG: 35, MARK: 25}
    sources = [
        SyntheticStreamSource(spec_for(stream_id), n_packets=n, seed=index)
        for index, (stream_id, n) in enumerate(counts.items())
    ]
    report = Recorder(writer, sources, queue_capacity=1, tick_seconds=0.001).run()

    assert {s: r.packets for s, r in report.streams.items()} == counts
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)

    package = open_package(writer.paths)
    for stream_id, expected in counts.items():
        rows = list(package.stream(stream_id).packets())
        assert len(rows) == expected
        # Nothing reordered and nothing skipped: the counter is the authority.
        assert [row["packet_seq"] for row in rows] == list(range(expected))


# -------------------------------------------------------- stream termination


def test_a_failing_source_closes_only_its_own_stream(data_root: DataRoot) -> None:
    """Losing one device must not destroy the recording of the others."""
    writer = open_writer(data_root, required=(EEG,), optional=(ECG,))
    sources = [
        SyntheticStreamSource(spec_for(EEG), n_packets=8, seed=1),
        SyntheticStreamSource(spec_for(ECG), n_packets=8, seed=2, fail_after=3),
    ]
    report = Recorder(writer, sources).run()

    assert not report.ok
    assert report.fatal is None
    assert report.streams[EEG].close_status is StreamCloseStatus.CLEAN
    assert report.streams[EEG].packets == 8
    assert report.streams[ECG].close_status is StreamCloseStatus.FAILED
    # The three packets that did arrive are real data and were committed.
    assert report.streams[ECG].packets == 3
    assert "synthetic source failure" in (report.streams[ECG].error or "")

    # EEG was the only required stream and it closed CLEAN, so the session is
    # still completable. That predicate belongs to the finalizer, not here.
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)
    assert verify_package(writer.paths).is_completed
    assert len(list(open_package(writer.paths).stream(ECG).packets())) == 3


def test_a_disconnecting_source_closes_disconnected_and_records_the_event(
    data_root: DataRoot,
) -> None:
    writer = open_writer(data_root, required=(EEG,))
    source = SyntheticStreamSource(spec_for(EEG), n_packets=8, seed=1, disconnect_after=4)
    report = Recorder(writer, [source]).run()

    assert report.streams[EEG].close_status is StreamCloseStatus.DISCONNECTED
    assert report.streams[EEG].packets == 4

    finalize(writer, outcome=RecordingOutcome.ABORTED, data_root=data_root)
    events = open_package(writer.paths).events()
    disconnects = [e for e in events if e.event_name == "DEVICE_DISCONNECTED"]
    assert len(disconnects) == 1
    assert disconnects[0].payload["stream_id"] == EEG
    assert "went away" in disconnects[0].payload["reason"]


def test_a_disconnect_is_never_inferred_from_an_ordinary_exception(data_root: DataRoot) -> None:
    """Only a source that declares a disconnect gets DISCONNECTED (AGENTS.md §7)."""
    writer = open_writer(data_root, required=(EEG,))
    source = SyntheticStreamSource(spec_for(EEG), n_packets=8, seed=1, fail_after=2)
    report = Recorder(writer, [source]).run()

    assert report.streams[EEG].close_status is StreamCloseStatus.FAILED
    assert report.streams[EEG].close_status is not StreamCloseStatus.DISCONNECTED


def test_a_required_stream_that_failed_cannot_be_sealed_completed(data_root: DataRoot) -> None:
    """Fail-closed survives the recorder: the completion predicate still rules."""
    writer = open_writer(data_root, required=(EEG,))
    source = SyntheticStreamSource(spec_for(EEG), n_packets=8, seed=1, fail_after=2)
    Recorder(writer, [source]).run()

    with pytest.raises(Exception, match="closed"):
        finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)


# --------------------------------------------------------- session termination


def test_request_stop_ends_every_stream_cleanly(data_root: DataRoot) -> None:
    writer = open_writer(data_root, required=(EEG, ECG))
    sources = [ForeverSource(spec_for(EEG)), ForeverSource(spec_for(ECG))]
    recorder = Recorder(writer, sources, tick_seconds=0.001)

    def stop_once_both_are_producing() -> None:
        for source in sources:
            source.started.wait(timeout=5.0)
        recorder.request_stop()

    stopper = threading.Thread(target=stop_once_both_are_producing)
    stopper.start()
    report = recorder.run()
    stopper.join()

    assert report.ok
    assert all(r.packets > 0 for r in report.streams.values())
    finalize(writer, outcome=RecordingOutcome.ABORTED, data_root=data_root)
    assert verify_package(writer.paths).is_sealed


def test_a_fatal_write_error_closes_the_session_technical_failure(
    data_root: DataRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure of the apparatus ends the session, not just one stream."""
    writer = open_writer(data_root, required=(EEG, ECG))

    def refuse(self: ChunkWriter, *args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ChunkWriter, "commit", refuse)
    sources = [
        SyntheticStreamSource(spec_for(EEG), n_packets=4, seed=1),
        SyntheticStreamSource(spec_for(ECG), n_packets=4, seed=2),
    ]
    with pytest.raises(RecorderFatalError) as raised:
        Recorder(writer, sources).run()

    assert "TECHNICAL_FAILURE" in str(raised.value)
    assert raised.value.report.fatal is not None
    assert writer.lifecycle.state is LifecycleState.CLOSED
    records = writer.paths.lifecycle.read_text(encoding="utf-8")
    assert "TECHNICAL_FAILURE" in records


def test_a_source_contract_violation_is_fatal_not_silently_dropped(data_root: DataRoot) -> None:
    """A stream declaring transport payloads must produce them; refusing is fatal."""
    writer = open_writer(data_root, required=(EEG,))

    class PayloadlessSource:
        def __init__(self) -> None:
            self.spec = spec_for(EEG, RawCaptureLevel.TRANSPORT_PAYLOAD)
            self._source = SyntheticSource(self.spec, seed=1)

        @property
        def descriptor(self):  # type: ignore[no-untyped-def]
            from consciousness_lab.synthetic.source import build_descriptor

            return build_descriptor(self.spec)

        def run(self, sink: PacketSink, stop: threading.Event) -> None:
            packet = as_source_packet(self._source.next_chunk(1))
            sink.submit(SourcePacket(packet=packet.packet, samples=packet.samples))

    with pytest.raises(RecorderFatalError):
        Recorder(writer, [PayloadlessSource()]).run()
    assert writer.lifecycle.state is LifecycleState.CLOSED


# ------------------------------------------------------------------- refusals


def test_the_recorder_refuses_a_session_whose_run_is_not_sealed(data_root: DataRoot) -> None:
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    with pytest.raises(RecorderError, match=r"run\.json"):
        Recorder(writer, [SyntheticStreamSource(spec_for(EEG), n_packets=1)])


def test_the_recorder_refuses_an_undeclared_stream(data_root: DataRoot) -> None:
    """An undeclared stream would make the package permanently unsealable."""
    writer = open_writer(data_root, required=(EEG,))
    with pytest.raises(RecorderError, match="declared in neither"):
        Recorder(writer, [SyntheticStreamSource(spec_for(ECG), n_packets=1)])
    assert not writer.paths.stream(ECG).descriptor.exists()


def test_the_recorder_refuses_two_sources_for_one_stream(data_root: DataRoot) -> None:
    writer = open_writer(data_root, required=(EEG,))
    sources = [
        SyntheticStreamSource(spec_for(EEG), n_packets=1),
        SyntheticStreamSource(spec_for(EEG), n_packets=1),
    ]
    with pytest.raises(RecorderError, match="same stream id"):
        Recorder(writer, sources)


def test_the_recorder_refuses_no_sources(data_root: DataRoot) -> None:
    writer = open_writer(data_root, required=(EEG,))
    with pytest.raises(RecorderError, match="at least one source"):
        Recorder(writer, [])


def test_a_recorder_runs_once(data_root: DataRoot) -> None:
    writer = open_writer(data_root, required=(EEG,))
    recorder = Recorder(writer, [SyntheticStreamSource(spec_for(EEG), n_packets=2)])
    recorder.run()
    with pytest.raises(RecorderError, match="runs once"):
        recorder.run()


# --------------------------------------------------------------- determinism


def test_identical_configuration_produces_identical_raw_bytes(tmp_path: Path) -> None:
    """Chunk boundaries must not depend on how the threads happened to interleave."""

    def record(root: Path) -> list[bytes]:
        data_root = DataRoot(root)
        writer = open_writer(
            data_root,
            required=(EEG, ECG),
            config=WriterConfig(chunk_max_rows=8, chunk_max_seconds=10_000),
        )
        sources = [
            SyntheticStreamSource(spec_for(EEG), n_packets=17, seed=5),
            SyntheticStreamSource(spec_for(ECG), n_packets=17, seed=6),
        ]
        Recorder(writer, sources, queue_capacity=1, monotonic_ns=FakeClock()).run()
        return [
            writer.paths.stream(stream_id).chunks_index.read_bytes() for stream_id in (EEG, ECG)
        ]

    assert record(tmp_path / "a") == record(tmp_path / "b")


def test_a_slow_writer_does_not_change_what_is_recorded(data_root: DataRoot) -> None:
    """Timing pressure changes when data lands, never what lands."""
    writer = open_writer(data_root, required=(EEG,), config=WriterConfig(chunk_max_rows=4))
    original = writer.commit_chunk

    def slow(*args: object, **kwargs: object) -> None:
        time.sleep(0.002)
        return original(*args, **kwargs)  # type: ignore[arg-type]

    writer.commit_chunk = slow  # type: ignore[method-assign]
    source = SyntheticStreamSource(spec_for(EEG), n_packets=21, seed=9)
    report = Recorder(writer, [source], queue_capacity=1, tick_seconds=0.001).run()

    assert report.streams[EEG].packets == 21
    assert report.streams[EEG].blocked_ns > 0
