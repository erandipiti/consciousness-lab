"""CL-003 — the asynchronous multi-stream recorder with fan-in orchestration.

Every test here pins a property of the recovered CL-003 scope rather than a
detail of the implementation: asynchrony, fan-in onto exactly one writer thread,
scheduling from ``run.json.writer_config``, back-pressure that never drops, and
honest termination on every path.
"""

import queue
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
    StreamDescriptor,
    WriterConfig,
)
from consciousness_lab.session.recorder import (
    PacketSink,
    Recorder,
    RecorderError,
    RecorderFatalError,
    RecorderReport,
    SourcePacket,
    _Handoff,
    _HandoffClosedError,
    _Sink,
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
    build_descriptor,
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


# ------------------------------------------------ CL-003-R1: the shutdown barrier


def handoff_packet(seq: int) -> SourcePacket:
    """The smallest well-formed packet; only its identity matters here."""
    return SourcePacket(packet={"packet_seq": seq})


def seqs_of(items: list[tuple[str, SourcePacket]]) -> list[int]:
    return [int(packet.packet["packet_seq"]) for _stream_id, packet in items]


class BlockedProducer:
    """A thread parked inside ``_Handoff.put`` on a full hand-off."""

    def __init__(self, handoff: _Handoff, item: tuple[str, SourcePacket]) -> None:
        self.handoff = handoff
        self.item = item
        self.entered = threading.Event()
        self.accepted: bool | None = None
        self.refused = False
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.entered.set()
        try:
            self.accepted = self.handoff.put(self.item, 5.0)
        except _HandoffClosedError:
            self.refused = True

    def start(self) -> None:
        self.thread.start()
        assert self.entered.wait(timeout=5.0)
        # Let the thread actually reach the wait inside put(). The assertions
        # below hold either way — parked in wait(), or about to re-enter put() —
        # because the barrier refuses in both cases.
        time.sleep(0.05)


def test_the_barrier_refuses_a_producer_that_is_already_blocked_in_submit() -> None:
    """CL-003-R1. The exact race: blocked in put(), teardown frees a slot.

    Under a plain queue this producer's packet is accepted *after* the final
    drain, and is then never written and never reported. The barrier makes
    acceptance and shutdown one atomic step, so the packet is refused instead.
    """
    handoff = _Handoff(1)
    assert handoff.put(("s", handoff_packet(0)), 1.0) is True

    producer = BlockedProducer(handoff, ("s", handoff_packet(1)))
    producer.start()

    remaining = handoff.close()
    producer.thread.join(timeout=5.0)

    assert not producer.thread.is_alive()
    assert producer.refused, "a producer blocked at teardown must be refused, not accepted"
    assert producer.accepted is None
    # Exactly what was accepted before the barrier, and nothing else.
    assert seqs_of(remaining) == [0]
    assert handoff.empty()
    assert handoff.closed


def test_the_barrier_is_idempotent_and_hands_queued_packets_over_once() -> None:
    handoff = _Handoff(4)
    for seq in range(3):
        assert handoff.put(("s", handoff_packet(seq)), 1.0) is True
    first = handoff.close()
    second = handoff.close()

    assert seqs_of(first) == [0, 1, 2]
    assert second == [], "a second close must not hand the same packets over again"
    with pytest.raises(_HandoffClosedError):
        handoff.put(("s", handoff_packet(99)), 1.0)


def test_nothing_can_be_consumed_from_a_closed_empty_handoff() -> None:
    handoff = _Handoff(2)
    handoff.close()
    with pytest.raises(queue.Empty):
        handoff.get(0.05)


class StopIgnoringSource:
    """A source that never notices ``stop``.

    Not a strawman: a vendor BLE or serial library can sit inside a blocking
    read that nothing in our process can interrupt. With a one-slot hand-off and
    a fan-in that has stopped consuming, this producer is *guaranteed* to be
    blocked in submission when teardown runs — which is exactly the window
    CL-003-R1 is about.

    ``budget`` is a safety net, not part of the scenario: with a one-slot
    hand-off and a fan-in that has stopped, this producer blocks within two
    submissions and cannot generate more. Exceeding the budget means the fan-in
    never stopped, so the test fails loudly instead of filling the disk.
    """

    def __init__(self, spec: SyntheticStreamSpec, budget: int = 5000) -> None:
        self.spec = spec
        self.budget = budget
        self._source = SyntheticSource(spec, seed=3)
        #: packet_seq values for which submit() RETURNED. The invariant under
        #: test is that every one of these is on disk.
        self.accepted: list[int] = []
        self.first = threading.Event()

    @property
    def descriptor(self) -> StreamDescriptor:
        return build_descriptor(self.spec)

    def run(self, sink: PacketSink, stop: threading.Event) -> None:
        while True:  # deliberately never consults `stop`
            if len(self.accepted) >= self.budget:
                raise AssertionError(
                    f"the fan-in was still consuming after {self.budget} packets; "
                    "request_stop() did not bound the recording"
                )
            packet = as_source_packet(self._source.next_chunk(1))
            sink.submit(packet)
            self.accepted.append(int(packet.packet["packet_seq"]))
            self.first.set()


def run_until_stopped(recorder: Recorder, source: StopIgnoringSource) -> RecorderReport:
    """Start the recorder and ask it to stop as soon as it is really recording."""

    def stop_once_recording() -> None:
        source.first.wait(timeout=5.0)
        recorder.request_stop()

    stopper = threading.Thread(target=stop_once_recording, daemon=True)
    stopper.start()
    try:
        return recorder.run()
    finally:
        stopper.join(timeout=5.0)


def test_no_packet_can_appear_after_the_final_drain(data_root: DataRoot) -> None:
    """CL-003-R1's invariant, end to end, through the join-timeout path.

    Every packet whose ``submit()`` returned is written to the package. A
    producer still trying to submit at teardown is refused — loudly, as a stream
    failure — and can never make a packet visible behind the final drain.
    """
    writer = open_writer(data_root, required=(EEG,), config=WriterConfig(chunk_max_rows=8))
    source = StopIgnoringSource(spec_for(EEG))
    recorder = Recorder(
        writer, [source], queue_capacity=1, tick_seconds=0.005, join_timeout_seconds=0.05
    )
    report = run_until_stopped(recorder, source)

    # The barrier is down and holds nothing: nothing can arrive behind us.
    assert recorder._handoff.closed
    assert recorder._handoff.empty()

    # The producer ended abnormally and the report says so. Whether it was
    # REFUSED at the barrier or simply never stopped depends on where its thread
    # happened to be when the join window expired — asserting one of those two
    # would be asserting a schedule, not a guarantee. The guarantee is that it
    # cannot end up looking clean, and that is what is checked.
    stream = report.streams[EEG]
    assert stream.refused >= 1 or stream.unstopped, "an abnormal end must be recorded"
    assert stream.close_status is StreamCloseStatus.FAILED
    assert not report.ok

    finalize(writer, outcome=RecordingOutcome.ABORTED, data_root=data_root)
    on_disk = [int(row["packet_seq"]) for row in open_package(writer.paths).stream(EEG).packets()]

    accepted = list(source.accepted)
    assert accepted, "the test proves nothing if the source never submitted anything"
    assert set(accepted) <= set(on_disk), (
        "a packet whose submit() returned was not written: "
        f"missing {sorted(set(accepted) - set(on_disk))}"
    )
    # And nothing was invented in the other direction either.
    assert set(on_disk) <= set(accepted)


def test_a_refused_packet_is_never_reported_as_a_clean_stream(data_root: DataRoot) -> None:
    """A stream that lost acquired data at the barrier must not read as CLEAN."""
    writer = open_writer(data_root, required=(EEG,))
    source = StopIgnoringSource(spec_for(EEG))
    recorder = Recorder(
        writer, [source], queue_capacity=1, tick_seconds=0.005, join_timeout_seconds=0.05
    )
    report = run_until_stopped(recorder, source)

    stream = report.streams[EEG]
    assert stream.close_status is not StreamCloseStatus.CLEAN
    # Refused-at-the-barrier and never-stopped are two ways for the same thing to
    # be true; which one happens is a matter of thread scheduling. The invariant
    # is that neither can be reported as clean. The refusal path itself is pinned
    # deterministically by test_the_barrier_refuses_a_producer_that_is_already_
    # blocked_in_submit, which drives _Handoff directly.
    assert stream.refused > 0 or stream.unstopped

    # Fail-closed survives: a required stream that did not close CLEAN cannot be
    # sealed COMPLETED, whatever the recorder thought.
    with pytest.raises(Exception, match="closed"):
        finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)


def test_request_stop_terminates_even_when_a_source_ignores_it(data_root: DataRoot) -> None:
    """An operator stop must always return. A wedged source cannot hang the host.

    Before CL-003-R1 the fan-in loop only ended when every stream had closed, so
    a source that never noticed ``stop`` made ``run()`` unreturnable.
    """
    writer = open_writer(data_root, required=(EEG,))
    source = StopIgnoringSource(spec_for(EEG))
    recorder = Recorder(
        writer, [source], queue_capacity=1, tick_seconds=0.005, join_timeout_seconds=0.05
    )
    started = time.monotonic()
    run_until_stopped(recorder, source)
    # The grace period plus the join window, with generous slack for a loaded
    # machine. The point is that it terminates at all, not the exact number.
    assert time.monotonic() - started < 10.0


# --------------------- CL-003-R3: chunk_max_rows is a packet-boundary bound


def chunk_sample_counts(paths: object, stream_id: str) -> list[int]:
    """Sample rows per committed chunk, read from the artifacts on disk."""
    import pyarrow as pa

    directory = paths.stream(stream_id).root / "samples"  # type: ignore[attr-defined]
    counts = []
    for artifact in sorted(directory.iterdir()):
        with pa.ipc.open_stream(artifact.open("rb")) as reader:
            counts.append(reader.read_all().num_rows)
    return counts


def test_no_chunk_exceeds_chunk_max_rows(data_root: DataRoot) -> None:
    """The bound is enforced BEFORE a packet is appended, not tested after.

    Appending first and checking afterwards produces a chunk that has already
    exceeded the limit, and nothing later can undo it without splitting the
    packet. With 3-sample packets and a limit of 10, a post-hoc check would let
    a chunk reach 12.
    """
    writer = open_writer(
        data_root, config=WriterConfig(chunk_max_rows=10, chunk_max_seconds=1_000_000)
    )
    spec = SyntheticStreamSpec(
        stream_id=EEG, capture_level=RawCaptureLevel.SYNTHETIC, samples_per_packet=3
    )
    Recorder(
        writer, [SyntheticStreamSource(spec, n_packets=9, seed=1)], monotonic_ns=FakeClock()
    ).run()
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)

    counts = chunk_sample_counts(writer.paths, EEG)
    assert counts, "the stream committed no chunks"
    assert max(counts) <= 10, f"a chunk exceeded chunk_max_rows: {counts}"
    assert sum(counts) == 27, counts
    assert verify_package(writer.paths).is_completed


def test_a_packet_bigger_than_the_limit_becomes_its_own_oversized_chunk(
    data_root: DataRoot,
) -> None:
    """The one documented exception (D37): packets are never split.

    The packet -> sample grouping is a preserved acquisition fact (v1 spec §19),
    so a packet that alone exceeds ``chunk_max_rows`` cannot be divided to
    satisfy a storage bound. It becomes a chunk of its own — and is committed
    immediately, so the overflow is exactly one packet and never drags others
    along with it.
    """
    writer = open_writer(
        data_root, config=WriterConfig(chunk_max_rows=4, chunk_max_seconds=1_000_000)
    )
    spec = SyntheticStreamSpec(
        stream_id=EEG, capture_level=RawCaptureLevel.SYNTHETIC, samples_per_packet=16
    )
    Recorder(
        writer, [SyntheticStreamSource(spec, n_packets=3, seed=1)], monotonic_ns=FakeClock()
    ).run()
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)

    counts = chunk_sample_counts(writer.paths, EEG)
    # One chunk per packet: each is oversized on its own and nothing is merged
    # into it. The alternative — splitting 16 sample rows across four chunks —
    # would break the packet grouping, which no approved contract permits.
    assert counts == [16, 16, 16], counts
    assert verify_package(writer.paths).is_completed
    package = open_package(writer.paths)
    assert len(list(package.stream(EEG).packets())) == 3
    assert len(list(package.stream(EEG).samples())) == 48


def test_an_oversized_packet_does_not_drag_ordinary_packets_with_it(
    data_root: DataRoot,
) -> None:
    """The exception is one packet wide, not one chunk wide."""
    writer = open_writer(
        data_root,
        required=(EEG, ECG),
        config=WriterConfig(chunk_max_rows=8, chunk_max_seconds=1_000_000),
    )
    big = SyntheticStreamSpec(
        stream_id=EEG, capture_level=RawCaptureLevel.SYNTHETIC, samples_per_packet=20
    )
    small = SyntheticStreamSpec(
        stream_id=ECG, capture_level=RawCaptureLevel.SYNTHETIC, samples_per_packet=2
    )
    Recorder(
        writer,
        [
            SyntheticStreamSource(big, n_packets=2, seed=1),
            SyntheticStreamSource(small, n_packets=8, seed=2),
        ],
        monotonic_ns=FakeClock(),
    ).run()
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)

    oversized = chunk_sample_counts(writer.paths, EEG)
    ordinary = chunk_sample_counts(writer.paths, ECG)
    assert oversized == [20, 20], oversized
    assert max(ordinary) <= 8, ordinary
    assert sum(ordinary) == 16, ordinary


def test_observation_rows_are_bounded_too(data_root: DataRoot) -> None:
    """`chunk_max_rows` applies to every raw table, not only samples."""
    writer = open_writer(
        data_root, config=WriterConfig(chunk_max_rows=4, chunk_max_seconds=1_000_000)
    )
    # 1 sample and 2 observations per packet: observations hit the bound first.
    spec = SyntheticStreamSpec(
        stream_id=EEG, capture_level=RawCaptureLevel.SYNTHETIC, samples_per_packet=1
    )
    Recorder(
        writer, [SyntheticStreamSource(spec, n_packets=7, seed=1)], monotonic_ns=FakeClock()
    ).run()
    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)

    import pyarrow as pa

    counts = []
    for artifact in sorted((writer.paths.stream(EEG).root / "observations").iterdir()):
        with pa.ipc.open_stream(artifact.open("rb")) as reader:
            counts.append(reader.read_all().num_rows)
    assert max(counts) <= 4, counts
    assert sum(counts) == 14, counts


def test_a_packet_lost_to_a_failed_pre_emptive_commit_is_counted(
    data_root: DataRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cutting to make room can fail, and the waiting packet must not vanish.

    Regression for a hole this ticket's own change opened: the pre-emptive
    commit returns early on a fatal write, and the packet it was making room for
    is held by nothing the accounting can walk — not the queue, not `pending`,
    not `in_flight`. The `accounted` identity caught it on the first run, which
    is the entire reason that identity is asserted rather than assumed.

    Shaped so the pre-emptive cut is what fires: one sample and one observation
    per packet against a limit of 2 packets, so the third arrival is the first
    thing that would exceed the bound.
    """
    writer = open_writer(
        data_root, config=WriterConfig(chunk_max_rows=2, chunk_max_seconds=1_000_000)
    )
    spec = SyntheticStreamSpec(
        stream_id=EEG,
        capture_level=RawCaptureLevel.SYNTHETIC,
        samples_per_packet=1,
        emit_device_time=False,
    )
    source = SyntheticStreamSource(spec, n_packets=0, seed=1)
    recorder = Recorder(writer, [source], queue_capacity=8, monotonic_ns=FakeClock())
    writer.open_stream(source.descriptor)
    sink = _Sink(recorder, EEG)
    recorder._sinks[EEG] = sink

    gen = SyntheticSource(spec, seed=1)
    for _ in range(3):
        sink.submit(as_source_packet(gen.next_chunk(1)))
    drive_one_packet(recorder)
    drive_one_packet(recorder)
    assert len(recorder._streams[EEG].pending.packets) == 2, "nothing was committed yet"

    def refuse(self: ChunkWriter, *args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ChunkWriter, "commit", refuse)
    drive_one_packet(recorder)  # the pre-emptive commit fires here, and fails

    report = recorder.report
    assert report.fatal is not None
    assert report.streams[EEG].submitted == 3
    assert report.streams[EEG].written == 0
    # 2 in the failed batch + the one it was making room for.
    assert report.streams[EEG].dropped == 3, "the waiting packet must be counted too"
    assert_accounted(report)


# -------------------------- CL-003-R1-R2: accounting for unwritten packets


def assert_accounted(report: RecorderReport) -> None:
    """The R1 contract: every submitted packet is durable or counted lost."""
    assert report.accounted, f"unaccounted packets: {report.unaccounted}"


def drive_one_packet(recorder: Recorder) -> None:
    """Pop one packet off the hand-off and accept it, exactly as _fan_in does."""
    stream_id, packet = recorder._handoff.get(1.0)
    recorder._accept(stream_id, packet)


def test_a_fatal_write_accounts_for_every_accepted_packet(
    data_root: DataRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CL-003-R1-R2. The failed batch and other streams' pending are not lost.

    An accepted packet lives in one of three unwritten places, and a fatal write
    must reach all three. Before this fix only the queued tail was counted: the
    batch whose commit failed was a local inside ``_commit`` and vanished with
    the frame, and packets accepted onto *other* streams sat in their pending
    accumulators unseen. A session could lose two whole chunks and report
    ``dropped == 0``.

    Driven a packet at a time from this thread — which is the fan-in thread's
    own job — so the placement of every packet at the moment of failure is
    exact rather than a matter of interleaving.
    """
    # Nothing auto-cuts: this test decides when a chunk is committed.
    writer = open_writer(
        data_root,
        required=(EEG, ECG),
        config=WriterConfig(chunk_max_rows=1_000_000, chunk_max_seconds=1_000_000),
    )
    source_a = SyntheticStreamSource(spec_for(EEG), n_packets=0, seed=1)
    source_b = SyntheticStreamSource(spec_for(ECG), n_packets=0, seed=2)
    recorder = Recorder(writer, [source_a, source_b], queue_capacity=16, monotonic_ns=FakeClock())
    for source in (source_a, source_b):
        writer.open_stream(source.descriptor)
    sinks = {sid: _Sink(recorder, sid) for sid in (EEG, ECG)}
    recorder._sinks.update(sinks)

    gen_a = SyntheticSource(spec_for(EEG), seed=1)
    gen_b = SyntheticSource(spec_for(ECG), seed=2)

    def submit_a() -> None:
        sinks[EEG].submit(as_source_packet(gen_a.next_chunk(1)))

    def submit_b() -> None:
        sinks[ECG].submit(as_source_packet(gen_b.next_chunk(1)))

    state_a = recorder._streams[EEG]
    state_b = recorder._streams[ECG]

    # 2 packets on A, committed durably.
    submit_a()
    submit_a()
    drive_one_packet(recorder)
    drive_one_packet(recorder)
    recorder._commit(state_a)
    assert state_a.written == 2 and state_a.chunks == 1

    # 2 more on A, accepted but not yet cut into a chunk.
    submit_a()
    submit_a()
    drive_one_packet(recorder)
    drive_one_packet(recorder)
    # 3 on B, accepted onto a DIFFERENT stream's pending accumulator.
    submit_b()
    submit_b()
    submit_b()
    for _ in range(3):
        drive_one_packet(recorder)
    # 1 more on A, still sitting in the hand-off queue.
    submit_a()

    assert len(state_a.pending.packets) == 2
    assert len(state_b.pending.packets) == 3
    assert not recorder._handoff.empty()

    def refuse(self: ChunkWriter, *args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ChunkWriter, "commit", refuse)
    recorder._commit(state_a)  # -> FatalWriteError -> _enter_fatal

    report = recorder.report
    assert report.fatal is not None

    # A: 5 submitted = 2 written + (2 in the failed batch + 1 still queued).
    assert report.streams[EEG].submitted == 5
    assert report.streams[EEG].written == 2
    assert report.streams[EEG].dropped == 3, (
        "the batch whose commit failed must be counted, not lost with the frame"
    )
    # B: never touched the failing write, but its accepted packets are just as
    # unwritten, and used to be reported as dropped == 0.
    assert report.streams[ECG].submitted == 3
    assert report.streams[ECG].written == 0
    assert report.streams[ECG].dropped == 3, "packets pending on another stream are unwritten too"
    assert_accounted(report)
    assert not report.ok


def test_unwritten_packets_are_counted_exactly_once(
    data_root: DataRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated fatal accounting must not inflate the count either.

    Counting and releasing happen together, so a second pass owns nothing. Both
    directions matter: an over-count is as wrong as an omission, and would make
    the report's accounting identity meaningless.
    """
    writer = open_writer(
        data_root,
        required=(EEG,),
        config=WriterConfig(chunk_max_rows=1_000_000, chunk_max_seconds=1_000_000),
    )
    source = SyntheticStreamSource(spec_for(EEG), n_packets=0, seed=1)
    recorder = Recorder(writer, [source], queue_capacity=8, monotonic_ns=FakeClock())
    writer.open_stream(source.descriptor)
    sink = _Sink(recorder, EEG)
    recorder._sinks[EEG] = sink

    gen = SyntheticSource(spec_for(EEG), seed=1)
    for _ in range(4):
        sink.submit(as_source_packet(gen.next_chunk(1)))
    for _ in range(2):
        drive_one_packet(recorder)

    def refuse(self: ChunkWriter, *args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ChunkWriter, "commit", refuse)
    recorder._commit(recorder._streams[EEG])
    once = recorder.report.streams[EEG].dropped
    assert once == 4  # 2 in the failed batch + 2 still queued

    for _ in range(3):
        recorder._account_unwritten()
    assert recorder.report.streams[EEG].dropped == once
    assert_accounted(recorder.report)

    recorder._teardown()
    assert recorder.report.streams[EEG].dropped == once
    assert_accounted(recorder.report)


def test_the_accounting_identity_holds_end_to_end_on_a_fatal_write(
    data_root: DataRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through run(), with real threads: whatever the interleaving, it balances."""
    writer = open_writer(data_root, required=(EEG, ECG), config=WriterConfig(chunk_max_rows=4))
    committed: list[str] = []
    original = ChunkWriter.commit

    def fail_after_the_first_chunk(self: ChunkWriter, *args: object, **kwargs: object) -> object:
        committed.append(self.descriptor.stream_id)
        if len(committed) > 1:
            raise OSError(28, "No space left on device")
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ChunkWriter, "commit", fail_after_the_first_chunk)
    sources = [
        SyntheticStreamSource(spec_for(EEG), n_packets=40, seed=1),
        SyntheticStreamSource(spec_for(ECG), n_packets=40, seed=2),
    ]
    with pytest.raises(RecorderFatalError) as raised:
        Recorder(sources=sources, writer=writer, queue_capacity=2, tick_seconds=0.005).run()

    report = raised.value.report
    assert report.fatal is not None
    assert_accounted(report)
    assert sum(r.dropped for r in report.streams.values()) > 0, (
        "a fatal write mid-session must leave unwritten packets to report"
    )
    assert writer.lifecycle.state is LifecycleState.CLOSED


def test_a_clean_session_writes_every_submitted_packet(data_root: DataRoot) -> None:
    """The same identity on the happy path: nothing submitted, nothing lost."""
    writer = open_writer(data_root, required=(EEG, ECG), config=WriterConfig(chunk_max_rows=5))
    sources = [
        SyntheticStreamSource(spec_for(EEG), n_packets=13, seed=1),
        SyntheticStreamSource(spec_for(ECG), n_packets=11, seed=2),
    ]
    report = Recorder(writer, sources, queue_capacity=2, tick_seconds=0.005).run()

    assert_accounted(report)
    assert report.streams[EEG].written == 13
    assert report.streams[ECG].written == 11
    assert all(r.dropped == 0 for r in report.streams.values())
    assert report.ok

    finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=data_root)
    package = open_package(writer.paths)
    for stream_id, expected in ((EEG, 13), (ECG, 11)):
        assert len(list(package.stream(stream_id).packets())) == expected
