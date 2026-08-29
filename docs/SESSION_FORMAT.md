# SESSION_FORMAT

**Status: no schema is defined.** This document records what the session
format must satisfy and what is still undecided. The schema itself is CL-002
work. Nothing here should be read as a field list.

> A design **proposal** answering R1–R7 and Q1–Q8 now exists at
> [`SESSION_SCHEMA_PROPOSAL.md`](SESSION_SCHEMA_PROPOSAL.md) (CL-002A, corrected
> in CL-002A-R1). It is a proposal, not a decision: `DECISIONS.md` is unchanged
> and this document's questions stay open until a human approves it.
>
> Note for R3 in particular: the proposal distinguishes the **sealed** recording
> outcome from the **effective** one, after post-seal downgrade annotations.
> Preserving all three outcomes means preserving that distinction, not just the
> three enum values.

Writing a schema before these questions are answered would freeze guesses into
the data contract, and the data contract is the one thing that cannot be
cheaply changed later — every recording made under it inherits it.

---

## Requirements (decided)

These constrain any schema that CL-002 proposes.

**R1 — Raw is immutable.** Raw streams are written once. No later process
edits, re-encodes or truncates them. Anything derived is a separate artefact.

**R2 — Derived artefacts are traceable.** Every derived artefact records which
raw inputs produced it, and enough environment identity to re-run the
derivation (see `AGENTS.md` §5 and the reproducibility requirement in
`README.md`).

**R3 — All session outcomes are preserved.** A session that was aborted, or
that failed technically, is kept — with its outcome recorded and
distinguishable. Deleting a failed session destroys the denominator: without
it, no one can say how often recording failed, and any completion rate computed
later is wrong. The three outcomes that must be distinguishable are at minimum
**completed**, **aborted**, and **technical failure**.

**R4 — Timing provenance survives.** Every timing quantity captured at
acquisition time is preserved separately and remains attributable to its
source. See `TIMING.md`. A reconstructed time never replaces the values it was
reconstructed from.

**R5 — A session is self-describing.** A session read years later, by someone
who was not present, must carry what is needed to interpret it: which devices,
which streams, which code version, which protocol, what happened. What exactly
that set is, is open (see Q4).

**R6 — No interpretation is stored as if it were measurement.** If any labelled
or scored quantity is ever written, it is marked as derived, with the procedure
and parameters that produced it. Raw and interpretation never share a
namespace.

**R7 — Participant identity is not stored in the raw data path.** How
participants are referenced is open (Q7); that raw files are not the place for
identifying information is not.

## Unresolved questions

These are open. Do not answer them by writing code.

**Q1 — Container format.** Options in play include a directory-per-session with
one file per stream, a single container per session, and an MNE-native layout.
Trade-offs: append-during-recording safety, partial-write recovery after a
crash, and whether the format is readable without this package. Undecided.

**Q2 — Serialisation for tabular and event data.** Parquet, Arrow IPC, CSV,
and newline-delimited JSON all have arguments. Undecided; note that `pyarrow`
and `duckdb` are in the dependency plan but their presence is not a decision.

**Q3 — Session identifier.** Scheme, uniqueness guarantee, whether it is
sortable by time, and whether it is derivable offline are all open.

**Q4 — Metadata set.** Which fields are mandatory versus optional, and where
metadata lives relative to the raw streams. Related: how a schema version is
recorded so a reader can tell which contract a session was written under.

**Q5 — Event and marker representation.** Whether markers share one timeline
with samples or live in a parallel stream; how the QT Py marker channel is
reconciled with device streams; what an event's identity is. This is a
semantics question, not a formatting one — see `AGENTS.md` §3.

**Q6 — Partial and failed sessions on disk.** R3 says they are preserved; how a
truncated stream, a mid-session disconnect or an unclean shutdown is
represented is open. Specifically: is a failed session structurally identical
to a completed one plus an outcome field, or a distinct shape?

**Q7 — Participant and session linkage.** How a session is associated with a
participant without putting identity in the data path, and where that mapping
lives. Has consent and retention implications — see `SAFETY.md`.

**Q8 — Storage location and retention.** `data/` is a local working directory,
not a decision about where recordings ultimately live or how long they are
kept.

## What CL-002 owes this document

When the schema is proposed, it should state, for each Q above, either the
decision taken and the alternatives rejected (recorded in `DECISIONS.md`), or
that the question remains open and why it can be deferred safely.
