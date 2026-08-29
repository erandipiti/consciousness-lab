# SESSION_FORMAT

**Status: Session Package v1 design is APPROVED.**

**Authoritative detailed specification:**
[`SESSION_SCHEMA_PROPOSAL.md`](SESSION_SCHEMA_PROPOSAL.md).

**Approved design baseline:** `c5e6a9e712cd4a214162bec57d99ea89c33b01e4`
(CL-002A + CL-002A-R1 + CL-002A-R2; human approval recorded in
CL-002A-APPROVAL).

**Decision index:** [`DECISIONS.md`](DECISIONS.md) D8–D26.

This document is now a **summary and a pointer**. It does not duplicate the
schema: where implementation detail is needed, the proposal governs. Its earlier
role — holding the requirements and the open questions that preceded the design
— is preserved below, with the questions answered rather than deleted.

---

## Requirements (unchanged, and now satisfied by the approved design)

**R1 — Raw is immutable.** Raw streams are written once. No later process edits,
re-encodes or truncates them. Anything derived is a separate artefact.

**R2 — Derived artefacts are traceable.** Every derived artefact records which
raw inputs produced it, and enough environment identity to re-run the
derivation.

**R3 — All session outcomes are preserved.** A session that was aborted, or that
failed technically, is kept, with its outcome recorded and distinguishable.
Deleting a failed session destroys the denominator. The three outcomes that must
be distinguishable are **completed**, **aborted** and **technical failure**.

> The approved design adds a distinction R3 did not anticipate: the **sealed**
> recording outcome versus the **effective** one, after post-seal downgrade
> annotations. Preserving all three outcomes means preserving that distinction,
> not just the three enum values. See `DECISIONS.md` D17.

**R4 — Timing provenance survives.** Every timing quantity captured at
acquisition time is preserved separately and remains attributable to its source.
A reconstructed time never replaces the values it was reconstructed from.

**R5 — A session is self-describing.** A session read years later, by someone
who was not present, carries what is needed to interpret it.

**R6 — No interpretation is stored as if it were measurement.** Any labelled or
scored quantity is marked derived, with the procedure and parameters that
produced it. Raw and interpretation never share a namespace.

**R7 — Participant identity is not stored in the raw data path.**

## Approved answers to Q1–Q8

| Q | Question | Approved answer | Detail |
|---|---|---|---|
| **Q1** | Container format | **Directory per session**, `data/sessions/<session_id>/`. The package is authoritative; the registry is a derived index | §3, D8 |
| **Q2** | Serialisation | **Arrow IPC stream** for raw chunks (not Parquet — a truncated IPC stream still yields every complete batch); length-prefixed binary for transport payloads; JSONL for events, lifecycle, annotations and the chunk index; JSON canonicalized per **RFC 8785** where hashed; SQLite for the registry; Parquet for derived only | §9, §12.2, D10, D24 |
| **Q3** | Session identifier | **UUIDv4**, opaque, no embedded chronology. Uniqueness enforced by `os.mkdir`. Chronology lives in explicit timestamp fields | §4, D9 |
| **Q4** | Metadata set | Split by **when facts become known**: `allocation.json` (immutable after allocation), `run.json` (sealed at recording start), `raw/<stream>/descriptor.json` (sealed at stream open). One authority per question | §7, D8 |
| **Q5** | Event and marker representation | One shared `events/events.jsonl`. `raw/` holds what devices sent; `events/` holds what the system and operator did. A hardware marker is **both** — raw data plus a semantic event that *references* it, never a copy. Payload schemas versioned per-schema and snapshotted into the package | §11, D26 |
| **Q6** | Partial and failed sessions | Same structural shape, plus `closure_condition` and `recording_outcome`. Absence of a `manifest.json` + `manifest.sha256` pair is the signal. Recovery **reports and never guesses**: a crash closes `RECOVERED_UNCLEAN` / `UNCLASSIFIED`, while a *known* fault closes `TECHNICAL_FAILURE` with a reason | §5, §14, D16 |
| **Q7** | Participant linkage | Generated pseudonym matching `^P[0-9]{3,6}$`; mapping kept outside the repository and outside the data tree; device aliases, never serials | §18, D26 |
| **Q8** | Storage and retention | The canonical unit is the whole `sessions/<id>/` directory. It is independently interpretable, and the registry is rebuildable by scanning packages. **Retention period, backup cadence and offsite location remain undecided** | §15, §19, D8, D15 |

## Still open

These are **not** frozen by the approval. Do not treat any of them as settled.

### Protocol and human decisions

- Retention period, backup cadence, offsite backup location.
- Withdrawal or deletion policy versus raw immutability — in direct conflict,
  unresolved.
- The Phase 0 protocol itself (`PHASE0_PROTOCOL.md` holds no procedure).
- **Which streams are `required`** for each protocol or session type. The
  completion predicate depends on this set, so it is the item that gates a
  working finalizer.
- Whether `UNCLASSIFIED` sessions are categorically excluded from analysis or
  handled by protocol-specific rules.
- The annotation tampering threat model (`DECISIONS.md` D20).

### Hardware validation

Every one of these is unmeasured, and the schema deliberately preserves the raw
information needed to answer them later rather than assuming an answer:

- The Muse S Athena acquisition backend — BrainFlow versus direct BLE or an
  alternative route — and the `raw_capture_level` available in practice.
- Device timestamp semantics: what each device time actually refers to.
- Whether BrainFlow's timestamp is device-provided or host-synthesized.
- Which packet and sample counters the Athena exposes.
- Counter widths and wrap behaviour.
- Actual sustained sample rates under BLE with two peripherals connected.
- Packet-loss behaviour.
- Polar H10 timing behaviour.
- QT Py serial timing and latency behaviour.

## What implementation owes this document

Nothing further. CL-002B implements `SESSION_SCHEMA_PROPOSAL.md` as approved. A
divergence between the implementation and that specification is a **bug**,
unless a later record in `DECISIONS.md` explicitly supersedes it.
