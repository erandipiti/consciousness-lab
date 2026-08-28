# AGENTS.md — contract for coding agents

This file governs every automated or assisted change to this repository. It
outranks convenience, inference and "this seemed obviously right". If a rule
here blocks you, stop and report the block — do not route around it.

---

## 1. Read before you modify

Before changing anything, read:

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module boundaries.
- [`docs/SESSION_FORMAT.md`](docs/SESSION_FORMAT.md) — what a session is and what
  is still undecided about it.
- [`docs/TIMING.md`](docs/TIMING.md) — the timing vocabulary. Use these exact
  terms; do not coin synonyms.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — what has already been decided, and
  why. Re-deciding a settled question silently is a defect.

For hardware work also read [`docs/HARDWARE.md`](docs/HARDWARE.md); for anything
touching a participant, [`docs/SAFETY.md`](docs/SAFETY.md).

## 2. Work only on your assigned CL ticket

Each change belongs to exactly one CL ticket. Do not implement the next
ticket because it is "already obvious", and do not add abstraction for a
ticket that has not been opened. Unrequested architecture is the failure mode
this rule exists to prevent.

If you find work that is out of scope, write it down in the handoff as a
finding. Do not do it.

## 3. Never silently modify schema or event semantics

A field name, a field type, an enum value, an event name, and the meaning of
an event are all part of the data contract. Changing any of them changes what
past recordings mean.

If a change to the contract is genuinely required:

1. Say so explicitly in the change description.
2. Record the change and its rationale in `docs/DECISIONS.md`.
3. State what happens to data recorded under the previous contract.

A schema change that arrives inside a commit described as a refactor is a
defect regardless of whether the code works.

## 4. Preserve timestamp provenance

Every recorded time value must remain attributable to its source. Do not
collapse distinct timing quantities into one field, do not overwrite a
device-provided value with a host value, and do not normalise timestamps in
place.

Use the vocabulary in `docs/TIMING.md` — host arrival time, device-provided
time, packet/sample counter, reconstructed sample time, offset, clock drift,
packet-arrival jitter, sample-timing uncertainty — and keep the quantities
separate. A derived time is stored alongside its inputs, never instead of them.

## 5. Never overwrite raw data

Raw physiological data is immutable once written.

- Never write to an existing raw file.
- Never re-encode, resample, filter, clip or reorder raw data in place.
- Derived artefacts are new files, in a separate location, with a recorded
  link back to the raw input they came from.
- A processing step that cannot be re-run from raw inputs is not acceptable.

If you believe a raw file is corrupt, report it. Do not repair it.

## 6. Never invent scientific thresholds

You may not originate any of the following:

- Signal quality thresholds, artefact-rejection criteria, impedance limits.
- Frequency band edges, epoch lengths, baseline windows.
- Session durations, trial counts, inter-trial intervals.
- Any numeric criterion that a scientific claim could rest on.
- Any operational definition of a mental state, including Focus.

These come from a named human researcher. When one is supplied, record it in
`docs/DECISIONS.md` with the source and the date. If code needs a value that
does not exist yet, stop and ask — do not pick a plausible default, and do not
copy one from a paper or a library example.

Engineering constants that carry no scientific claim (buffer sizes, retry
counts, timeouts) are fine, but say in a comment why the value was chosen.

## 7. Separate verified hardware behaviour from assumptions

Every statement about a physical device is one of two things, and must be
labelled as such:

- **Verified** — observed on the physical device. Record what was measured,
  how, on what date, with what firmware/SDK version, in `docs/HARDWARE.md`.
- **Assumed** — from a datasheet, a vendor SDK doc, a forum post, or
  inference. Mark it assumed and name the source.

Never promote an assumption to verified because the code appears to work.
Never write a comment or a doc line that implies a device behaviour was tested
when it was not.

## 8. Add tests for behaviour

Any behaviour you add or change gets a test. Prefer tests that pin the
contract (what the data must look like, what the invariant is) over tests that
mirror the implementation.

Hardware-dependent code is tested against recorded fixtures or fakes, never
against a live device in the default test run. A test that requires hardware
must be marked so it can be excluded, and it does not count as verification of
device behaviour — see rule 7.

## 9. Document structural decisions

If your change establishes a boundary, a format, a lifecycle, a naming rule,
or forecloses an alternative that someone might reasonably have chosen, add an
entry to `docs/DECISIONS.md`. Record the decision, the alternatives considered
and why they were rejected. An undocumented structural decision is the thing
that makes the next agent guess.

Distinguish clearly, everywhere you write: **decided** vs **unresolved**. Do
not phrase an open question as though it were settled.

## 10. Before you report done

- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy`
- `uv run pytest`

Report what you actually ran and what it actually returned. If something is
incomplete, say so plainly and say what is blocked.
