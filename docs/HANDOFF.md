# HANDOFF — Consciousness Lab, Study 001

> **Read this first, then `AGENTS.md`, then the frozen specifications it names.**
> This document is the state of the repository and the ticket queue. It is
> written for whoever — human or agent — picks the work up next.
>
> **Baseline:** `67c76f9` on `main`. CI green. 482 tests.

---

## 1. Where the project actually is

Five layers were declared at CL-001. Three now hold code.

| Layer | State |
|---|---|
| `storage` | **done for v2.** Canonical JSON, exact 64-bit integers, payload framing, Arrow schemas, chunk writer, package layout, verifier. |
| `session` | **done for v2.** Allocator, lifecycle, annotations, writer, finalizer, registry, recovery, and the CL-003 recorder. |
| `acquisition` | **EMPTY.** No device adapter, no BLE, no serial, no firmware. This is where the next work goes. |
| `analysis` | **empty.** Not started. No ticket is open, and none should be until acquisition produces real data. |
| `core` | **not created.** The ticket that needs shared types creates it. |

**Session Package v2 is APPROVED AND FROZEN** (`docs/SESSION_SCHEMA_V2_PROPOSAL.md`,
`docs/PACKAGE_INTEGRITY_V2.md`, `DECISIONS.md` D27–D34). A divergence between the
implementation and those documents is a bug, not a design question. It survived
one architectural redesign, two Codex conformance reviews and three rounds of
human review; nineteen defects were found and closed. Do not reopen it.

**The recorder (CL-003) is done** and carries D35–D39. It runs one thread per
`StreamSource`, funnels everything into the single thread that touches
`SessionWriter`, cuts chunks on packet boundaries from `writer_config`, blocks
rather than drops under back-pressure, and never chooses a `RecordingOutcome`.

**What has never happened: a device has never been connected.** Every row in
`docs/HARDWARE.md` reads *pending verification*, every hardware claim in the
codebase reads `assumed`, and the only `StreamSource` that exists is the
deterministic synthetic generator.

That is the gap the hardware now closes, and it is the whole of the work below.

---

## 2. The contract a device adapter must satisfy

It already exists and is stable. An adapter implements this and nothing else
(`session/recorder.py`):

```python
class StreamSource(Protocol):
    @property
    def descriptor(self) -> StreamDescriptor: ...
    def run(self, sink: PacketSink, stop: threading.Event) -> None: ...
```

- `run` produces `SourcePacket`s into `sink.submit(...)` until `stop` is set.
- Raise `SourceDisconnectedError` to close the stream `DISCONNECTED`; any other
  exception closes it `FAILED`. Returning normally closes it `CLEAN`.
- **Host arrival times are stamped by the source, never later.** Only the source
  knows the moment the packet surfaced to our process. A timestamp taken after
  the packet waited in a queue is a different quantity, and recording it as
  arrival time would be silently wrong forever.

The recorder's threading model is settled (D35): all concurrency lives above
`SessionWriter`. An adapter that needs a lock inside the writer is an adapter
doing something wrong.

---

## 3. Ticket queue

Scope for each is **recovered from the repository, not invented** — the method
CL-003 used. Every claim below cites where the requirement is already written.

### CL-004 — Device characterisation harness  ← START HERE

**Not an adapter.** A harness that connects each physical device and *records
what it actually does*, in the format `docs/HARDWARE.md` §"Recording a
verification" already requires: date, firmware/revision, host OS and library
version, what was measured and how, what was observed including failure modes.

Why this and not the adapter: `HARDWARE.md` §"Open questions" lists five
questions that are **measurements**, not decisions —

- Does each device expose a device-side clock, a counter, both, or neither?
- What is the sustained sample rate under BLE, as opposed to nominal?
- What does a reconnect do to counters and to any device-side time base?
- Which host platforms work? BLE stacks differ and `bleak` abstracts them
  imperfectly.
- Is a hardware synchronisation path required, or is post-hoc alignment enough?

Every adapter design decision depends on those answers, and writing the adapter
first means guessing them and then encoding the guess in immutable data.
`TIMING.md` makes the sharpest version of the point: *BrainFlow producing a
timestamp column does not establish that the timestamp came from the device.*
The harness is how you find out which it is.

Deliverables:

- `acquisition/probe.py` — connect, stream for a bounded window, and report raw
  observations. It writes **no session package** and makes **no claim**.
- A CLI entry point (`consciousness-lab probe <device>`), following `cli.py`.
- A report template matching `HARDWARE.md`'s required fields, so the output can
  be pasted into the verification log with nothing added.

Constraints: no session package written; no `assumed` promoted to `verified` by
code; no threshold, tolerance or quality criterion introduced. The harness
reports numbers; a human reads them.

**A human must run it.** The agent builds it; Erandi connects the devices and
pastes the output back. That output then updates `HARDWARE.md` — which is a
human edit, dated, per that document's own rule.

### CL-005 — Muse S Athena adapter

`StreamSource` over `brainflow`, designed **from CL-004's findings**, not from
vendor documentation. `HARDWARE.md` records that the board identifier, channel
layout, sample rate and which timing quantities are device-provided versus
host-synthesised are all currently unconfirmed. The descriptor this adapter
emits must record, per channel and per timing quantity, whether the value is
device-provided or host-derived — the `HardwareClaim` / `ClaimStatus` machinery
in `session/model.py` exists precisely for that and is currently all `assumed`.

Capture level is a real decision, not a default: `raw_capture_level` is
`transport_payload` only where the boundary genuinely exposes device bytes,
`library_decoded` where BrainFlow decoded before us (D13, v2 §13). Choose it
from what CL-004 observed.

### CL-006 — Polar H10 adapter

Same shape. Note what `HARDWARE.md` already flags: `polar-python` is a
third-party community library, not a Polar product, and its maintenance status,
stream coverage and timing fidelity are unverified. Whether the study needs
streams it does not expose is an open question — answer it in CL-004, not here.

### CL-007 — Marker channel: QT Py firmware and adapter

`HARDWARE.md`: *no firmware exists in `firmware/`. The marker mechanism, its
electrical interface, and how a marker is placed on a common timeline with the
BLE streams are undesigned.* This ticket designs all three, and it is the one
where the answer to "hardware sync path or post-hoc alignment?" becomes load-
bearing. Do not start it before CL-004 has measured serial round-trip latency
and its variability.

> ### Gate amended (CL-007-A, 2026-09-10) — see `DECISIONS.md` D42
>
> The original gate is kept above because it was right about the thing that
> matters. It was, however, **unsatisfiable as written**: `probe serial` measures
> a round trip by sending `P <token>` and awaiting `R <token>`, so it needs a
> device that replies. No firmware, no measurement, and the gate could never
> open.
>
> Split in two, and only the first half moved:
>
> - **The instrument** — firmware that replies, and the probe that times it — is
>   a *prerequisite for* the measurement, not a consequence of it. Authorised by
>   Erandi on 2026-09-10, who asked for the firmware and the wiring directly.
> - **The design** — the marker mechanism, its electrical interface, and how a
>   mark is placed on a BLE stream's timeline — stays gated exactly as written,
>   on a real measurement existing.
>
> **No physical measurement has been taken.** Nothing merged under CL-007-A may
> be read as having opened the gate: the firmware has never been flashed, `probe
> serial` has never been run against a board, and every timing number about this
> device remains unknown.
>
> An untested proposal for the alignment mechanism exists — strike a device with
> the marker's button, and use a cough as a cross-device check. It is recorded as
> **open questions** in `HARDWARE.md`, deliberately not as a design, and this
> ticket does not evaluate it. Doing so needs the bench evidence this gate is
> about, plus the human-reserved decision on any protocol that rests on it.

### CL-008 — Multi-device session end to end

All three sources under the recorder concurrently, producing a real Session
Package v2 that passes the eight-condition predicate. `HARDWARE.md` warns that
*concurrent connections to two BLE peripherals from one host adapter, and the
effect on packet-arrival jitter, are unmeasured* — so this ticket is partly a
measurement too, and its findings go back into the verification log.

This is also the first ticket that **needs** `run.required_streams` to be a real
set. See §4.

### Later, and explicitly not now

`analysis/` stays empty. No Focus definition, no bands, no epochs, no scoring.
The structural rule in `ARCHITECTURE.md` is not negotiable: **acquisition knows
nothing about interpretation.** A ticket that touches both layers is two tickets.

---

## 4. What only a named human can answer

`DECISIONS.md` §"Awaiting a named human" is binding: **nothing in that list may
be answered by a coding agent** (`AGENTS.md` §6). Having the devices in hand
changes some of it and none of the rest.

**Now answerable, because the hardware exists** — but by measurement plus a
human decision, never by an agent:

- Which streams are `required` for each protocol or session type. This is the
  one item the list itself flags as *gating a working `Finalizer`* (D22): the
  completion predicate cannot mean anything until this set is real. It is
  currently configuration read from `run.json`, deliberately with no default,
  and code fails closed rather than choosing. **CL-008 is blocked on it.**
- Every row of `HARDWARE.md`'s status table and open-questions list — through
  CL-004's output, transcribed by a human with a date.

**Still blocked, and hardware does not help:**

- The Phase 0 protocol in full.
- Any signal-quality, artefact or impedance criterion.
- Any frequency band, epoch length or baseline window.
- Any operational definition of Focus.
- Any timing tolerance, drift budget or jitter limit. CL-002A froze *how the raw
  quantities are preserved*, not what counts as acceptable.
- Consent, retention, withdrawal and access policy; retention period, backup
  cadence, offsite location.
- Withdrawal versus raw immutability — these are in direct conflict and no
  approval has resolved it.
- Whether `UNCLASSIFIED` sessions are excluded from analysis.
- The annotation tampering threat model (D20).

If a ticket appears to need one of these, that is the finding. Stop and report
it as a **SPECIFICATION BLOCKER**; do not invent a value.

---

## 5. Non-negotiables

Carried by every ticket in this repository so far. They do not lapse.

- **Raw data is immutable.** No acquisition package is ever mutated or migrated.
- **Acquisition contains no interpretation.** No Focus semantics, anywhere.
- **No live neurofeedback and no EEG-band feedback.**
- **No invented scientific thresholds.** No minimum packets, samples, duration or
  amplitude; no quality criterion. Structural validity is not scientific
  validity (v2 §14).
- **Hardware behaviour stays explicitly unverified** until `HARDWARE.md` says
  otherwise, with a date and a method. Code may not promote a claim.
- **Completed, aborted and technical-failure sessions are all preserved.**
- **A durable claim must be true when it is written.** Raw is immutable, so a
  false record is permanent. This is the lesson of the last review round: seven
  defects, all of them a writer asserting something before checking it.
- **No speculative architecture.** Build what the specification says.

---

## 6. How this repository has been worked, and why it converged

Not style — this is what took v1 from five failed review rounds to a v2 that
passed clean.

1. **Recover scope from the repository; do not choose it.** CL-003 defined
   itself by quoting the four places that named it. Do the same.
2. **Verify before claiming.** Every review finding in this project was
   reproduced with a script before it was accepted, and several turned out to be
   partly wrong. Never relay a finding you have not reproduced.
3. **Test against physical bytes, then restore every hash a forger could
   restore.** A check that only survives because a digest went stale is not a
   check. `tests/conftest.py` has `rewrite_chunk_chain` and `reseal_manifest`
   for exactly this, and one test asserts the reseal helper is a no-op on an
   untouched package, so the premise itself is proven.
4. **One definition of each rule, shared by every caller.** The writer validates
   chunks with the same `reconcile_chunk` the verifier uses; recovery and
   verification share `check_manifest_contract`. Two almost-identical
   definitions is the defect class this design exists to remove.
5. **Adversarial review, scoped, and aimed where the last one was not.** The
   first Codex pass covered the verifier and found five defects. The second was
   aimed deliberately at the *write* side and found seven more. Untested surface
   right now: **concurrency, and the CL-003 recorder.** `writer.py` states that
   "concurrency that reaches this file is a defect in the caller" — nobody has
   attacked that claim.
6. **Escalate rather than iterate.** When successive rounds keep finding new
   instances of one defect class, the defect is architectural. That rule is what
   produced v2 instead of a sixth patch.

---

## 7. Gates

Every ticket ends with all four green, and CI must pass:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Merging to `main` deploys nothing, but `main` is the record. Do not force-push
over someone else's work: two lines of development have already run in parallel
here, and the correct resolution was a rebase, not a discard.
