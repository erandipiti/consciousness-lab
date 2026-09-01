"""CL-003 — the asynchronous multi-stream recorder with fan-in orchestration.

**Scope, recovered from the repository, not invented.** Four records in this
repository name CL-003 and, between them, define it by naming exactly what
CL-002B left out:

- ``CHANGELOG.md`` (CL-002B): "The asynchronous multi-device recorder is CL-003
  and is deliberately absent."
- ``synthetic/source.py``: "There is no asynchrony, no device fan-in, no
  scheduling and no back-pressure here."
- ``synthetic/__init__.py``: "The generic asynchronous multi-device recorder
  with fan-in orchestration is CL-003."
- ``session/writer.py``: "This is deliberately synchronous and single-threaded.
  The generic asynchronous multi-stream recorder with fan-in orchestration is
  CL-003, not this ticket."

So CL-003 is the four named properties, and nothing else:

1. **Asynchrony.** Several stream sources produce concurrently, each at its own
   pace, none waiting on another.
2. **Fan-in.** Everything they produce is funnelled to exactly **one** thread,
   which is the only thread that ever touches :class:`SessionWriter`. The
   writer's promise to be synchronous and single-threaded is *preserved*, not
   deleted: all the concurrency lives above it.
3. **Scheduling.** Chunk boundaries and periodic clock snapshots follow
   ``run.json``'s already-approved ``writer_config`` — ``chunk_max_rows`` /
   ``chunk_max_seconds`` (v1 spec §12.2, "whichever comes first") and
   ``clock_snapshot_interval_seconds`` (v1 spec §10.3, which requires periodic
   snapshots and which nothing implemented until now). No new policy value is
   invented here: the numbers come from a frozen schema field that is itself
   labelled writer configuration and explicitly not a scientific parameter.

   **Chunks are cut on packet boundaries** (D37). No chunk exceeds
   ``chunk_max_rows`` in any raw table, with exactly one exception: a packet
   whose own rows exceed the limit becomes an oversized chunk of its own,
   because a packet is never split. Chunk boundaries carry no analytical
   meaning (v1 spec §10.2), so where they fall is free; the packet→sample
   grouping does carry meaning (§19), so it is not.
4. **Back-pressure.** The hand-off is bounded. A source that outruns the writer
   **blocks**; nothing is ever silently dropped. Where the host could not keep
   up, the device's own counter is what reveals it (``TIMING.md``: counters
   govern loss), and the recorder never fabricates a substitute.

**The shutdown barrier (CL-003-R1).** Back-pressure is only honest if shutdown
cannot lose an in-flight packet, so acceptance and the shutdown decision are one
atomic step in :class:`_Handoff`, not two operations against a queue:

    once teardown has decided that no more packets will be consumed, no producer
    can subsequently make a new packet visible to the fan-in; and every packet
    that was successfully submitted before that barrier is either written to the
    package or reported as an explicit failure — never silently abandoned.

A producer waiting to submit when the barrier drops is *refused*, loudly: its
packet was never accepted, and its stream closes ``FAILED`` saying so.

**Ownership (CL-003-R1).** Keeping that promise on a fatal write needs one more
thing: at every instant, an accepted packet must be in exactly one place, and
every place must be reachable from the fatal path. There are four:

    _Handoff queue  ->  stream.pending  ->  stream.in_flight  ->  written

``in_flight`` exists precisely because the third place used to be a local
variable inside ``_commit``: the batch whose write just failed was invisible to
the accounting, so a session could lose a whole chunk and still report
``dropped == 0``. Entering a fatal path now walks all three unwritten places,
counts them, and clears them — so the count is exactly once — and the identity

    submitted == written + dropped

holds on every path. It is *checked* rather than used to derive anything: a
count inferred by subtraction would agree with itself no matter which packets
actually reached the disk.

**Deliberately out of scope**, because no other repository record puts them in
CL-003: device adapters, BLE or serial transport, reconstructed timing, cross-
device alignment, any analysis, and any hardware claim. The recorder is defined
against an abstract source and exercised by the existing synthetic one.

**No scientific decision is made here.** The recorder has no concept of session
duration, minimum packet count, quality, epoch or Focus. It never chooses a
``RecordingOutcome`` and never calls ``finalize()``: what a session *was*
scientifically is a human judgement, and the recorder only reports what it
observed (``AGENTS.md`` §6).

**Failure semantics — the one distinction that matters.** A failure of the
recording *apparatus* ends the session; a failure of one *device* ends one
stream:

- A chunk commit that fails for any reason closes the whole session
  ``CLEAN / TECHNICAL_FAILURE`` through the path CL-002B already defined. A
  package whose raw directory holds artifacts no commit record names can never
  be sealed, so continuing to record into it would be dishonest.
- A source that stops abnormally closes **its** stream — ``DISCONNECTED`` if
  the source itself declares a disconnect, ``FAILED`` otherwise — and the other
  streams keep recording. Losing one device must not destroy the recording of
  the others. Whether the session is still completable is then decided by the
  finalizer's existing predicate, not here.

Packets already received from a stream that then fails are committed before it
closes. Data that arrived is real, whatever happened next.
"""

import collections
import contextlib
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from consciousness_lab.session.model import (
    StreamCloseStatus,
    StreamDescriptor,
    WriterConfig,
)
from consciousness_lab.session.writer import FatalWriteError, SessionWriter
from consciousness_lab.storage.chunk_writer import PendingChunk

#: How long a blocked producer waits before re-checking whether the recorder is
#: still consuming, and how often the fan-in loop wakes to apply time-based
#: scheduling when no packet arrived. Engineering constant only (AGENTS.md §6):
#: it bounds teardown latency and the granularity of ``chunk_max_seconds``, and
#: it carries no scientific claim. Chosen small enough that shutdown feels
#: immediate to an operator and large enough not to spin a core on an idle
#: session.
DEFAULT_TICK_SECONDS = 0.05

#: Depth of the fan-in hand-off. Engineering constant only: it bounds how much
#: acquired data sits in RAM before a producer is made to wait, and nothing
#: else. It is not a sample count, not a buffer the data model knows about, and
#: not a threshold any scientific claim rests on.
DEFAULT_QUEUE_CAPACITY = 256

#: How long teardown waits for a source thread to notice the stop signal before
#: giving up on it and reporting it as unstopped. Engineering constant: it
#: exists so a wedged device library cannot hang the operator forever.
DEFAULT_JOIN_TIMEOUT_SECONDS = 5.0


class RecorderError(RuntimeError):
    """The recorder was asked to do something it must refuse."""


class RecorderStoppingError(RuntimeError):
    """Raised into a producer that is blocked while the recorder tears down.

    A source **must not** swallow this: it means the fan-in loop is no longer
    consuming, so anything the source produces from here on cannot be written.
    Let it propagate out of ``run()``.
    """


class SourceDisconnectedError(RuntimeError):
    """A source declares that its device went away.

    Raising this is the *only* way a stream is closed ``DISCONNECTED``. The
    recorder never infers a disconnect from an ordinary exception, because
    "the device went away" and "our code raised" are different facts and
    guessing between them would put an unverified claim into immutable data
    (``AGENTS.md`` §7).
    """


@dataclass(frozen=True)
class SourcePacket:
    """One packet exactly as the acquisition boundary observed it.

    The rows are the on-disk row shapes of the ``packets`` / ``samples`` /
    ``observations`` tables. **The recorder never edits them** — in particular
    it never writes, corrects or re-stamps a host arrival time. Host arrival
    time is "the moment the packet surfaced to our process" (``TIMING.md``), so
    only the source is in a position to capture it; a time stamped after the
    packet has waited in a queue would silently be a different quantity
    (``AGENTS.md`` §4).
    """

    packet: dict[str, Any]
    samples: list[dict[str, Any]] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    #: Transport bytes, for ``raw_capture_level = transport_payload`` streams
    #: only. The chunk writer refuses a mismatch in either direction.
    payload: bytes | None = None


class PacketSink(Protocol):
    """Where a source hands a packet over. Bound to exactly one stream."""

    def submit(self, packet: SourcePacket) -> None:
        """Hand one packet to the recorder, blocking while the queue is full."""


class StreamSource(Protocol):
    """One live source of packets for one stream.

    Structural on purpose: an acquisition adapter satisfies this by shape and
    never has to import from the session layer, so the downward-only dependency
    rule in ``ARCHITECTURE.md`` survives contact with a real device adapter.
    """

    @property
    def descriptor(self) -> StreamDescriptor:
        """The stream this source produces. Sealed before recording starts."""

    def run(self, sink: PacketSink, stop: threading.Event) -> None:
        """Produce packets until exhausted or until ``stop`` is set.

        Runs on its own thread. Return normally to close the stream ``CLEAN``;
        raise :class:`SourceDisconnectedError` to close it ``DISCONNECTED``; any
        other exception closes it ``FAILED``.
        """


@dataclass(frozen=True)
class StreamReport:
    """What the recorder observed for one stream. In memory only.

    Nothing here is persisted. These are host-side engineering observations,
    not acquisition facts, and v2 has exactly one authority for every fact it
    does persist — a new persisted summary would need an approved decision
    (D29), which CL-003 does not have and does not need.
    """

    stream_id: str
    #: Packets accepted off the hand-off by the fan-in thread.
    packets: int
    #: Packets whose ``submit()`` returned. Differs from ``packets`` only when
    #: some were still queued when recording ended.
    submitted: int
    #: Packets that reached disk inside a committed chunk.
    written: int
    samples: int
    observations: int
    chunks: int
    close_status: StreamCloseStatus | None
    #: Why the stream ended abnormally, verbatim. ``None`` if it ended normally.
    error: str | None
    #: Nanoseconds this stream's producer spent blocked by back-pressure. A
    #: non-zero value means the host could not keep up; what that cost is
    #: answered by the device counter, never by this number.
    blocked_ns: int
    #: True if the source thread never returned after being asked to stop.
    unstopped: bool
    #: Packets refused at the shutdown barrier. They were never accepted, so no
    #: acquired data was lost silently — the stream closes FAILED saying so.
    refused: int = 0
    #: Packets accepted but never made durable, because a fatal error ended the
    #: session first: the queued tail, the in-flight batch whose write failed,
    #: and every stream's pending accumulator. Counted exactly once, never
    #: silent — ``submitted == written + dropped`` always.
    dropped: int = 0


@dataclass(frozen=True)
class RecorderReport:
    """What the recorder observed for the whole session. In memory only.

    When ``fatal`` is set the per-stream ``close_status`` may be ``None``: the
    writer's fatal path closed every stream itself, and ``stream_close.json``
    on disk is the authority for how each one ended (D33), not this report.
    """

    streams: dict[str, StreamReport]
    #: The diagnosed fatal error that closed the session, if one did.
    fatal: str | None = None

    @property
    def accounted(self) -> bool:
        """Is every submitted packet either durable or explicitly counted lost?

        The R1 contract as one boolean. False means a packet was acquired and
        the recorder cannot say what became of it, which is the failure this
        whole ticket exists to make impossible.
        """
        return all(
            report.submitted == report.written + report.dropped for report in self.streams.values()
        )

    @property
    def unaccounted(self) -> dict[str, int]:
        """Per stream, submitted minus (written + dropped). Empty when sound."""
        return {
            stream_id: report.submitted - report.written - report.dropped
            for stream_id, report in self.streams.items()
            if report.submitted != report.written + report.dropped
        }

    @property
    def ok(self) -> bool:
        """True when nothing failed. Says nothing about the scientific outcome."""
        return self.fatal is None and all(
            report.close_status is StreamCloseStatus.CLEAN
            and report.refused == 0
            and report.dropped == 0
            for report in self.streams.values()
        )


class RecorderFatalError(RuntimeError):
    """A diagnosed fatal error ended the session. Carries the report."""

    def __init__(self, message: str, report: RecorderReport) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class _StreamState:
    """The fan-in thread's private accumulator for one stream."""

    stream_id: str
    pending: PendingChunk = field(default_factory=PendingChunk)
    #: The batch handed to ``commit_chunk`` and not yet known to be durable.
    #: Its own slot, not a local in ``_commit``: a local would vanish with the
    #: stack frame of a failed write, taking a whole chunk of acquired packets
    #: out of the accounting with it.
    in_flight: PendingChunk | None = None
    #: Host monotonic time the current chunk began accumulating, for
    #: ``chunk_max_seconds``. A durability cadence measured on the host clock —
    #: never device time, which would let a device's timebase decide how our
    #: storage is cut.
    started_ns: int | None = None
    packets: int = 0
    samples: int = 0
    observations: int = 0
    chunks: int = 0
    #: Packets that reached disk inside a committed chunk.
    written: int = 0
    consumed: int = 0
    #: Packets that were accepted but could not be written, because a diagnosed
    #: fatal error ended the session first. Never silent: it reaches the report.
    dropped: int = 0
    closed: bool = False
    close_status: StreamCloseStatus | None = None
    error: str | None = None
    unstopped: bool = False


class _HandoffClosedError(RuntimeError):
    """The barrier is down. This packet was never accepted by the fan-in."""


class _Handoff:
    """The bounded fan-in hand-off and its shutdown barrier, under one lock.

    **Why not a ``queue.Queue`` (CL-003-R1).** With a plain queue, "is the
    recorder still consuming?" and "enqueue this packet" are two separate
    operations, and teardown can land between them: a producer checks the flag,
    blocks inside ``put``, and its packet is accepted *after* teardown has done
    its final drain. That packet is acquired data that is never written and
    never reported — precisely the silent drop back-pressure exists to prevent.
    Re-checking the flag after ``put`` does not fix it either; it only moves the
    window, and the packet is by then already visible to a consumer that has
    stopped looking.

    Here the two are one critical section:

    * ``put`` decides acceptance and enqueues under the same lock hold, so a
      packet is either accepted before the barrier or refused, never both;
    * ``close`` sets the barrier **and** takes everything still queued in that
      same hold, so the list it returns is provably every packet that was ever
      accepted and not yet consumed — no window on either side;
    * a producer waiting inside ``put`` when ``close`` runs is woken and
      refused with :class:`_HandoffClosedError`, which the sink turns into a
      loud stream failure rather than a lost packet.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise RecorderError("the fan-in hand-off needs a capacity of at least 1")
        self._cv = threading.Condition()
        self._items: collections.deque[tuple[str, SourcePacket]] = collections.deque()
        self._capacity = capacity
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._cv:
            return self._closed

    def put(self, item: tuple[str, SourcePacket], timeout: float) -> bool:
        """Accept one packet. True if accepted, False if the wait timed out.

        Raises :class:`_HandoffClosedError` when the barrier is down. The packet
        was **not** accepted, and the caller must treat it as never submitted.
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            while True:
                if self._closed:
                    raise _HandoffClosedError("the recorder has stopped consuming packets")
                if len(self._items) < self._capacity:
                    self._items.append(item)
                    self._cv.notify_all()
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(remaining)

    def get(self, timeout: float) -> tuple[str, SourcePacket]:
        """One packet, or ``queue.Empty`` if none arrived within ``timeout``."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while not self._items:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._closed:
                    raise queue.Empty
                self._cv.wait(remaining)
            item = self._items.popleft()
            self._cv.notify_all()
            return item

    def empty(self) -> bool:
        with self._cv:
            return not self._items

    def close(self) -> list[tuple[str, SourcePacket]]:
        """Lower the barrier and take what is still queued, atomically.

        Idempotent: a second call returns an empty list, because the first one
        already took everything and no producer can have added more.
        """
        with self._cv:
            self._closed = True
            rest = list(self._items)
            self._items.clear()
            self._cv.notify_all()
            return rest


class _Sink:
    """The bounded hand-off one producer writes into.

    ``submit`` blocks while the hand-off is full — that is the back-pressure.
    It fails only when the barrier is down, and that failure is visible: the
    packet was never accepted, and the stream will close ``FAILED`` saying so.
    """

    def __init__(self, recorder: "Recorder", stream_id: str) -> None:
        self._recorder = recorder
        self._stream_id = stream_id
        self.submitted = 0
        self.blocked_ns = 0
        #: Packets the barrier refused. Non-zero means this stream did not end
        #: cleanly, whatever else it did.
        self.refused = 0

    def submit(self, packet: SourcePacket) -> None:
        recorder = self._recorder
        blocked_from: int | None = None
        while True:
            try:
                accepted = recorder._handoff.put((self._stream_id, packet), recorder._tick_seconds)
            except _HandoffClosedError as exc:
                self.refused += 1
                raise RecorderStoppingError(
                    f"{self._stream_id}: the recorder stopped consuming before this packet "
                    "was accepted; it was refused, not dropped"
                ) from exc
            if accepted:
                if blocked_from is not None:
                    self.blocked_ns += recorder._monotonic_ns() - blocked_from
                # Incremented only after the packet is in the hand-off, and
                # always before the producer thread records that it finished.
                # The fan-in loop therefore never sees "finished" while a packet
                # it has not counted is still in flight.
                self.submitted += 1
                return
            if blocked_from is None:
                blocked_from = recorder._monotonic_ns()


class Recorder:
    """Runs several stream sources concurrently into one session package.

    ``run()`` blocks in the calling thread, and **the calling thread is the
    single writer thread**: it is the only one that ever calls ``SessionWriter``.
    Sources run on their own threads and can only put packets on a queue.
    """

    def __init__(
        self,
        writer: SessionWriter,
        sources: Sequence[StreamSource],
        *,
        queue_capacity: int = DEFAULT_QUEUE_CAPACITY,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        join_timeout_seconds: float = DEFAULT_JOIN_TIMEOUT_SECONDS,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        """``monotonic_ns`` is a scheduling seam, not a data seam.

        It decides *when* the recorder cuts a chunk or emits a snapshot. Every
        time value that reaches disk still comes from the writer's own clock
        reading, so a test may control the schedule without ever controlling a
        recorded timestamp.
        """
        if writer.run is None:
            raise RecorderError(
                "the recorder requires a session whose run.json is already sealed; "
                "writer_config is the scheduling contract and it lives there"
            )
        if not sources:
            raise RecorderError("the recorder requires at least one source")
        ids = [source.descriptor.stream_id for source in sources]
        duplicates = sorted({name for name in ids if ids.count(name) > 1})
        if duplicates:
            raise RecorderError(f"two sources declare the same stream id: {duplicates}")
        # Refused here rather than at finalization: an undeclared stream makes
        # the package structurally unsealable (finalizer._assert_sealable), and
        # by then its descriptor is already sealed and immutable.
        declared = set(writer.run.required_streams) | set(writer.run.optional_streams)
        undeclared = sorted(set(ids) - declared)
        if undeclared:
            raise RecorderError(
                f"stream(s) {undeclared} are declared in neither run.required_streams nor "
                "run.optional_streams; a package holding them could never be sealed"
            )

        self._writer = writer
        self._config: WriterConfig = writer.run.writer_config
        self._sources = list(sources)
        self._tick_seconds = tick_seconds
        self._join_timeout_seconds = join_timeout_seconds
        self._monotonic_ns = monotonic_ns

        self._handoff = _Handoff(queue_capacity)
        #: Set to ask sources to finish. Cooperative: a source decides when it
        #: can safely stop talking to its device.
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._finished: dict[str, tuple[str, str]] = {}
        self._sinks: dict[str, _Sink] = {}
        #: (thread, stream_id) pairs. Paired at creation rather than matched by
        #: position against ``_sources`` later: a positional zip is a coupling
        #: between two lists that nothing enforces, and it made an internal
        #: helper explode when the lists legitimately differed in length.
        self._threads: list[tuple[threading.Thread, str]] = []
        self._streams: dict[str, _StreamState] = {
            stream_id: _StreamState(stream_id=stream_id) for stream_id in ids
        }
        self._fatal: str | None = None
        self._last_snapshot_ns: int | None = None
        self._started = False

    # ---------------------------------------------------------------- public

    def request_stop(self) -> None:
        """Ask every source to finish. Safe to call from any thread."""
        self._stop.set()

    @property
    def report(self) -> RecorderReport:
        """What has been observed so far. Meaningful after ``run()`` returns."""
        streams = {}
        for stream_id, state in self._streams.items():
            sink = self._sinks.get(stream_id)
            streams[stream_id] = StreamReport(
                stream_id=stream_id,
                packets=state.packets,
                submitted=sink.submitted if sink is not None else 0,
                written=state.written,
                samples=state.samples,
                observations=state.observations,
                chunks=state.chunks,
                close_status=state.close_status,
                error=state.error,
                blocked_ns=sink.blocked_ns if sink is not None else 0,
                unstopped=state.unstopped,
                refused=sink.refused if sink is not None else 0,
                dropped=state.dropped,
            )
        return RecorderReport(streams=streams, fatal=self._fatal)

    def run(self) -> RecorderReport:
        """Record until every source is exhausted or ``request_stop()`` is called.

        Returns what was observed. Raises :class:`RecorderFatalError` if a
        diagnosed fatal error closed the session — the package is then already
        ``CLOSED / TECHNICAL_FAILURE`` and must not be finalized.
        """
        if self._started:
            raise RecorderError("a recorder runs once; construct another for another session")
        self._started = True

        # Descriptors are sealed from this thread, before any producer exists,
        # so stream creation is never concurrent with stream writing.
        for source in self._sources:
            self._writer.open_stream(source.descriptor)
        self._last_snapshot_ns = self._monotonic_ns()

        for source in self._sources:
            sink = _Sink(self, source.descriptor.stream_id)
            self._sinks[source.descriptor.stream_id] = sink
            thread = threading.Thread(
                target=self._produce,
                args=(source, sink),
                name=f"cl003-source-{source.descriptor.stream_id}",
                # Daemon so a wedged device library cannot keep the interpreter
                # alive after the operator has been told the session is over.
                daemon=True,
            )
            self._threads.append((thread, source.descriptor.stream_id))
            thread.start()

        try:
            self._fan_in()
        finally:
            self._teardown()

        report = self.report
        if self._fatal is not None:
            raise RecorderFatalError(self._fatal, report)
        return report

    # ----------------------------------------------------------- fan-in loop

    def _fan_in(self) -> None:
        """The single writer thread. Nothing else calls ``SessionWriter``."""
        stop_deadline: float | None = None
        while self._fatal is None:
            # A source is free to ignore ``stop`` — a vendor SDK can sit inside
            # a blocking read that nothing can interrupt. The operator asked to
            # stop, so the fan-in stops on a grace period instead of waiting
            # forever for a producer that may never return; teardown then closes
            # the barrier, and any packet still in flight is refused rather than
            # accepted into a package nobody is writing.
            #
            # Real wall time on purpose: this is a safety timer, not scheduling,
            # so an injected scheduling clock must not be able to defer it.
            if self._stop.is_set():
                if stop_deadline is None:
                    stop_deadline = time.monotonic() + self._join_timeout_seconds
                elif time.monotonic() >= stop_deadline:
                    return
            try:
                stream_id, packet = self._handoff.get(self._tick_seconds)
            except queue.Empty:
                self._on_tick()
                if self._all_closed():
                    return
                continue
            self._accept(stream_id, packet)
            self._on_tick()

    def _on_tick(self) -> None:
        """Everything the schedule owes that no packet arrival triggers."""
        if self._fatal is not None:
            return
        for state in self._streams.values():
            # The same rule arrival uses. A chunk that has aged past
            # chunk_max_seconds must be cut even when no packet arrives to
            # trigger the check, and two copies of that rule would drift. The
            # row bound needs no tick: it can only be approached by an arrival.
            if not state.closed and self._aged_out(state):
                self._commit(state)
                if self._fatal is not None:
                    return
        self._maybe_snapshot(self._monotonic_ns())
        if self._fatal is None:
            self._close_finished()

    def _maybe_snapshot(self, now: int) -> None:
        """Periodic ``CLOCK_SNAPSHOT`` (v1 spec §10.3).

        ``start_recording`` emits one and ``finalize`` emits one; the periodic
        one in between is what makes a UTC step visible as a discontinuity
        rather than as silent corruption, and it had no implementation before
        CL-003 because nothing ran a loop.
        """
        interval_ns = self._config.clock_snapshot_interval_seconds * 1_000_000_000
        if interval_ns <= 0 or self._last_snapshot_ns is None:
            return
        if now - self._last_snapshot_ns < interval_ns:
            return
        self._last_snapshot_ns = now
        try:
            self._writer.emit_clock_snapshot()
        except OSError as exc:
            self._fail(f"clock snapshot failed: {type(exc).__name__}: {exc}")

    def _accept(self, stream_id: str, packet: SourcePacket) -> None:
        state = self._streams[stream_id]
        state.consumed += 1
        if state.closed:
            # Unreachable by contract: a stream is closed only once its
            # producer has finished and every packet it submitted has been
            # consumed. If it happens anyway, data was acquired that cannot be
            # written, and that must fail loudly rather than vanish. This packet
            # is held by nothing but this frame, so it is counted here — the one
            # unwritten packet _account_unwritten cannot reach.
            state.packets += 1
            state.dropped += 1
            self._fail(
                f"packet arrived for {stream_id} after it was closed; acquired data "
                "cannot be written and must not be discarded silently",
                stream_id=stream_id,
            )
            return
        # Cut BEFORE appending, so the open chunk never exceeds the bound in the
        # first place. The previous order appended and then tested, which let a
        # packet carry a chunk past the limit with nothing able to undo it.
        if self._would_exceed_rows(state, packet):
            self._commit(state)
            if self._fatal is not None:
                # The commit that was making room failed. This packet has been
                # accepted off the hand-off but is held by nothing the fatal
                # accounting can walk — not the queue, not pending, not
                # in_flight — so it is counted here, exactly like the
                # closed-stream branch above. Returning without this is how a
                # packet goes missing, which is the defect D39 exists to
                # prevent; the accounting identity caught it immediately.
                state.packets += 1
                state.dropped += 1
                return
        if state.started_ns is None:
            state.started_ns = self._monotonic_ns()
        state.pending.packets.append(packet.packet)
        state.pending.samples.extend(packet.samples)
        state.pending.observations.extend(packet.observations)
        if packet.payload is not None:
            state.pending.payloads.append((int(packet.packet["packet_seq"]), packet.payload))
        state.packets += 1
        state.samples += len(packet.samples)
        state.observations += len(packet.observations)
        if self._oversized_alone(state) or self._aged_out(state):
            self._commit(state)

    def _would_exceed_rows(self, state: _StreamState, packet: SourcePacket) -> bool:
        """Would adding this packet push any raw table past ``chunk_max_rows``?

        Checked **before** the packet is appended, which is the whole point.
        Appending first and testing afterwards produces a chunk that has already
        exceeded the bound, and no later check can undo that without splitting
        the packet.

        Returns False on an empty accumulator: there is nothing to cut, and a
        packet is never split. See :meth:`_oversized_alone` for what happens to a
        packet that is bigger than the limit all by itself.
        """
        limit = self._config.chunk_max_rows
        if limit <= 0 or not state.pending.packets:
            return False
        return (
            len(state.pending.packets) + 1 > limit
            or len(state.pending.samples) + len(packet.samples) > limit
            or len(state.pending.observations) + len(packet.observations) > limit
        )

    def _oversized_alone(self, state: _StreamState) -> bool:
        """Is this chunk a single packet that alone exceeds ``chunk_max_rows``?

        The documented exception (D37). The packet→sample grouping is a
        preserved acquisition fact — the v1 spec §19 lists dropping it among the
        things that would make future timing analysis impossible — so a packet is
        never split across chunks to satisfy a storage bound. Such a packet
        becomes a chunk of its own and is committed immediately, so the overflow
        is exactly one packet and never drags others with it.
        """
        limit = self._config.chunk_max_rows
        if limit <= 0 or len(state.pending.packets) != 1:
            return False
        return len(state.pending.samples) > limit or len(state.pending.observations) > limit

    def _aged_out(self, state: _StreamState) -> bool:
        """Has the open chunk lived longer than ``chunk_max_seconds``?

        Host wall time, never device time: this is a durability cadence, not a
        property of the data.
        """
        if state.started_ns is None or not state.pending.packets:
            return False
        seconds_ns = self._config.chunk_max_seconds * 1_000_000_000
        return self._monotonic_ns() - state.started_ns >= seconds_ns

    def _commit(self, state: _StreamState) -> None:
        """Seal the accumulated chunk. Empty accumulators are not chunks."""
        if not state.pending.packets:
            state.started_ns = None
            return
        # Ownership moves pending -> in_flight here, and stays there until the
        # write is KNOWN to have succeeded. Swapping out before the write is
        # what stops a failed commit being retried into a second chunk carrying
        # the same packets; holding it in the stream rather than in a local is
        # what keeps a failed batch visible to the fatal accounting (D39).
        state.in_flight = state.pending
        state.pending = PendingChunk()
        state.started_ns = None
        try:
            self._writer.commit_chunk(state.stream_id, state.in_flight)
        except FatalWriteError as exc:
            # commit_chunk already closed the session CLEAN / TECHNICAL_FAILURE
            # and durably closed every stream. Nothing more may be written.
            # in_flight is left set on purpose: _account_unwritten owns it now.
            self._enter_fatal(str(exc))
            return
        # Any commit failure at all is fatal; see the module docstring.
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}", stream_id=state.stream_id)
            return
        state.written += len(state.in_flight.packets)
        state.chunks += 1
        state.in_flight = None

    # -------------------------------------------------------------- closures

    def _close_finished(self) -> None:
        """Close every stream whose producer is done and fully drained."""
        with self._lock:
            finished = dict(self._finished)
        for stream_id, outcome in finished.items():
            state = self._streams[stream_id]
            if state.closed:
                continue
            sink = self._sinks[stream_id]
            if state.consumed < sink.submitted:
                continue
            self._close(state, outcome)
            if self._fatal is not None:
                return

    def _close(self, state: _StreamState, outcome: tuple[str, str]) -> None:
        """Commit what arrived, record why the stream ended, close it durably."""
        self._commit(state)
        if self._fatal is not None:
            return
        kind, message = outcome
        status = {
            "clean": StreamCloseStatus.CLEAN,
            "disconnected": StreamCloseStatus.DISCONNECTED,
            "failed": StreamCloseStatus.FAILED,
        }[kind]
        # The event's host arrival time is when the recorder *observed* the
        # outcome, which is within one tick of the source returning. When the
        # device actually failed is not knowable from here and is not invented.
        # Suppressed rather than fatal: the durable closure record below is the
        # terminal authority (D33), and losing it to a failed event write would
        # be the worse outcome.
        if kind == "disconnected":
            with contextlib.suppress(OSError, ValueError):
                self._writer.emit_event(
                    "DEVICE_DISCONNECTED",
                    "device_disconnected.v1",
                    origin="system",
                    payload={"stream_id": state.stream_id, "reason": message or "unspecified"},
                )
        elif kind == "failed":
            with contextlib.suppress(OSError, ValueError):
                self._writer.emit_event(
                    "TECHNICAL_ERROR",
                    "technical_error.v1",
                    origin="system",
                    payload={"message": message, "stream_id": state.stream_id},
                )
        self._writer.close_stream(state.stream_id, status)
        state.closed = True
        state.close_status = status
        state.error = message or None

    def _all_closed(self) -> bool:
        return self._handoff.empty() and all(state.closed for state in self._streams.values())

    # -------------------------------------------------------------- teardown

    def _teardown(self) -> None:
        """Stop the sources, write everything they queued, close every stream."""
        self._stop.set()
        deadline = time.monotonic() + self._join_timeout_seconds
        for thread, _stream_id in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        for thread, stream_id in self._threads:
            if thread.is_alive():
                self._streams[stream_id].unstopped = True

        if self._fatal is not None:
            # The session is already CLOSED / TECHNICAL_FAILURE. Writing
            # anything else into it would append to a terminal package. The
            # failing path already accounted for every unwritten packet; this
            # call is idempotent and only guards a fatal that somehow skipped
            # it. Lost to a DIAGNOSED failure, and counted, not invisible.
            self._account_unwritten()
            for state in self._streams.values():
                state.closed = True
            return

        # THE BARRIER (CL-003-R1). One atomic step: after it returns, no
        # producer can make another packet visible, and ``remaining`` is exactly
        # every packet that was accepted and not yet consumed. A producer still
        # blocked in ``submit`` — including one whose thread outlived the join
        # window — is woken and refused, so nothing can arrive behind us.
        remaining = self._lower_barrier()

        for index, (stream_id, packet) in enumerate(remaining):
            if self._fatal is not None:
                # A commit failed while writing this tail. Everything the
                # streams still held was accounted for inside that failure; this
                # slice is held only by this local list, so it is counted here
                # and nowhere else.
                for lost_id, _packet in remaining[index:]:
                    self._streams[lost_id].dropped += 1
                for state in self._streams.values():
                    state.closed = True
                return
            self._accept(stream_id, packet)
        self._close_finished()
        if self._fatal is not None:
            return
        for state in self._streams.values():
            if state.closed:
                continue
            if state.unstopped:
                # Its producer may still be generating packets nobody will
                # write. That stream is not clean and must not be recorded as
                # though it were.
                self._close(state, ("failed", "the source did not stop when asked"))
            else:
                self._close(state, ("clean", ""))
            if self._fatal is not None:
                return

    def _lower_barrier(self) -> list[tuple[str, SourcePacket]]:
        """Stop accepting packets for good. Idempotent; returns what was queued."""
        return self._handoff.close()

    def _account_unwritten(self) -> None:
        """Count every accepted-but-not-durable packet, exactly once (D39).

        Ownership, not arithmetic. An accepted packet is in exactly one of three
        unwritten places, and this walks all three:

        1. the **queued tail** — still in the hand-off, never consumed;
        2. each stream's **in-flight batch** — handed to ``commit_chunk``, whose
           write failed. This is the one that used to be a local variable and
           therefore vanished from the count along with a whole chunk;
        3. each stream's **pending accumulator** — accepted on some other stream
           and not yet cut into a chunk when the session died.

        Counting and clearing happen together, which is what makes it exactly
        once: a second call finds nothing left to own. Deriving the number from
        ``submitted - written`` instead would be self-consistent whatever
        actually reached the disk, which is why the identity is asserted rather
        than used as the source.
        """
        for stream_id, _packet in self._lower_barrier():
            self._streams[stream_id].dropped += 1
        for state in self._streams.values():
            if state.in_flight is not None:
                state.dropped += len(state.in_flight.packets)
                state.in_flight = None
            if state.pending.packets:
                state.dropped += len(state.pending.packets)
                state.pending = PendingChunk()
            state.started_ns = None

    def _enter_fatal(self, message: str) -> None:
        """Record a fatal error whose closure the writer has already performed."""
        if self._fatal is None:
            self._fatal = message
        self._stop.set()
        self._account_unwritten()

    def _fail(self, message: str, *, stream_id: str | None = None) -> None:
        """Close the session CLEAN / TECHNICAL_FAILURE, then stop everything."""
        if self._fatal is not None:
            return
        self._fatal = message
        self._stop.set()
        self._account_unwritten()
        with contextlib.suppress(OSError):
            self._writer.fail_technical(message, stream_id=stream_id)

    # ------------------------------------------------------------- producers

    def _produce(self, source: StreamSource, sink: _Sink) -> None:
        """One source's thread. It may not touch the writer, only the queue."""
        stream_id = source.descriptor.stream_id
        kind, message = "clean", ""
        try:
            source.run(sink, self._stop)
        except RecorderStoppingError:
            # Teardown. Ordinarily that is not a device fault and the stream is
            # closed CLEAN. But if the barrier REFUSED a packet, acquired data
            # never reached the package, and calling that stream clean would be
            # exactly the silent loss CL-003-R1 exists to make impossible.
            if sink.refused:
                kind, message = (
                    "failed",
                    (
                        f"{sink.refused} packet(s) were refused at the fan-in shutdown barrier; "
                        "acquired data did not reach the package"
                    ),
                )
            else:
                kind, message = "clean", ""
        except SourceDisconnectedError as exc:
            kind, message = "disconnected", str(exc)
        # A source may raise anything at all; none of it may escape this thread.
        except Exception as exc:
            kind, message = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            # Set last, after every submitted packet has been counted, so the
            # fan-in loop can safely treat "finished" as "nothing more coming".
            with self._lock:
                self._finished[stream_id] = (kind, message)
