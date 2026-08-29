# Changelog

All notable changes to this project are recorded here. Structural decisions and
their rationale live in [`docs/DECISIONS.md`](docs/DECISIONS.md); this file
records what changed and when.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning policy is unresolved — see `docs/OPERATIONS.md`.

## [Unreleased]

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
