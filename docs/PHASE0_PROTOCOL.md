# PHASE0_PROTOCOL

**Status: the Phase 0 protocol is not defined.** This document does not
describe a procedure. It records what a protocol document must contain, and
states plainly that the content has not been supplied.

A protocol is a scientific artefact. It is written by the researcher running
the study, not derived by whoever is writing the code. Any procedure, duration,
trial structure or instruction that appeared here without that provenance would
be invented — which `AGENTS.md` §6 forbids — and would then be indistinguishable
from a real decision to the next reader.

---

## What is known

- Phase 0 is the first phase of Study 001.
- It uses the devices listed in `HARDWARE.md`, none of which are verified.
- It produces sessions, which must be preserved under all outcomes
  (`SESSION_FORMAT.md` R3).
- It involves no live neurofeedback and no EEG-band feedback (`README.md`).

That is the complete set of things this repository currently knows about
Phase 0.

## What must be supplied before code can implement it

Each item below must come from a named human, with a date, and be recorded in
`DECISIONS.md` when it arrives.

- **Purpose.** What Phase 0 is for, and what would make it a failure.
- **Participant flow.** What the participant is asked to do, in order,
  including instructions given verbatim.
- **Structure and durations.** Blocks, trials, rests, and how long each runs.
  No default may be assumed.
- **Conditions.** What varies between conditions, and how condition order is
  determined.
- **Markers.** Which events are marked, what each marker means, and who or
  what emits it. This determines the event semantics in `SESSION_FORMAT.md` Q5.
- **Device configuration.** Which streams are recorded from which device, at
  what settings.
- **Start and stop criteria.** What must be true before recording begins, and
  what ends a session normally.
- **Abort criteria.** What causes a session to be stopped early, who decides,
  and how that is recorded. Feeds the outcome taxonomy in
  `SESSION_FORMAT.md` R3.
- **Technical-failure handling.** What the operator does when a device drops,
  and whether the session is resumed or ended.
- **Quality criteria.** Any signal-quality requirement, and whether it gates
  recording or is recorded for later filtering. Note that a gate implies a
  threshold, and thresholds are supplied, never chosen here.
- **Consent and participant handling.** See `SAFETY.md`.

## Open

- Whether Phase 0 output is analysed at all in Study 001, or only used to
  validate the acquisition path.
- Whether the protocol is versioned independently of the code, and how a
  session records which protocol version it ran under.
- Where the operator-facing materials live in `protocols/` and in what format.

## Note for coding agents

If a ticket asks you to implement Phase 0, and this document is still empty of
procedure, that ticket is blocked. Report it as blocked. Do not fill in a
plausible protocol.
