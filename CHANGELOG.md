# Changelog

All notable changes to this project are recorded here. Structural decisions and
their rationale live in [`docs/DECISIONS.md`](docs/DECISIONS.md); this file
records what changed and when.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning policy is unresolved — see `docs/OPERATIONS.md`.

## [Unreleased]

### Added — CL-007-A: QT Py marker firmware, the serial probe, and the recording host

**`firmware/qtpy_marker/`** — CircuitPython for the marker channel: a switch that
reports, over USB serial, the device-clock time at which its polling loop first
*observed* the input go low. Not when the switch physically closed, which is
earlier by an unmeasured amount. That is all it is, and the README says so:
placing even that instant on a BLE stream's timeline is still the open question
`TIMING.md` calls the hardest in the study.

- The timestamp is taken on the **first edge**, before debouncing and before any
  serial write. Debounce first and you have added an unmeasured delay to a
  device whose whole purpose is knowing when. A test asserts the ordering
  against the source, since CircuitPython cannot run here.
- Device time and host arrival time are kept as **two quantities** and neither is
  derived from the other (`TIMING.md` rule 2). Periodic heartbeats exist so the
  offset between the clocks can be watched and its *drift* measured rather than
  an offset taken once being assumed to hold.
- `boot.py` gives marks their own USB serial channel, so a traceback on the
  console can never land inside the data stream.
- **No electrical contact with a participant**: a switch, a GPIO, and ground.
  Nothing is driven anywhere, and tests assert there is no output pin and no
  analog out. A version that injects into an EEG channel is a different device
  and a different decision, needing the exact model, its ADC limits, and a named
  human (`SAFETY.md` S6).

**`probe serial`** — closes the gate `HANDOFF.md` puts on CL-007: round-trip
latency **and its variability**. Token-matched, so a late reply cannot pass as a
prompt one, and unanswered pings are counted rather than quietly excluded — a
latency figure over only the replies that arrived flatters the link. It
interprets nothing: no offset, no drift, no corrected time.

This also closes a contradiction: CL-004's acceptance said the QT Py was out of
scope, while `HANDOFF.md` gated CL-007 on CL-004 having measured serial latency.
The probe is the measurement; it needs no marker design and no participant.

**`DECISIONS.md` D41 — the Mac records, mimisbrunnr stores and processes.**
Decided by Erandi. Session packages are designed to move between hosts
(`SESSION_FORMAT.md` Q8), so the split costs nothing structurally. Consequence:
every device row must be verified **on the Mac**, and the BlueZ finding recorded
on mimisbrunnr transfers to nothing. WSL was rejected on architecture: WSL2 has
no path to the host Bluetooth adapter without a passed-through dongle and a
custom kernel.

`HARDWARE.md` gains the macOS setup, including the CoreBluetooth permission that
makes every device look switched off when a terminal does not hold it.

**Corrected before review returned (CL-007-A-R1).** Two things:

- **`capture_polar` started ECG and nothing else.** The accelerometer is the
  channel that carries a physical event, so an ECG-only capture left the whole
  alignment question unmeasurable while looking like a working capture — the
  exact failure this package exists to prevent. It now starts every stream the
  device offers that polar-python can start, with every parameter read from the
  device's own settings response and gaps *reported* rather than filled in. A
  feature the library cannot start is a recorded finding about the library.
- **A settings-matching bug** meant no parameter was ever resolved: the device
  labels a setting `SAMPLE_RATE` while the library's parameter is `sample_rate`,
  and the comparison was case-sensitive, so it silently matched nothing and read
  as "the device did not answer".

The firmware README's section on striking was rewritten. The design was always
that **the button is the striking face** — the impact presses the switch — and
the previous text framed that as a correction rather than as the design. It now
lays out the alignment chain honestly: two sharp, marker-timestamped legs from
striking each device, with a cough as the independent cross-check that both
sensed one physical event. A cough is a few hundred milliseconds with a
build-up, so its instant is ambiguous; it is the right confirmation and the
wrong ruler.

**The Mac, prepared (D41).** `scripts/bootstrap-mac.sh` — idempotent, installs
nothing silently, and finishes by running `probe env`, so the first thing that
happens on that machine is evidence about it rather than an assumption. It is
honest about the one thing it cannot do: CoreBluetooth grants Bluetooth access
to the *application*, and without it every scan returns nothing and every device
looks switched off. The script names the symptom, not just the fix.

Checked rather than assumed: `uv.lock` already resolves for macOS — `bleak`
pulls `pyobjc-framework-corebluetooth` under a `sys_platform == 'darwin'` marker
and BrainFlow ships a `py3-none-any` wheel — so `uv sync --locked` needs no
special handling on the Mac. That was the risk worth finding before a bench
session, not during one.

`HARDWARE.md` gains **a bench session in order**, because the order is not
arbitrary: each step either produces evidence the next needs, or separates a
host problem from a device problem before an hour is spent confusing them. The
solo Muse and Polar probes must run before the concurrent one or there is
nothing for it to be compared against — and the comparison is the operator's,
not the probe's. Plus how to move evidence and sessions back to mimisbrunnr,
which is what `SESSION_FORMAT.md` Q8 designed the package to allow.

**Corrected in review (CL-007-A-R3).** Three findings, all correct:

- **The clock was sampled before the GPIO was read**, so a mark could precede the
  observation that produced it — biased early by the pin-read interval, and
  systematic rather than noise, which is the kind of error that survives
  averaging and never announces itself. The pin is now read first and the clock
  sampled *inside* the transition. The test previously only proved the timestamp
  preceded debounce; it now proves it follows the observation.
- **The firmware and README claimed `probe serial` measures the polling loop's
  detection jitter. It does not** — that probe times a ping reply and never
  touches the button or the GPIO. Claiming a quantity is measured by something
  that does not measure it is the exact failure this project exists to prevent,
  and it was in code written to prevent it. The number is now stated as
  unmeasured, with the bench setup that *would* establish it named and marked as
  not existing.
- **The CL-007 gate was routed around.** `HANDOFF.md` forbade starting CL-007
  before CL-004 had measured serial round-trip latency, and this ticket shipped
  firmware with no measurement taken. The gate is now amended on the record
  (`DECISIONS.md` D42, authorised by Erandi), with the original wording preserved
  above the amendment: it was **unsatisfiable as written**, because `probe
  serial` needs a device that replies, so "CL-004 measures serial RTT"
  presupposed the firmware CL-007 was forbidden to build. Only the *instrument*
  half moved. The alignment design stays gated, and the amendment says plainly
  that no physical measurement exists — the firmware has never been flashed.

**Corrected in review again (CL-007-A-R4).** The firmware README designed the
very thing D42 — written in this same change — says stays gated, and asserted
device behaviour nobody has observed. A rule broken by the change that
introduced it is worse than no rule.

- The "striking with the button" section is **gone**: the physical mechanism, the
  three-link alignment chain, and the cross-device check were alignment design.
- With it went claims like *"a cough is genuinely sensed by both"* and *"exactly
  the right instrument"* — statements about how devices and bodies behave, with
  no observation behind them (`AGENTS.md` §7).
- The README now says only what the device is, that relating a mark to any stream
  is undesigned and gated, and where the open questions live.
- **The proposal is not lost.** It is recorded in `HARDWARE.md` → Open questions
  as two questions, attributed and marked Unknown, with a note that it is
  recorded as questions and not as a design.

**A flaky test on `main`, found and fixed.** `test_no_packet_can_appear_after_the
_final_drain` and its sibling asserted `refused >= 1`, which is a matter of where
a producer thread happened to be when the join window expired — a schedule, not a
guarantee. They failed roughly half the time. Both now assert the actual
invariant: an abnormal end is *recorded* one way or the other and can never read
as clean. The refusal path itself stays deterministically pinned by the
`_Handoff` unit test that drives the barrier directly. 25 consecutive recorder
runs green, 5 consecutive full runs green.

**Corrected in review a third time, and then made mechanical (CL-007-A-R5).**
R4 removed the unverified device claims from the firmware README — and left the
same claims in `devices.py` and in a test docstring. *"The accelerometer is the
channel that carries a tap or a cough"* is a statement about what a device
senses, in source, while `HARDWARE.md` records exactly that as **Unknown**. The
code contradicted the documentation of the same repository.

The claims are gone. The capture's stated reason is now the only one available
without interpreting anything: **the device offers the stream, therefore it is
recorded.** Choosing a subset would require knowing what each stream is for,
which is what has not been established.

**That is three consecutive rounds of one defect class** — an unobserved device
claim, in a different file each time, surviving because each fix was aimed at the
file rather than the mistake. `DECISIONS.md` D27 records the project's own rule
for this: when successive rounds keep finding new instances of one class, the
defect is not the instances.

So `tests/test_vocabulary.py` now enforces it. Code may not name a physical event
a person might produce — not in a comment, not in a docstring, not in a test —
because naming one means the code has decided what a signal is *for*. Proposals
live in `HARDWARE.md` as open questions. The guard matches whole words only, and
its own tests prove both that it catches the exact sentence that got through
three times and that it does not fire on ordinary prose; the flashing
instruction that collided with it was reworded rather than exempted, because a
guard with exceptions accumulates exceptions until it means nothing.

**Corrected in review (CL-007-A-R6) — the authority documents contradicted the
tree.** Adding firmware made four statements false and none of them was updated:
`HARDWARE.md`'s status row still said *no firmware written*, its assumptions
section still said *No firmware exists in `firmware/`*, `firmware/README.md`
still said *Empty at CL-001*, and `HANDOFF.md`'s layer table still said the
acquisition layer had no firmware. `HARDWARE.md` is the project's authority on
what exists versus what is assumed versus what is verified; a change that
invalidates one of its statements and leaves it standing turns the authority into
a liar, quietly, because nothing fails.

All four corrected to the truthful boundary, and the boundary is the point:
**firmware exists and has never been flashed.** The QT Py row still reads
*Pending verification*. Nothing about the firmware's behaviour has been observed
— not that it runs, not that it enumerates as a serial device, not that it emits
one line. Serial round-trip latency stays unmeasured, detection jitter stays
unmeasured with no bench setup that would measure it, and the marker mechanism
stays gated by D42. **Code existing is not a device behaving**, and the documents
now say so in those words.

`tests/test_documents_match_the_tree.py` makes it mechanical. Statements that can
be checked against the filesystem are, in both directions — a document may not
claim firmware is absent while it is present, nor present once it is gone — and
the QT Py row is separately asserted to still read *Pending verification*, because
the risk in updating a document to admit code exists is that it drifts toward
sounding verified. The guard was tested by reintroducing the false statement and
confirming it fails.

This is the second time stale status statements needed a ticket: CL-003-R3 existed
only to correct three that had been false since CL-002B.

**Corrected in review (CL-007-A-R7) — two timing claims nobody had measured.**
Same class as R3–R5, escaping through a guard that was too narrow: the vocabulary
check catches code *naming a physical event*, and neither of these named one.

- **The marker said it reports "exactly when it was pressed."** It polls. It can
  only report the device-clock time at which the loop *first observed* the input
  go low; the physical closure is earlier by an interval this same head records
  as unmeasured. A claim contradicted by a document in the same commit, for the
  second time in this PR. `M` is now described as the first **observed** high→low
  transition, with the gap to the physical edge kept explicit in both the
  firmware and its README.
- **The boot banner hard-coded `ns_per_tick` to `1`.** `time.monotonic_ns()`
  returning integer nanoseconds says nothing about the board's tick — a coarse
  clock scaled into nanoseconds is indistinguishable from a fine one at this end.
  The banner now reports `time_unit=ns resolution=unmeasured`: the representation
  is knowable from here and is stated as fact, the resolution is not and says so.

The guard is widened rather than the instances patched, which is the third time
that has been the right move in this PR. `test_vocabulary.py` now also refuses
absolute-precision phrasing in the marker files, requires them to say the mark is
an *observation* and to keep the gap *unmeasured*, and forbids any per-tick number
in the banner. Both new guards were verified by reintroducing the exact defect and
confirming they fail.

**Corrected in review (CL-007-A-R8) — R7's fix, undone three paragraphs later.**
The README's opening correctly said `M` is the first *observed* transition, and a
later section still said *"this firmware reports when its switch closed"*. Same
defect, same file, same head. A second instance sat in this changelog.

The deeper problem was the guard R7 added: it checked that the honest words were
**present** somewhere in the file, which a contradiction elsewhere passes
trivially. Presence is not absence.

- The guard is now **sentence-level and wrap-proof**. A reporting verb paired with
  the physical event, in a sentence that never says *observed*, fails. Whitespace
  is normalised first, because the sentence that survived a whole review round did
  so by being split across two lines — including one wrap introduced by the very
  edit that was fixing it.
- It reads **prose only**. A first version split `.py` files on periods, joined a
  trailing comment to the next `def`, and invented a sentence nobody wrote;
  comments and docstrings are extracted properly now.
- `CHANGELOG.md` is deliberately outside its scope: a history that quotes a past
  error while describing its fix is doing its job.

Verified by reintroducing the exact two-line-wrapped sentence and confirming it
fails.

**Corrected in review (CL-007-A-R9) — source still enforced the rule this PR
repealed.** `capture_serial`'s docstring said *"the marker firmware cannot be
designed until serial round-trip latency and its variability are measured"*. D42,
added in this same pull request, says the opposite: the **instrument** — firmware
that answers a ping, and the probe that times it — is a prerequisite *for* the
measurement and had to exist first; only the mechanism and its electrical
interface stay gated. The repository was giving two incompatible instructions in
one commit, and closing exactly that contradiction was in this ticket's objective.

Both gate statements in `src/` now match D42. A sweep found the second one in
`probe.py`, which said *mechanism* and was already correct but read ambiguously;
it now names what is gated and what is not.

The class is R6's — prose contradicting an authority — one level up: not a
document against the tree, but source against a **decision**. So it is guarded
the same way. A sentence in `src/` that asserts something is gated on a
measurement, names the firmware or the probe, and does not name the mechanism,
alignment or electrical interface, now fails. Verified by reintroducing the
superseded wording.

**32 new tests** (510 → 542, plus 1 deselected).


### Added — CL-004: hardware verification harness

Hardware is in hand. It does not unlock writing device adapters — it unlocks
**verification**. Every row in `HARDWARE.md` still reads *pending verification*,
its log is empty, and all five of its open questions gate a design choice in the
adapter that would otherwise be guessed.

**`verification/`** — a diagnostic that sits beside the system, not inside it
(`DECISIONS.md` D40). Not a sixth layer, takes no data anywhere, and nothing in
the data path may import it.

- **It observes and does not conclude.** An `Observation` is `what`, `value`,
  `how` — there is no field for what it means, and a test asserts that. Series
  are *described* (monotonic, its steps, where it went backwards) and never
  named, because `HARDWARE.md` warns that a BrainFlow timestamp column does not
  establish that the timestamp came from the device.
- **`AGENTS.md` §7 is structural, not a convention.** A run without its purpose,
  method, host, library versions and — for a device — firmware cannot be
  written at all. Nothing reaches disk before that check. Failure modes are
  required too: a run that saw nothing and reports no failure is refused,
  because silence records that nobody looked.
- **Verification is not a session.** Reports go to `data/verification/`, never
  `data/sessions/`, and writing one produces no package. Putting a device on to
  see whether it streams is not a study recording.
- **Promotion stays human.** Reports carry `verified: false`. No code path can
  move a `HARDWARE.md` row.
- **`consciousness-lab probe env | scan | muse | polar`**, with `--purpose`,
  `--method` and `--firmware` required and undefaulted.
- **`hardware` pytest marker**, excluded by default so no default run opens a
  Bluetooth radio; a test asserts the exclusion rather than trusting it. A
  passing hardware test says a code path ran, not that a device behaves.

**First real finding, from `probe env` on the intended host:** mimisbrunnr has
the radio (Intel `8087:0026`, `btusb`, `hci0`) and D-Bus, but **BlueZ is not
installed** — no `bluetoothd`, no `bluetoothctl`, no `bluetooth.service`. No BLE
device is reachable there until it is. Recorded in `HARDWARE.md` as a **host**
observation; no device row moved.

**Corrected during review (CL-004-R1).** The first head shipped a measurement
surface that could not answer two of this ticket's own acceptance questions:

- `capture_polar()` connected, listed services and polled the host clock — it
  never subscribed to a characteristic, so it produced no evidence about
  delivered data at all, and built a `notifications` list it never filled. It
  now runs two paths: raw GATT notifications from every notifiable
  characteristic (undecoded, so nothing can be misdecoded), and the PMD streams
  that need a control-point handshake, via polar-python with parameters taken
  from the **device's own** `request_stream_settings` response rather than a
  number chosen here.
- There was no reconnect probe and no concurrent-peripheral probe, though both
  are `HARDWARE.md` open questions this ticket exists to measure. `probe
  reconnect` captures two windows around a real disconnection and puts them
  side by side, computing no difference; `probe concurrent` holds both devices
  on one adapter and records what each did, comparing against nothing.

Both stay evidence-only, and tests assert the silence: the reconnect report
contains no word that reads as a verdict, and the concurrent report never says
anything degraded.

**28 new tests** (461 → 489, plus 1 deselected). No device adapter, no
transport, no reconstructed timing, no analysis. Session Package v2, D27–D34 and
the CL-003 recorder are untouched. The QT Py is deliberately out of scope: no
firmware exists and the marker mechanism is undesigned.

### Fixed — CL-003-R3: chunk_max_rows is a packet-boundary bound, and D37 said otherwise

Final CL-003 verification found a contract mismatch. D37 and `_should_cut()`
claimed no raw table in a chunk exceeds `chunk_max_rows`, but `_accept()`
appended a whole packet and only then tested the threshold. Reproduced: a limit
of 4 with 16-sample packets produced chunks of 16, four times the bound.

Resolved as a contract, not papered over:

- **The bound is enforced before a packet is appended.** With 3-sample packets
  and a limit of 10 a chunk now reaches 9; under the old order it reached 12.
- **One documented exception**, because packets are never split: a packet whose
  own rows exceed the limit becomes an oversized chunk of its own, committed
  immediately so the overflow is one packet wide. The packet→sample grouping is
  a preserved acquisition fact (v1 spec §19); chunk boundaries carry no
  analytical meaning (§10.2), so the boundary moves and the grouping does not.
- **D37 corrected in place**, with the original overclaim kept on the record.
  The frozen spec is untouched: §12.2 only ever said "whichever comes first" and
  never claimed a hard cap — the overclaim was in the decision record.

The change opened a new hole and the accounting identity caught it immediately:
a pre-emptive commit can fail, and the packet it was making room for is held by
nothing `_account_unwritten()` can walk. It is now counted at that point, like
the closed-stream branch. That is what D39's identity is for.

**5 new tests** (456 → 461): the bound across many packets, the oversized-packet
exception and that it does not drag ordinary packets along, observation rows
bounded as well as samples, and the failed pre-emptive commit.

### Fixed — CL-003-R3: stale status statements (documentation only)

Three places still described the repository as it was at CL-001, which stopped
being true when CL-002B landed and has been false through two more tickets. They
were reported as findings during CL-003 and left alone as out of scope; they are
corrected here, in their own documentation-only change.

- `README.md` said the current milestone was CL-001 and that "no acquisition,
  session or analysis code exists yet". Now names CL-003, says what exists and —
  more importantly — what deliberately does not: no device adapter, no BLE or
  serial transport, no reconstructed timing, no analysis.
- `docs/ARCHITECTURE.md` said "Nothing described below is implemented".
  Now carries a per-layer status table. `acquisition` and `analysis` are empty
  and `core` does not exist, which is stated rather than implied.
- `src/consciousness_lab/__init__.py` said the package contained no session
  handling. It has contained the session layer since CL-002B.

**No code, test or behavior changed**, and no claim about hardware moved: every
device is still unverified and README's hardware section is untouched. The point
of this change is that a document which overstates what exists is the same defect
as one that understates it — both make the next reader guess.

### Fixed — CL-003-R1-R2: account for every accepted-but-unwritten packet

Review of the CL-003-R1 barrier found the new `dropped` accounting incomplete on
a fatal write. The batch handed to `commit_chunk` lived in a local inside
`_commit()`, so when that write failed it vanished with the stack frame; packets
accepted onto *other* streams sat unseen in their pending accumulators. Only the
queued tail was counted, so a session could lose two whole chunks of acquired
data and still report `dropped == 0` — contradicting D38's guarantee that every
submitted packet is either durable or explicitly surfaced.

Reproduced before the fix: 5 packets submitted on stream A (2 durable) reported
`dropped == 1`; stream B with 3 accepted packets reported `dropped == 0`. Five
acquired packets unaccounted for.

- **One ownership path** (`DECISIONS.md` D39). An accepted packet is in exactly
  one of `_Handoff` → `pending` → `in_flight` → written. `in_flight` is now a
  field on the stream rather than a local, so a failed batch stays reachable.
  `_account_unwritten()` walks all three unwritten places, counting **and
  clearing** them, which is what makes the count exactly once.
- **`submitted == written + dropped` on every path**, exposed as
  `RecorderReport.accounted` / `.unaccounted` and as new `submitted` and
  `written` fields per stream. It is *checked* against the independent ownership
  count, never used to derive it: a count inferred by subtraction agrees with
  itself whatever actually reached the disk.
- A packet arriving for an already-closed stream — unreachable by contract — is
  counted too, since it is held by nothing the accounting can walk.
- Producer threads are now paired with their stream id at creation instead of
  matched by position against the source list.

**4 new tests** (452 → 456): the two-stream regression the review asked for
(A's commit fails while B holds accepted packets in `pending` and a tail remains
queued, asserting all three are counted), an over-count guard proving repeated
accounting is idempotent, the identity end to end through `run()` with real
threads, and the same identity on a clean session. No Session Package v2
behavior changed.

### Fixed — CL-003-R1: the fan-in shutdown barrier

Review of CL-003 found that teardown could race an in-flight `queue.put`. A
producer already blocked inside `put` could be accepted *after* teardown set
`_abandoned` and after its single final drain, leaving an acquired packet in the
queue: never written, never reported. That breaks CL-003's central guarantee
that back-pressure blocks and acquired packets are never silently dropped.

Both defects were reproduced against the pre-fix code before anything changed.

- **`_Handoff` replaces the queue-plus-flag** (`DECISIONS.md` D38). Acceptance
  and the shutdown decision happen under one lock, and `close()` lowers the
  barrier and takes everything still queued in the same hold — so the list it
  returns is provably every packet that was accepted and not yet consumed, with
  no window on either side. A producer waiting inside `put` is woken and
  **refused**; its packet was never accepted, so "submitted" still implies
  "written".
- **A refusal is never reported as a clean stream.** The stream closes `FAILED`
  with the refused count, and `RecorderReport.ok` requires zero refused and zero
  dropped. Packets that a *diagnosed* fatal error prevents from being written
  are counted as `dropped` rather than vanishing.
- **`request_stop()` now always terminates.** The fan-in previously ended only
  when every stream had closed, so a source that never noticed `stop` — a vendor
  SDK inside a blocking read — made `run()` unreturnable. It now stops on a
  wall-clock grace period; real time deliberately, so an injected scheduling
  clock cannot defer a safety timer.

**6 new tests** (446 → 452), including the deterministic barrier regression:
a producer is forced to be blocked in submission across the join-timeout path,
and the test proves both that it is refused and that every packet whose
`submit()` returned is on disk. No Session Package v2 behavior changed.

### Added — CL-003: asynchronous multi-stream recorder with fan-in orchestration

**Scope recovered from the repository, not chosen.** Four records name CL-003 —
`CHANGELOG.md` (CL-002B), `synthetic/source.py`, `synthetic/__init__.py` and
`session/writer.py` — and between them define it by naming exactly what CL-002B
left out: **asynchrony, device fan-in, scheduling, back-pressure**. Those four
properties, and nothing else, are what this ticket implements.

**`session/recorder.py`** — `Recorder` runs one thread per `StreamSource` and
funnels every packet through a bounded queue into the calling thread, which is
the only thread that ever touches `SessionWriter`. The writer's single-threaded
promise is preserved rather than deleted (D35).

- **Scheduling comes from the frozen schema, not from new constants.** Chunk
  boundaries follow `run.json.writer_config.chunk_max_rows` /
  `chunk_max_seconds`, and the periodic `CLOCK_SNAPSHOT` that v1 spec §10.3
  requires — and which nothing implemented, because nothing ran a loop — now
  follows `clock_snapshot_interval_seconds`. `chunk_max_rows` is applied to
  every raw table, which is the safer reading of a field the spec leaves
  untabled (D37).
- **Back-pressure blocks, never drops.** A producer that outruns the writer
  waits. Where the host could not keep up, the device counter is the authority
  on loss (`TIMING.md`); the recorder fabricates no substitute.
- **Host arrival times are never re-stamped.** Only the source can capture "the
  moment the packet surfaced to our process"; a time taken after the packet
  waited in a queue would silently be a different quantity (`AGENTS.md` §4).
- **Apparatus failure ends the session; device failure ends one stream** (D36).
  A failed chunk commit closes the session `CLEAN / TECHNICAL_FAILURE` through
  the CL-002B path. A source that stops abnormally closes only its own stream —
  `DISCONNECTED` only when the source raises `SourceDisconnectedError`, `FAILED`
  otherwise — and the other streams keep recording. Packets that already
  arrived are committed before the stream closes.
- **No scientific decision is made.** The recorder has no session duration, no
  minimum packet count, no quality notion and no Focus semantics. It never
  chooses a `RecordingOutcome` and never calls `finalize()`.
- **Nothing new is persisted.** Back-pressure and stream reports are returned in
  memory. No new event schema, no new package authority, no new file (D29).

**`synthetic/source.py`** — `SyntheticStreamSource` adapts the existing
deterministic generator to the recorder's source contract, plus
`as_source_packet()`. The generator itself is unchanged.

**Session Package v2 was not reopened.** No schema field, enum value, event
name, event meaning, decision record D27–D34 or frozen document was changed.
`WriterConfig` was read for the first time; it was not modified.

**25 new tests** (421 → 446), covering fan-in onto exactly one writer thread,
host-arrival immutability, both chunk-cut rules, the periodic snapshot,
back-pressure under a one-slot queue, per-stream and session-level failure,
operator stop, byte-level determinism, and every refusal.

**Out of scope, unchanged:** device adapters, BLE/serial transport,
reconstructed timing, cross-device alignment, analysis. No hardware fact was
promoted from assumed to verified; no device has been connected.
### Added — `docs/HANDOFF.md`

State of the repository and the ticket queue for whoever picks the work up next.
Records the baseline (`67c76f9`), what is frozen and must not be reopened, the
`StreamSource` contract a device adapter satisfies, and a ticket sequence
CL-004 → CL-008 whose scope is recovered from existing repository text rather
than chosen.

The ordering decision it records: **CL-004 is a characterisation harness, not an
adapter.** `HARDWARE.md`'s five open questions are measurements, not decisions,
and every adapter design depends on their answers — writing an adapter first
means guessing them and then encoding the guess into immutable data.

It also splits `DECISIONS.md` §"Awaiting a named human" into what the arrival of
physical devices makes answerable (the `HARDWARE.md` status table, and
`required_streams` — the one item that gates a working `Finalizer`) and what it
does not touch (Phase 0, every scientific threshold, consent and retention
policy, withdrawal versus raw immutability).

### Fixed — CL-002B-R2-R3: write-side conformance

A second scoped Codex review of `8bad8bb`, deliberately aimed at the **write**
side rather than the verifier — the blind spot the first review's scope left
open, and where every one of the R1/R2 human findings had landed. It returned
**NO-GO** with seven findings; all seven were reproduced before anything changed.
No frozen document, decision record, relation, recovery semantic or scientific
assumption was touched.

The unifying defect: hardening the verifier does not stop a *writer* from making
an irreversible durable claim first. A verifier finding is recoverable — you
learn the package is bad. A bad write is not: raw data is immutable, so the
false claim is now permanent acquisition data.

- **A chunk could be committed after its stream closed.** `close_stream` writes
  an immutable closure record; a later `commit_chunk` made that record false,
  and the package still sealed and verified COMPLETED. Closure is now terminal
  for a stream exactly as CLOSED is for a session: the commit is refused, not
  the record.
- **The writer committed chunks the verifier would reject.** `commit()` checked
  only emptiness, payload count and observation semantics — non-increasing
  `packet_seq`, missing dense sample keys, dangling references, duplicate sparse
  triples and mismatched payload frames all reached immutable raw. Every chunk
  is now reconciled against the **physical bytes just written**, by
  `stream_state.reconcile_chunk` — the same function verification uses, not a
  writer-side copy that could drift — plus the chain-level ordering rule a
  single chunk cannot see. Failed artifacts stay on disk as orphans, which is
  exactly what a crash at that point leaves.
- **Terminal `CLOSED / CLEAN / COMPLETED` was appended before raw conformance
  was checked.** Committing one bad chunk produced a permanent COMPLETED claim
  over a package that could never verify and could never be closed any other
  way. The full preflight now runs *before* the terminal record, and again
  before the manifest, because the appends change what is being sealed.
- **`lifecycle.jsonl` and `events/events.jsonl` were hashed into the manifest
  without being verified.** A record whose own `record_sha256` did not verify
  was sealed anyway, manufacturing a package that condition 3 then rejected.
  Both are now checked with the verifier's own `verify_jsonl_region` before any
  digest is written.
- **A sealed or terminally closed package still accepted writes.**
  `emit_event`, `commit_chunk`, `open_stream` and `start_recording` all now go
  through `_assert_writable`, which reads the **durable** state — so a second
  writer or a resumed process is stopped by the same rule, not by a flag.
- **Publishing a write-once file could leave a trap.** `atomic_write_new`
  published by hard-linking a named temporary and then unlinking it; a crash
  between the two left `manifest.sha256` *and* `manifest.sha256.tmp` as the same
  inode — accepted by condition 1, rejected by condition 6, classified SEALED by
  recovery, and clearable by nothing. Publishing now uses an anonymous
  (`O_TMPFILE`) file where the filesystem supports it, so there is no name to
  clean up; where it does not, `recovery.publish_residue` recognises the
  leftover by device+inode identity and `resume_finalization` clears it. Only
  provably redundant residue is touched — a genuine half-written file is
  evidence and is left exactly where it is.
- **`close_unclean` could trap a package permanently.** A crash leaving an
  orphan artifact produces a package no valid v2 seal can ever cover; writing
  the terminal record over it made that permanent, since CLOSED cannot be
  re-closed and the package cannot be resumed. It now reports
  `sealing_blockers` and refuses by default, with `force=True` to record the
  terminal fact as a deliberate decision rather than a side effect. An ordinary
  crash with no blockers closes and seals exactly as before.

**Tests: 421 → 442**, in `tests/test_write_side_conformance.py`.

### Fixed — CL-002B-R2-R2: pre-seal control consistency

Human review of `cc5baf3` closed the five R1 findings and raised two more. Both
were reproduced before anything changed. No frozen document, decision record,
relation, recovery semantic or scientific assumption was touched.

- **`finalize()` decided from memory and hashed from disk.** Structural and
  completion decisions came from `writer.run`, while `control_sha256` sealed
  whatever `run.json` physically held, and `allocation.json` was hashed without
  ever being typed. Both physical control documents are now loaded and validated
  before anything is written — canonical on disk, through their models,
  `allocation` required to declare major 2 and to name its own directory — and
  the **physical** `Run` is the authority for stream declaration, membership and
  the required-stream checks. A valid-but-different `run.json` is the dangerous
  case, not the malformed one: `run.json` is sealed once and never rewritten, so
  a divergence from what the writer sealed is external mutation, and
  finalization refuses rather than silently choosing a side. On any of these,
  nothing is written: no closure record, no lifecycle transition, no manifest.
  *The invariant: the control documents finalization decides on are the same
  physical bytes `control_sha256` seals.*
- **State B recovery could digest a manifest the verifier would reject.**
  `_resume_over_existing_manifest` checked canonical bytes, model parse and the
  three authoritative fields, but not schema major or the removed v1 keys — so
  minor-version tolerance let a `schema_version: "1.0"` or a resurrected
  `streams` block through, and recovery would happily attest to it. Verification
  and recovery now share **one** definition of a valid v2 manifest,
  `package_layout.check_manifest_contract`: canonical on disk, model-valid,
  major 2, no key v2 deleted. A digest is an attestation, so it must never be
  created over bytes the verifier would then reject — that manufactures a sealed
  package that cannot verify, which is worse than an unsealed one that honestly
  cannot. `extra="forbid"` was again not used, and an unknown future v2.x field
  is still tolerated and still resumable, with a test that says so.

**Tests: 398 → 421**, in `tests/test_control_authority_r2.py`.

### Fixed — CL-002B-R2-R1: human-review conformance gaps

Human review of `8640d9d` returned **NO-GO** with five implementation defects.
All five were reproduced against the physical bytes before anything was changed.
No frozen document, decision record, relation, recovery semantic or scientific
assumption was touched.

- **Allocation structural validity was missing from the predicate.**
  `allocation.json` was sealed and hashed but never typed, so a canonical
  document violating the model — wrong schema major, an invalid participant
  pseudonym, a `session_id` naming a different package — could be rehashed into
  `control_sha256` and still verify complete. It is now validated from its
  physical bytes via `load_on_disk(Allocation, ...)`, required to declare major
  2, and required to agree with the directory name. Inside **condition 2**, so
  the predicate still has exactly eight conditions.
- **Known v1 `ChunkCommit` fields could be resurrected.** `parse_chunk_record`
  rejected `record_sha256` alone; `first_packet_seq`, `last_packet_seq`,
  `descriptor_sha256` and the old top-level `packets` / `observations` /
  `samples` / `payloads` blocks were silently ignored. All eight now fail
  closed via `REMOVED_CHUNK_KEYS`, the same principle already used for
  `REMOVED_MANIFEST_KEYS`: a known-deleted authority is not an unknown future
  field, and forward tolerance for genuine v2.x fields is preserved and tested.
- **The real manifest crash window was unhandled.** A crash between step 10 and
  step 11 leaves `manifest.json` present and `manifest.sha256` absent;
  `resume_finalization` tried `atomic_write_new` over the existing manifest and
  died on `ImmutableFileError`. All four partial-pair states are now explicit:
  **A** neither file — reconstruct and write the pair; **B** manifest without
  its digest — validate the existing bytes against independently derived state
  and, only if they agree, write the digest over them, never replacing the
  manifest; **C** a lone digest — impossible under the specified order, fails
  closed, no manifest is invented; **D** a non-verifying pair — fails closed,
  neither file rewritten.
- **`fail_technical` could write CLOSED with a stream still unclosed.** With
  two streams open, the failing one got `FAILED` while the healthy one got no
  closure record at all — and CLOSED is terminal, so recovery could never
  afterwards create it and resumption blocked forever. Every terminal path now
  goes through `close_all_streams`: the diagnosed stream gets `FAILED`, the
  others `CLEAN` because that is what was observed, and nothing invents
  `DISCONNECTED` for a stream that showed nothing. If closure cannot be made
  durable, **no CLOSED record is written** — an interrupted session is true, a
  terminal one would not be. `finalize` enforces the same rule.
- **Stream declaration was applied only to COMPLETED.** V09's stream-set
  agreement is structural, not outcome-specific, so an ABORTED or
  TECHNICAL_FAILURE package could be sealed with a stream declared in neither
  `required_streams` nor `optional_streams`. The preflight is now split:
  `_assert_sealable` (declaration, valid durable closure) runs for **every**
  outcome; `_assert_completable` keeps only the completion-specific rules. No
  new completion condition was introduced.

`resume_finalization` additionally validates `allocation.json` and `run.json`
through their models rather than as canonical dictionaries: a resumable
transaction must never create a manifest pair over a structurally invalid
control document.

**Tests: 366 → 398**, in `tests/test_conformance_gaps_r1.py`. Every regression
mutates physical bytes and then rebuilds the whole chain, `control_sha256` and
the manifest pair, so what fails is conformance rather than a stale digest —
and one test asserts that premise directly, proving the reseal helper leaves an
untouched package complete.

### Added — CL-002B-R2: Session Package v2 implemented

`src/` now writes and verifies **Session Package v2** (`schema_version = "2.0"`).
The v1 implementation is replaced, not carried alongside: no Study 001 recording
exists under v1, so backward compatibility was not a requirement, and a v2 reader
meeting a v1 package raises `UnsupportedSchemaVersionError` rather than guessing
(D27). No legacy or debug reader was built, because nothing needed one.

**Removed from the production writer** — each was a persisted second copy of a
fact with another authority (D28, D29):

- per-chunk `NNNNNN.commit.json` sidecars; `chunks.jsonl` is the sole commit
  authority
- `ChunkArtifact` entirely — artifact `path` and `bytes` are gone, replaced by
  `artifact_sha256`, a map of kind to digest
- `first_packet_seq` / `last_packet_seq` / `descriptor_sha256` from the commit
  record, and `record_sha256` from `chunks.jsonl`
- `ManifestStream`, `manifest.session_id`, `manifest.scope_note`, the flat
  `inventory`, the `schemas` list and the `events_seal` wrapper
- `payload_ref` — from the packets Arrow schema, the writer, the reader and the
  synthetic source. The packets schema is now identical at every capture level.

**Added**

- `raw/<stream_id>/stream_close.json`, written once per opened stream via
  tmp → fsync → rename → fsync(dir) and immutable thereafter (D33). It is the
  sole persisted closure authority; the manifest seals it and does not repeat it.
  `RECOVERED_UNCLEAN` joins the status domain.
- `manifest.control_sha256`, a map keyed on a **derived** control set —
  `allocation.json`, `run.json`, each stream's three control files, and exactly
  the schema snapshots the sealed events log references. Raw artifact hashes
  appear once, in `chunks.jsonl` (D30).
- `storage/package_layout.py`: the closed v2 file set, the derived control set,
  the referenced-schema set, and canonical-on-disk verification for documents
  and JSONL regions.
- Physical Arrow schema conformance and observation row semantics validated on
  **read** by the same shared validator the writer uses (D31).
- `recovery.resume_finalization`: a durable terminal lifecycle record with no
  manifest pair is `INTERRUPTED_FINALIZATION` and may be resumed without
  altering the recorded outcome. Resuming an ABORTED package seals ABORTED.

**Fixed while porting**

- `recovery.scan` and the derived registry read the **whole** `lifecycle.jsonl`,
  while the verifier reads only the sealed prefix. A record appended past
  `sealed_len` therefore changed the outcome those two reported. Both now use
  `lifecycle.authoritative_records`, so every reader agrees where the authority
  stops.
- The registry's stream rows were built from `manifest.streams`, which v2 does
  not have; they now come from the physical stream directories and each
  stream's own `stream_close.json`.

**Tests: 340 → 366.** Four v1 files were deleted outright, having existed only to
reconcile representations v2 removed: `test_equivalence_matrix.py` (the R01–R34
matrix), `test_canonical_and_physical.py` (sidecar and summary reconciliation),
`test_leaf_integrity.py` (inventory `bytes`, duplicate inventory paths) and
`test_referential_integrity.py` (manifest ↔ filesystem). Six replaced them:
`test_relations_v01_v14.py`, `test_v2_contract.py`, `test_layout_closure.py`,
`test_arrow_conformance.py`, `test_stream_closure.py`,
`test_finalization_and_resume.py`, `test_canonical_on_disk.py` and
`test_mutation_properties.py`. Adversarial tests mutate physical bytes and then
restore every hash a forger could restore, so what survives is the conformance
rule rather than an incidental hash mismatch.

**Five defects the scoped conformance review found, all reproduced
independently before being fixed:**

- The manifest accepted the keys v2 *deleted* — `streams`, `session_id`,
  `inventory`, `schemas`, `scope_note`, `events_seal` — because
  minor-version tolerance ignores unknown fields. A resealed package could
  therefore carry a `streams` block contradicting the authority the fact
  actually lives in. Those six names now fail closed; a genuinely new v2.x
  field is still tolerated, which is the point of the tolerance.
- Canonical-on-disk was enforced for the manifest and the JSONL logs but not
  for `allocation.json`, `run.json`, `descriptor.json`, `stream_close.json`,
  the schema snapshots or `annotations.head.json`. A hash proves identity, not
  canonicity, so each could have been sealed in a second spelling.
  `annotations.head.json` mattered most: it sits outside the DAG, so nothing
  else would have noticed.
- The closed-layout scan checked the root, `events/` and `schemas/`, then
  descended per stream — so a file dropped straight into `raw/` fell between
  the two passes and no per-stream scan ever saw it.
- `finalize` would write an immutable `CLOSED / CLEAN / COMPLETED` lifecycle
  record for a package containing an undeclared stream, or an unclosed file
  set, and only the verifier would object afterwards. Both are now refused
  before any lifecycle record is written, and the file-set check runs for
  every outcome — a sealed ABORTED package with an unexpected file is just as
  unverifiable as a sealed COMPLETED one. `resume_finalization` enforces the
  same.
- `open_package` skipped condition 5 entirely, so a package with an undeclared
  stream or no closure record was readable. Condition 5 is now **split**: its
  structural half (declaration, a valid `stream_close.json`, a readable
  `run.json`) gates reading; its outcome half (required stream CLEAN) does
  not, because a required stream that closed DISCONNECTED is exactly what a
  legitimately aborted package looks like, and refusing to read it would make
  the failure case the unreadable one.

**One deliberate non-enforcement, stated rather than hidden.** A deleted
`observations` row is not detected, because §10.3 names no observation primary
key and forbids inventing one — observations are not a complete set by contract.
`test_mutation_properties.py` asserts that boundary explicitly instead of
implying the class is covered.

### Fixed — CL-002A-R3-APPROVAL-ERRATA: stale implementation pointer (documentation only)

`docs/SESSION_FORMAT.md` §"What implementation owes this document" still read
"CL-002B implements `SESSION_SCHEMA_PROPOSAL.md` as approved", pointing the live
implementation obligation at the **superseded v1** specification and contradicting
the freeze recorded at the top of the same document. It now points at
`SESSION_SCHEMA_V2_PROPOSAL.md` and **CL-002B-R2**, and states that CL-002B's v1
obligation is discharged and historical — `src/` writing v1 today is the gap
CL-002B-R2 closes, not a defect against the frozen v2 specification.

Documentation only. No architecture, decision, schema semantics, accounting,
recovery behaviour or scientific scope was touched.

### Approved — CL-002A-R3-APPROVAL: Session Package v2 frozen (documentation only)

**Human approval, 2026-08-31.** Session Package v2 architecture is **APPROVED**;
draft records **D27–D34 are APPROVED** and are now recorded in
[`docs/DECISIONS.md`](docs/DECISIONS.md) as decisions, not drafts. No further
architecture or adversarial review is required. Production code and tests are
unchanged — the implementation in `src/` still writes v1, and CL-002B-R2 is the
task that migrates it.

**Frozen**

- `docs/SESSION_SCHEMA_V2_PROPOSAL.md` — now **APPROVED AND FROZEN**, the
  authoritative specification for `schema_version = "2.0"`. A future
  implementation discrepancy is a bug unless a later decision record supersedes
  it. §16 no longer holds drafts; `DECISIONS.md` governs.
- `docs/PACKAGE_INTEGRITY_V2.md`, `docs/V1_TO_V2_SIMPLIFICATION.md` — frozen with
  it.
- `docs/SESSION_SCHEMA_PROPOSAL.md` — marked **SUPERSEDED BY SESSION PACKAGE v2,
  historical record**, per D27. Nothing is deleted; its approval history stands
  as recorded, and it remains accurate about v1. `docs/CHUNK_EQUIVALENCE.md`
  stays as the v1 implementation audit.
- `docs/SESSION_FORMAT.md` — points at v2 as the current contract, keeps the v1
  baseline and its audit as history, and states plainly that `src/` still writes
  v1.

**Three historical/accounting wording corrections required by the approval.**
None changes the 34 → 14 accounting or any v2 semantics.

- **Scope.** "v2 relations with no v1 ancestor" now reads "with no **R01–R34**
  ancestor" everywhere. R01–R34 is the numbered v1 equivalence matrix, not an
  inventory of every normative invariant the approved v1 specification states.
- **V11 history corrected.** v1 §12.2 already required exactly one canonical
  record per line, terminated by a newline, in `lifecycle.jsonl`,
  `annotations.jsonl`, `chunks.jsonl` and `events/events.jsonl`. V11 is the
  numbered form of that existing rule, strengthened to define torn tails
  explicitly and to reject trailing bytes in a finalized package. The prior claim
  that "v1 never stated it" was wrong.
- **V14 history corrected.** v1 §12.2 already defines `record_sha256`
  verification — parse, remove `record_sha256`, canonicalize, hash, compare. V14
  promotes that existing requirement into the numbered matrix and gives it its
  explicit pre-seal role. The prior claim that v1 "never required a reader to
  check" the values was wrong.
- **V13 ancestry clarified.** R34 is the ancestor of V13's closed-filesystem
  direction only. The requirement that the physical `schemas/` set equal
  **exactly** the schema ids the sealed events log references is a v2
  strengthening R34 did not enforce.
- **V12 wording tightened** in the same pass, for accuracy rather than by
  request: v1 verified by re-canonicalizing from the parsed object, which
  tolerated a non-canonical on-disk spelling that re-canonicalized to the same
  content. V12 removes that latitude. The earlier phrasing ("v1 required
  canonical bytes only between two copies") described the mechanism wrongly.

**Counts unchanged:** v1 **34** (R01–R34) → v2 **14** (V01–V14); 17 removed
outright, 17 surviving consolidated into 11, 3 with no R01–R34 ancestor.

**Next:** CL-002B-R2 is unlocked. CL-003 remains locked.

### Proposed — CL-002A-R3-R1a: documentation consistency cleanup (documentation only)

**Status: PROPOSAL. Not approved.** No architecture, authority, recovery
semantics, minimality decision, draft record or scientific scope was reopened.
`DECISIONS.md` is untouched; production code and tests are unchanged.

- **The v1 baseline was wrong.** Every v2 document quoted "35" v1 relations. The
  normative v1 matrix (`CHUNK_EQUIVALENCE.md` §3) has always contained **34**,
  R01–R34, as the CL-002B-R1-C3 changelog entry itself records. The figure was
  introduced by CL-002A-R3 and repeated four times. Corrected everywhere.
- **The counts did not reconcile.** "35 → 14, 30 removed, 4 added" cannot sum,
  because "removed" and "added" were counted in prose phrases from the
  disposition table while the totals counted numbered relations. Replaced with a
  single unit — one numbered relation in a normative list — and a per-relation
  mapping in `PACKAGE_INTEGRITY_V2.md` **§4.1**: of v1's 34, **17 are removed
  outright** and **17 survive, consolidating into 11** v2 relations; **3** v2
  relations have no **R01–R34** ancestor (V11, V12, V14). `17 + 17 = 34`;
  `11 + 3 = 14`.
- **Stale "9" removed.** `PACKAGE_INTEGRITY_V2.md` still said "It needs 9" and
  `V1_TO_V2_SIMPLIFICATION.md` still reported v2 = 9, both left over from a draft
  whose list ended at V09. Neither matched the list shipped in the same commit.
- **V10 and V13 reclassified as reformulations, not additions.** Both are
  remaining directions of v1's R34 inventory bijection, restated now that v2
  persists no flat inventory. R34 is the one v1 relation that fans out (into V04,
  V10 and V13) rather than merging.
- **The two §3 physical conformance contracts** — Arrow schema equality and
  observation row semantics on read — are now explicitly labelled as a different
  counting unit and are deliberately not among the 14.
- **The DAG claim is scoped.** "Every byte in the sealed package is reachable
  from `manifest.sha256`" now reads "every byte **within the manifest integrity
  scope**", since `annotations.*`, `logs/` and `data/derived/` are outside the
  DAG by design and carry their own mechanisms.
- **D27–D32 → D27–D34** where a document refers to the current complete draft
  set. References describing what an earlier round contained are left as
  historical record.
- One malformed Markdown table row in `SESSION_SCHEMA_V2_PROPOSAL.md`
  (`payload_ref.length`, two cells in a three-column table) repaired; wording
  unchanged.

### Proposed — CL-002A-R3-R1: pre-seal durability and closure authority (documentation only)

Human review of R3 returned **GO WITH REQUIRED CHANGES**. The simplification
direction is accepted; five narrow corrections were applied before freeze. Still
docs-only: no production code, no test, no `DECISIONS.md` change.

- **R1-1 — stream closure is now durable before the manifest.** R3 gave
  `manifest.stream_close_status` authority over terminal closure, which meant
  the fact lived only in RAM until the final seal: a crash after the terminal
  lifecycle record but before `manifest.json` lost every per-stream status.
  Replaced by `raw/<stream_id>/stream_close.json`, written once and immutable,
  sealed through `control_sha256`. `RECOVERED_UNCLEAN` added to the status
  domain as an operational observation, never mapped silently to `FAILED` or
  `DISCONNECTED`. **The v2 manifest now owns no semantic stream fact at all.**
- **R1-2 — `record_sha256` retained in `lifecycle.jsonl` and
  `events/events.jsonl`.** R3 removed them on consistency grounds; review
  reversed that, correctly. Before a manifest exists there is no whole-file
  seal, and recovery must read exactly those logs to learn what durably
  happened. A per-record hash there is integrity metadata protecting the sole
  authority, not a competing one. `chunks.jsonl` still drops its self-hash.
  Final disposition: **chunks removed, lifecycle retained, events retained,
  annotations retained.**
- **R1-3 — interrupted finalization is resumable.** Explicit crash-window table
  and finalization order. A package with a durable terminal lifecycle record but
  no manifest pair is `INTERRUPTED_FINALIZATION` — not `UNREADABLE`, and not
  automatically `RECOVERED_UNCLEAN`. Recovery may complete the interrupted
  sealing transaction without altering the outcome already written; that is not
  promotion to `COMPLETED`. Contradictory durable state reports `BLOCKED`.
- **R1-4 — the package layout is closed.** The file set is exhaustively defined
  for the root, `events/`, `schemas/` and `raw/<stream>/`. An unexpected
  immutable file invalidates a sealed package even if it is simply absent from
  `control_sha256`, and the `schemas/` set must equal the schema ids the sealed
  events log references. Enforced inside conditions 2 and 7 — **still eight
  conditions**.
- **R1-5 — canonical means canonical on disk.** Re-canonicalizing a parsed
  document must reproduce the physical bytes exactly. "It parses and would
  canonicalize to the same content" is not sufficient: one record must have one
  spelling.

Draft decisions **D33** (per-stream durable closure authority) and **D34**
(pre-seal logs retain local record integrity) added. D27–D32 revised where
affected. All remain drafts.

Relation count reconciled against the normative lists, in one unit. v1 has
**34** numbered relations (R01–R34); v2 has **14** (V01–V14). Of the 34, **17 are
removed outright** and **17 survive, consolidating into 11**; **3** v2 relations
have no **R01–R34** ancestor (V11, V12, V14). `17 + 17 = 34`, `11 + 3 = 14`.
R01–R34 is the numbered v1 matrix, not an inventory of every invariant the
approved v1 specification states. None of the
additions is a relation between two persisted copies. Two further checks were
added as §3 physical conformance contracts and are deliberately not counted among
the 14. `PACKAGE_INTEGRITY_V2.md` §4.1 names every relation on both sides.
Earlier drafts reported "35 → 14, 30 removed, 4 added"; that baseline was off by
one and mixed counting units, and did not sum.

### Proposed — CL-002A-R3: Session Package v2 simplification (documentation only)

**Status: PROPOSAL. Not approved.** `DECISIONS.md` is untouched; production code
and tests are unchanged. Draft decision records D27–D32 await human approval.

**Why the design was reopened.** v1 survived five adversarial review rounds and
never converged, because each round found a *different* independently falsifiable
representation of the same fact: manifest ↔ filesystem (R1), sidecar ↔ chain
(C1), parsed model ↔ canonical bytes and summary ↔ physical rows (C2), byte
frame / row set / list (C3), Arrow schema and row semantics (C4). That is a
recursion with no floor, because v1 persists the same fact in several places by
design. v2 removes the duplicates instead of enforcing them.

**New documents**

- `docs/SESSION_SCHEMA_V2_PROPOSAL.md` — the complete v2 data contract,
  `schema_version = "2.0"`, specific enough to implement without inventing
  semantics.
- `docs/PACKAGE_INTEGRITY_V2.md` — authority by fact, the integrity DAG,
  physical conformance contracts, and **11** referential relations where v1
  needed 34. (Revised to 14 by CL-002A-R3-R1 and reconciled by CL-002A-R3-R1a;
  the "9" first published here never matched the list this commit shipped.)
- `docs/V1_TO_V2_SIMPLIFICATION.md` — per-representation disposition table with
  the integrity relations each removal eliminates.

`docs/CHUNK_EQUIVALENCE.md` is marked **SUPERSEDED FOR V2** and kept intact as
the v1 implementation audit.

**Principal changes proposed**

- Chunk sidecars removed; `chunks.jsonl` is the only persisted commit authority.
- Artifact paths, artifact byte counts, chunk packet ranges and chunk descriptor
  hashes removed — all deterministically derivable.
- `ManifestStream` replaced by a `stream_close_status` map; closure status is the
  only stream fact with no other on-disk home.
- `manifest.inventory` narrowed to control files and turned into a **map**, so
  duplicate paths are structurally impossible; raw artifacts are sealed
  transitively through the chunk chain, hashed once rather than twice.
- Hierarchical integrity DAG: manifest seals control files and stream indexes;
  each index seals its own artifacts.
- **Added deliberately:** Arrow physical schema conformance and observation row
  semantics on read, closing C4's G1 and G2. Neither is a relation between two
  persisted copies.
- `sparse_long` primary key resolved as
  `(packet_seq, sample_index_in_packet, channel_id)`, closing the one open
  specification blocker from C4.

**26 equivalence relations removed, 2 added.** No provenance fact is removed —
every deletion is a copy or a derivable summary. v1 is not to be used for Study
001 acquisition; a v2 reader fails closed on v1 and no migration is designed.

No scientific threshold, minimum data rule, hardware assumption or Focus
semantics was introduced.

### Fixed — CL-002B-R1-C4: leaf-level integrity closure

Previous Codex verdict: **`NO-GO`**, three confirmed finding groups.

| Finding | Attack | Root |
|---|---|---|
| **F1** | payload frame `packet_seq` disagrees with the packet row that references it | byte-framed artifact treated as opaque |
| **F2a/F2b** | all sample rows deleted; duplicate sample key replacing another | row set treated as an artifact, `n_samples` used only as an upper bound |
| **F3a/F3b** | artifact `bytes` lies while SHA is correct; duplicate inventory entry accepted | `bytes` unchecked; list collapsed to a set |

**Common cause.** C3 decomposed *records* into fields but treated three things
as atomic that are themselves multi-field representations — a byte-framed
artifact, a row set, and a list. The enumeration was one level too shallow in
exactly those places.

**Leaf decomposition.** `docs/CHUNK_EQUIVALENCE.md` gains recursive decomposition
of every compound representation, authority for each leaf, collection-type
classification (multiplicity and order are part of the type), and an explicit
threat-model boundary.

**Newly enforced**

- **Artifact `bytes`** alongside `sha256`, for `packets`, `observations`,
  `samples`, `payloads` and every inventory entry. A matching SHA does not
  validate a separate claim about the same file.
- **Payload frame identity** — each frame's own `packet_seq` must equal the
  referencing packet row's, `payload_len` must equal `payload_ref.length`, and
  frames ↔ packet rows is a bijection: no shared frame, none unreferenced. A
  valid CRC proves a frame is intact, not that it belongs to that packet.
- **Dense sample key-set identity** — for `n_samples = N` the keys must be
  exactly `(packet_seq, 0) … (packet_seq, N-1)`, each once, compared as a
  **multiset** so a duplicate cannot mask a missing key. `n_samples = 0` expects
  an empty set.
- **Inventory bijection** — duplicate paths rejected before any set comparison.

**Deliberately not invented:** no observation cardinality or uniqueness rule
(the schema names no observation primary key), and no `sparse_long` cardinality
rule — see the specification blocker below.

**SPECIFICATION BLOCKER — `sparse_long` sample identity.** §9.1 calls
`(packet_seq, sample_index_in_packet)` "the sample primary key" immediately after
defining sparse rows as `packet_seq, sample_index_in_packet, channel_id, value`.
Those are inconsistent for that layout: the pair cannot be the key if a row also
carries `channel_id`. No cardinality or uniqueness rule is enforced for sparse
streams and none was invented; only the unambiguous structural references are
checked. Needs a human decision. No Study 001 stream is sparse today, and dense
streams are unaffected.

**Tests.** L1–L20 in `tests/test_leaf_integrity.py` plus payload-ref leaf and
frame-bijection cases (29). The property sweep grew from 15 to **28 mutation
dimensions**; an audit confirms 50 of 56 dimension × capture-level combinations
apply and **all 50 are caught**, with 6 genuinely inapplicable. 340 total.

**Threat-model boundary, stated explicitly.** This establishes structural,
referential and cross-representation integrity — not cryptographic authenticity.
An actor who coherently rewrites every artifact and all metadata cannot be
distinguished from the original package without signatures or append-only media,
and that is out of scope for Session Package v1.

### Added — CL-002B-R1-C3: chunk representation equivalence matrix

**Why single-attack patching stopped.** Three review cycles produced the same
shape of defect, never the same bug twice: manifest vs missing raw directory,
sidecar vs `chunks.jsonl`, parsed-model equality vs canonical record identity,
asymmetric null handling, then chain ordering and symmetric null. Each fix
closed the reported attack and left an adjacent state of the *same relation*
unenumerated. The failure was never a missing `if`; it was an unenumerated
relation. So this round enumerated them before touching code.

**`docs/CHUNK_EQUIVALENCE.md`** records the 10 representations of a committed
chunk, field-by-field authority (no artifact is universally authoritative — that
assumption caused two of the defects), 34 required relations R01–R34, the
nullability matrix including symmetric-invalid states, the collection/order
matrix, the structural foreign keys actually enforced, zero-chunk semantics, and
what is deliberately not enforced.

**Newly enforced matrix rows**

- **R12 payload key *presence*** — §12.2 requires the `payloads` key omitted at
  `library_decoded` / `synthetic`, "never written as a null". Two records both
  carrying an explicit null agree with each other and both violate the contract;
  pairwise equality is blind to it, so key presence is now checked directly.
- **R19 / R24 / R25 ordering** — `packet_seq` is strictly increasing within a
  chunk and across the chain (§9.1), and `chunk_id` strictly increases along the
  append-only chain. A reversed or swapped chain previously passed because every
  chunk still matched its own artifact. Contiguity is **not** required for
  either: a gap is a device fact for a later ticket.
- **R20–R23 structural foreign keys** — `samples.packet_seq` and
  `observations.packet_seq` must reference a packet in the same chunk, and
  sample indices must fall within that packet's `n_samples`. These follow from
  §9.1's own statements and are structural, not scientific.

**Property-based mutation testing.** A Hypothesis sweep over 15 mutation
dimensions × capture level × chunk position asserts one property: mutate any
single representation so a required relation is violated, recompute every
attacker-controlled hash, and `is_completed` must be false. An explicit audit
confirmed 28 of 30 mutation/capture-level combinations apply and **all 28 are
caught**; the 2 skips are genuinely inapplicable.

**Explicit regressions.** E1–E25 name the high-value attack classes, plus
adjacent-swap, duplicate chunk id, per-position (first/middle/last) forging,
cardinality 0/1/2/3/5, and finalizer parity for ordering and key presence.

**Test-fixture integrity.** The new foreign-key checks caught the C2-7 "consistent
rewrite" fixture, which shifted the packets artifact but not the samples and
observations referencing it. The *fixture* was inconsistent, not the production
code. It was corrected rather than the check weakened.

**Closed from the previous review:** both remaining BLOCKING findings — reordered
chain, and symmetric `"payloads": null` — each with named tests (E12, E7).

311 tests. No scientific completeness rule: zero-chunk streams remain valid and
no minimum chunk, packet, sample or duration criterion exists.

### Fixed — CL-002B-R1-C2: canonical record and physical packet reconciliation

Previous Codex verdict: **`GO WITH REQUIRED CHANGES`**, three BLOCKING findings.

- **B1** — sidecar reconciliation compared parsed `ChunkCommit` models rather
  than the same on-disk record. `chunks.jsonl` omits `payloads` as §12.2
  requires; a sidecar carrying `"payloads": null`, re-sealed with a refreshed
  inventory and manifest, parsed to an equal model and passed.
- **B2** — a forged `chunks.jsonl` plus matching sidecars could lie about
  artifact metadata: `first_packet_seq` / `last_packet_seq` were only ever
  compared manifest ↔ chain, never against the packets artifact, and two
  commits could claim the same artifact paths.
- **B3** — the finalizer preflight checked which chunk ids were present but
  never whether the records matched what the writer committed, so a chain and
  sidecars rewritten under the same ids could seal an immutable `COMPLETED`.

**Shared root cause.** Interpreted representations were compared where the
actual bytes and the actual data were required: parsed models stood in for
on-disk record identity, and summaries were checked against other summaries.

**Canonical full-record comparison.** `ChunkRecordOnDisk` retains the parsed
model *and* the canonical bytes of the complete parsed document, canonicalized
from the JSON — never from `model_dump()`, which would discard exactly the
fields the model ignores. Sidecar ↔ chain identity now compares those bytes, so
an ignored unknown field or an omitted-versus-null key is detected even when the
models are equal.

**Physical packet-derived range.** Each committed chunk's `packets` artifact is
opened and its real `packet_seq` column read. Every chunk's claimed
`first_packet_seq` / `last_packet_seq` is checked against those rows — for every
chunk, not just the stream endpoints, so a forged middle chunk cannot hide.
`ManifestStream` packet range now derives from the physical rows. Artifact paths
must be each chunk's own canonical names and may not be reused across chunks.
`packet_seq` must be strictly increasing within a chunk, which spec §9.1 already
states; consecutiveness is deliberately **not** required, as a gap is a device
fact for a later ticket.

**Finalizer.** The preflight compares writer-memory committed records to the
disk chain by canonical bytes, rejects disk chunk ids the writer never
committed, and applies the same physical packet check before sealing
`COMPLETED`.

**One source of truth.** All of it lives in `storage/stream_state.py`; the
verifier and the finalizer consume it and neither re-derives packet ranges.

**Tests.** 15 in `tests/test_canonical_and_physical.py` — the exact B1/B2/B3
reproductions plus C2-1 through C2-10, including a positive control that rewrites
the packets artifact *and* every dependent summary and asserts the package still
verifies. Every negative test recomputes record hashes, chain links, sidecars,
manifest summaries, inventory and `manifest.sha256`, so none passes because a
hash was left stale. 275 total.

No scientific completeness rule was added: a zero-chunk stream remains valid and
no minimum packet, chunk, sample or duration criterion exists.

### Fixed — CL-002B-R1: manifest / raw referential integrity

**A BLOCKING false-complete.** `manifest.streams` was built entirely from the
writer's in-memory state, while verification iterated the raw directories that
happened to exist. Neither view proved anything about the other, so deleting a
required stream's whole raw directory before finalization produced a package
where **all eight completion conditions returned true** while
`raw/<stream_id>/` did not exist. Reproduced before the fix; 20 of the 23
initial regression tests fail against `ab7ea9c`.

**The fix is one source of truth, not a third view.**
`storage/stream_state.py` derives chunk count, chain head, packet range,
descriptor hash and sidecars from disk, and both the verifier and the finalizer
preflight use it.

*Verifier.* Condition 5 owns semantic required-stream closure — run declares it,
the manifest agrees, the raw directory exists, and it closed `CLEAN` — plus the
rule `manifest.required == (stream_id in run.required_streams)` for every
stream. Condition 7 owns physical integrity: bidirectional manifest ↔ raw set
reconciliation, duplicate ids, descriptor presence / `stream_id` / hash of the
**stored bytes**, per-chunk `descriptor_sha256`, sidecar ↔ `chunks.jsonl`
agreement, and `chunk_count` / chain head / packet range against the actual
chain. No ninth condition was added; `is_completed()` is still all eight.

*Finalizer.* Stream summaries are derived from physical state instead of writer
memory, and sealing `COMPLETED` is refused when required streams, descriptors,
committed chunks, their artifacts or their sidecars are not on disk.

`chunks.jsonl` is now created empty at stream open, so a zero-chunk stream is
structurally identical to any other and "index absent" never has to be
interpreted.

**New verifier findings:** `MANIFEST_STREAM_MISSING_RAW`,
`RAW_STREAM_MISSING_MANIFEST`, `DUPLICATE_MANIFEST_STREAM`,
`REQUIRED_FLAG_MISMATCH`, `MISSING_DESCRIPTOR`, `DESCRIPTOR_STREAM_ID_MISMATCH`,
`MANIFEST_DESCRIPTOR_HASH_MISMATCH`, `CHUNK_DESCRIPTOR_HASH_MISMATCH`,
`CHUNK_COUNT_MISMATCH`, `CHAIN_HEAD_MISMATCH`, `PACKET_RANGE_MISMATCH`,
`MISSING_STREAM_STRUCTURE`, `SIDECAR_MISMATCH`.

**Tests.** 28 in `tests/test_referential_integrity.py` (R1–R15 plus sibling
cases). Every tamper test recomputes record, chain, inventory and manifest
hashes, so a stale hash is never the only defence and each test asks whether an
internally self-consistent but semantically false package can pass. 260 total.

**Codex review: `NO-GO`**, one BLOCKING finding — sidecars were never
reconciled against `chunks.jsonl`, so an added or contradicting
`NNNNNN.commit.json` survived once the inventory was updated and the manifest
re-hashed. Independently reproduced, then fixed in this ticket as a same-class
relationship, with five regression tests. Codex was **not** re-run, to avoid a
review loop; the recorded verdict remains NO-GO and the fix is unreviewed by it.

**No scientific completeness rule was added.** A zero-chunk stream remains
structurally valid; whether it is scientifically usable is a future protocol
decision.

### Added — CL-002B: Session Package v1 implementation

Implements the approved schema (`DECISIONS.md` D8–D26,
`SESSION_SCHEMA_PROPOSAL.md`). No hardware adapter, no analysis, no timing
reconstruction, no Focus logic. **Zero specification deviations.**

**storage/** — `integer_types` (`int64_decimal` / `uint64_decimal`, refusing
JSON Numbers on disk), `canonical_json` (RFC 8785 JCS + the `record_sha256`
procedure), `payload` (PYLD framing + CRC-32C), `safe_paths` (traversal and
symlink refusal), `arrow_schema` (packets / samples / observations),
`observations` (exactly-one-value-column rule), `chunk_writer` (atomic commit +
hash chain), `checksums` (atomic and no-clobber writes), `paths`, `verifier`
(the eight-condition predicate with structured findings), `reader`
(read + replay).

**session/** — `model` (typed on-disk contract, capture-level invariant),
`allocator` (mkdir-first ordering), `lifecycle`, `annotations` (effective
outcome, fail-closed), `writer`, `finalizer`, `registry` (derived SQLite,
rebuildable), `recovery` (reports, never repairs).

**synthetic/** — a deterministic seeded source, scoped to exercising v1.
The asynchronous multi-device recorder is CL-003 and is deliberately absent.

**232 tests**, including 6 property tests, fault injection at every chunk
commit stage, and a regression for every constructed false-complete package.

### Implementation review — CL-002B

Three read-only Codex passes. All findings fixed, each with a regression test.

| Pass | Finding | Sev | Disposition |
|---|---|---|---|
| 1 | Immutable package content overwritable via public APIs (sealed reopen, `run.json`, descriptors, chunk 0 restart) | BLOCKING | Fixed: `SealedPackageError` guards, `atomic_write_new`, `ChunkWriter` refuses an occupied stream |
| 1 | Verifier wrote a temp file inside the sealed package | SERIOUS | Fixed: sealed prefix parsed in memory |
| 1 | `finalize()` could seal `COMPLETED` when not clean | SERIOUS | Fixed: refuses before writing the terminal record |
| 1 | `read_sessions()` could be read as truth | SERIOUS | Fixed: documented as cache, added `query_sessions()`, wired the derived upsert into allocation and finalization |
| 1 | int64/uint64 accepted JSON Numbers on disk | SERIOUS | Fixed: `load_on_disk()` on every disk read |
| 1 | Observation rows unvalidated | SERIOUS | Fixed: enum + exactly-one-value-column checks before write |
| 1 | `hardware_verification` shape mismatch | SERIOUS | Fixed: `HardwareVerification{status, ref}` |
| 1 | Reader did not verify; no minor-version tolerance; replay/synthetic origins unconstrained | MODERATE | Fixed |
| 1 | Unknown git provenance recorded as clean; event payloads unvalidated | MINOR | Fixed |
| 2 | **Extra immutable files added after sealing were invisible** | BLOCKING | Fixed: condition 2 now checks both directions |
| 2 | **A symlink could stand in for a moved raw artifact** | BLOCKING | Fixed: symlinks rejected in sealed content and at seal time |
| 2 | **Annotation files parsed non-strictly** | BLOCKING | Fixed: `load_on_disk()` |
| 2 | Corrupt Arrow raised out of the verifier | SERIOUS | Fixed: `UNREADABLE_CHUNK_ARTIFACT` finding |
| 2 | Path traversal in artifact paths and `payload_ref.file` | SERIOUS | Fixed: `safe_paths`, and `payload_ref.file` must name its own chunk |
| 2 | `ENOSPC` escaped as a bare `OSError` | SERIOUS | Fixed: closes `CLEAN` / `TECHNICAL_FAILURE` with a reason |
| 2 | `recovery.scan` called any manifest pair sealed | SERIOUS | Fixed: requires verifier condition 1 |
| 3 | `raw_ref.packet_seq` written as a JSON Number | SERIOUS | Fixed: typed `RawRef` with `Int64Decimal` |
| 3 | Manifest parsed non-strictly | SERIOUS | Fixed: `load_on_disk(Manifest, …)` |
| 3 | Per-record hashes inside the sealed lifecycle and events regions unverified | SERIOUS | Fixed: `_verify_sealed_jsonl()` in condition 3 |

Pass 2 constructed three packages that reported `is_completed() == True` while
missing or carrying tampered data. Each has a named regression test.

### Notes

- No hardware adapter, BLE, serial, MNE, HRV, feature extraction, timing
  reconstruction, cross-device sync, Focus logic, ML, UI or cloud code exists.
- No scientific threshold was introduced.
- No `D8`–`D26` decision was changed.
- `pyproject.toml`: `pyarrow` added to the untyped-import override; it ships no
  `py.typed`.

### Approved — CL-002A-APPROVAL: Session Package v1 frozen (documentation only)

**Session Package v1 received human approval.** The design from CL-002A,
corrected by CL-002A-R1 and CL-002A-R2, is now authoritative for Study 001.

**Approved design baseline:** `c5e6a9e712cd4a214162bec57d99ea89c33b01e4`

- `docs/DECISIONS.md` now contains the **authoritative decision index**, D8–D26:
  package-as-authority, UUIDv4 identity, Arrow IPC immutable chunks,
  packet/sample/observation separation, timing provenance, raw capture levels
  and the v1 preservation invariant, acquisition provenance, the derived SQLite
  registry, the three-field lifecycle, sealed versus effective outcome, the
  `COMPLETED` creation rule, annotation integrity, the finalization marker, the
  eight-condition predicate (bound by reference, not duplicated), post-seal
  mutability, RFC 8785, exact integer semantics, and the participant / device /
  derived / replay / event records. D4 ("No schema at CL-001") is marked
  **superseded**, not deleted.
- `docs/SESSION_FORMAT.md` no longer presents Q1–Q8 as unresolved. It now
  summarises the approved answers and points to the authoritative
  specification, and keeps R1–R7 with a note that R3 gained the sealed/effective
  distinction.
- `docs/SESSION_SCHEMA_PROPOSAL.md` is marked **APPROVED FOR IMPLEMENTATION**,
  with the explicit rule that a future implementation discrepancy is a bug
  unless a later decision record supersedes it. The §25 review trace is
  preserved deliberately.
- **Lateral `ABORTED` <-> `TECHNICAL_FAILURE` reclassification is explicitly
  rejected for schema v1** (D19). The annotation model stays downgrade-only with
  exactly four permitted transitions. A mis-classified session cannot be
  corrected sideways; that cost is accepted in favour of minimising mutable
  scientific state, and may be revisited in a future schema revision.
- **CL-002B is now unblocked.**

### Notes

- **No production code was written.** No model, writer, registry, adapter,
  canonicalization helper or test was created; those are CL-002B.
- No scientific threshold, band definition, duration or state definition was
  changed or introduced. No timing threshold was frozen.
- No hardware assumption was promoted from assumed to verified. Every device
  claim still reads `assumed` / `unverified`.
- Protocol and hardware-validation questions remain explicitly open, listed in
  `DECISIONS.md` ("Awaiting a named human") and `SESSION_FORMAT.md`
  ("Still open").

### Changed — CL-002A-R2: numeric exactness and final schema invariants (documentation only)

Three narrow corrections to `docs/SESSION_SCHEMA_PROPOSAL.md`. No architecture
reopened, no code.

- **Integer exactness in JSON.** RFC 8785 constrains JSON Numbers to IEEE-754
  doubles, so integers are interoperably exact only within ±(2^53 − 1). Our UTC
  nanosecond timestamps sit near 1.8e18 — about 200x beyond that — and a
  conforming parser silently rounds `1787923530123456789` to
  `…456768`. New §12.2.1 defines the logical types **`int64_decimal`** and
  **`uint64_decimal`**: any field whose *declared domain* is int64/uint64 is a
  canonical decimal **string** in JSON, with exact grammars (no leading `+`, no
  leading zeros, no `-0`, no decimal point, no exponent, no whitespace) and a
  ban on transiting a float. Applies to every JSON/JSONL document in the
  package, not only hashed ones. Deliberately-bounded fields (channel index,
  schema versions, writer config) stay JSON Numbers, and the rule is symmetric.
  **Arrow is unaffected** — raw tables keep native int64/uint64.
  All 13 affected JSON examples in the document were converted.
- **Raw capture invariant.** `raw_capture_level = "transport_payload"` now holds
  **iff** `transport_payload_preserved = true`; `library_decoded` and
  `synthetic` both imply `false`. The "unless a human decision disables it"
  escape is removed for v1: if transport bytes cross our acquisition boundary
  they MUST be preserved. That escape had permitted
  `transport_payload` + `false`, a state in which two reasonable implementers
  would disagree about whether a payload artifact must exist. Chunk shape is now
  a per-level table with exactly one valid shape each.
- **Post-seal mutability.** New §14.1 is the single authoritative list. Inside
  the sealed package exactly three objects may change: `annotations.jsonl`
  (append-only), `annotations.head.json` (atomic replace) and `logs/`.
  Everything else is immutable, listed explicitly. `registry.sqlite` and
  `data/derived/` are mutable but live *outside* the package, and the document
  now distinguishes those two senses of "mutable".

### Fixed

- **Finalization now writes `annotations.head.json`** (step 6 of §14),
  initialised to zero records and excluded from manifest inventory. R1 made a
  missing head file mean INDETERMINATE but never specified writing one, so as
  written **every cleanly finalized session would have failed the completion
  predicate**. Found by the R2 review; regression tests M3 and M4.
- The chunk write sequence no longer writes `payloads/*.part` unconditionally —
  it is now explicitly conditional on `raw_capture_level = "transport_payload"`,
  matching the R2-2 invariant.
- §5.1 no longer calls `annotations.head.json` "the one file in the package"
  mutable after sealing, which contradicted the §14.1 list of three.

### Removed

- The open question *"whether transport payload capture may be disabled when
  bytes are available"*. For Session Package v1 the answer is **no**, fixed by
  the §9.1 invariant. Revisiting it needs a future major schema version.

### Notes

- **No code was written.** CL-002B is not started.
- `docs/DECISIONS.md` remains unchanged; the proposal is still **PROPOSED**.
- No scientific threshold was changed; no hardware assumption was promoted to
  verified.

### Changed — CL-002A-R1: final schema corrections (documentation only)

Three corrections to `docs/SESSION_SCHEMA_PROPOSAL.md` before the design is put
to human approval. No architecture was reopened.

- **Completion vs finalization.** The `manifest.json` + `manifest.sha256` pair is
  now consistently called a **finalization / sealed-package marker** and never a
  completion marker; a cleanly aborted session produces an identical valid pair.
  Added §5.1 defining **sealed** versus **effective** recording outcome, with a
  deterministic algorithm that fails closed on a corrupt `annotations.jsonl`
  rather than falling back to the sealed value. Replaced the inconsistent
  seven/eight-condition predicate with one **eight-condition** predicate that
  evaluates the effective outcome. The permitted annotation transitions are now
  an explicit four-row table; nothing may ever create or restore `COMPLETED`.
- **Raw capture level.** "Transport bytes are always canonical raw" was not
  implementable against a backend that never exposes them. Added
  `raw_capture_level` (`transport_payload` / `library_decoded` / `synthetic`),
  `transport_payload_preserved`, `acquisition.backend` and `decode_boundary`.
  Payload capture is mandatory where payloads are exposed; where they are not,
  that is recorded and never fabricated. `packets.payload_ref` is now nullable.
- **Canonical JSON.** Adopted **RFC 8785 (JCS)** for every hashed record. The
  earlier claim that `json.dumps(sort_keys=True, …)` would make independent
  implementations agree is withdrawn, with the two divergences named (UTF-16 key
  ordering, ECMAScript number formatting). NaN / Infinity / `-0.0` are rejected
  at write time.

- **Annotation log anti-deletion pointer.** Added `annotations.head.json`,
  written at finalization and updated atomically after each annotation, holding
  the expected byte length, record count and head hash. A hash chain proves the
  records present are intact but cannot prove none was removed; without this,
  deleting `annotations.jsonl` silently restored a sealed `COMPLETED`. Every
  annotation now also carries `from` and `to`, and `from` must match the outcome
  in force. Both found by the CL-002A-R1 review.

### Fixed

- Pass-3 required changes **RC1 and RC2 were reported as applied in `eb89ed0`
  but were not written to disk** — an editing script aborted before its write,
  discarding them while the review trace still claimed success. §5 and §14
  shipped with the pre-RC1 wording, leaving the `UNCLASSIFIED` → `COMPLETED`
  path that RC1 existed to close. Both re-applied and extended here; the miss is
  recorded in §25.4 rather than quietly patched.

### Notes

- **No code was written.** CL-002B is not started.
- `docs/DECISIONS.md` remains unchanged; the proposal is still **PROPOSED**.
- No scientific threshold was changed, and no hardware behaviour was promoted
  from assumed to verified.

### Added — CL-002A: session schema design proposal (documentation only)

- `docs/SESSION_SCHEMA_PROPOSAL.md` — Session Package v1 and Session Registry
  design proposal. Answers `SESSION_FORMAT.md` Q1–Q8 and the CL-002A special
  review questions. Directory-per-session packages, Arrow IPC immutable chunks,
  canonical device payload capture, separate packet/sample/observation tables,
  a fully derived SQLite registry, and an eight-condition completion predicate.
- Pointer from `docs/SESSION_FORMAT.md` to the proposal.

### Notes

- **No code was written.** No Python module, schema model, storage writer,
  registry or migration exists. CL-002B is not started.
- **`docs/DECISIONS.md` is deliberately unchanged.** Every design point in the
  proposal is labelled PROPOSED / OPEN — HUMAN DECISION REQUIRED /
  OPEN — HARDWARE VALIDATION REQUIRED / DEFERRED SAFELY. Nothing is decided
  until a human approves it.
- Independently reviewed in three adversarial passes by OpenAI Codex CLI 0.133.0
  (read-only). 4 blocking + 10 serious findings in pass 1, 2 blocking + 3 serious
  plus three constructed false-complete states in pass 2, and 9 required changes
  in the final pass. Final verdict `GO WITH REQUIRED CHANGES`; all 9 applied.
  Trace in §25 of the proposal.
- No scientific threshold, band definition, duration or state definition was
  introduced.

## [0.1.0] — 2026-08-28

### Added — CL-001: repository bootstrap and engineering contract

- Python 3.11 project managed with `uv`; full dependency graph frozen in
  `uv.lock` (D1, D6).
- `src/` layout with the `consciousness_lab` package and a single-command CLI
  (`consciousness-lab version`) that proves the environment, build and console
  script are wired end to end (D2).
- Validation toolchain: pytest, pytest-asyncio, hypothesis, ruff, mypy (strict),
  pre-commit.
- GitHub Actions CI installing the locked environment and running lint, format
  check, type check and tests.
- `AGENTS.md` — the contract every coding agent works under.
- Documentation set: `ARCHITECTURE.md` (module boundaries only),
  `SESSION_FORMAT.md` (requirements and open questions, no schema),
  `TIMING.md` (timing vocabulary, no frozen thresholds), `HARDWARE.md` (planned
  devices, all pending verification), `PHASE0_PROTOCOL.md` (requirements for a
  protocol that has not been supplied), `SAFETY.md`, `OPERATIONS.md`,
  `DECISIONS.md`.
- `data/` working directory, tracked but contents git-ignored (D5).
- `protocols/` and `firmware/` placeholders with scope notes.

### Notes

- No acquisition, session or analysis code exists.
- No hardware adapter has been tested against a physical device.
- No scientific threshold, band definition, duration or state definition has
  been introduced.
