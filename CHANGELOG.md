# Changelog

All notable changes to this project are recorded here. Structural decisions and
their rationale live in [`docs/DECISIONS.md`](docs/DECISIONS.md); this file
records what changed and when.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning policy is unresolved — see `docs/OPERATIONS.md`.

## [Unreleased]

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
