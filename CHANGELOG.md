# Changelog

All notable changes to this project are recorded here. Structural decisions and
their rationale live in [`docs/DECISIONS.md`](docs/DECISIONS.md); this file
records what changed and when.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning policy is unresolved — see `docs/OPERATIONS.md`.

## [Unreleased]

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
