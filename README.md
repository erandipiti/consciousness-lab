# Consciousness Lab

> **Picking this up?** Read [`docs/HANDOFF.md`](docs/HANDOFF.md) first: it states
> where the project actually is, what is frozen, the ticket queue, and which
> questions only a named human may answer.

Research platform for **Study 001**. This repository holds the acquisition,
session-handling and analysis code for the study, plus the documentation that
defines how that code is allowed to behave.

Current milestone: **CL-007-A — the marker instrument.** Hardware is in hand and
none of it has been connected.

What exists: the locked environment and validation pipeline (CL-001), Session
Package v2 as the acquisition data contract (CL-002A/CL-002B, `docs/DECISIONS.md`
D8–D34), a recorder that drives several stream sources concurrently into one
package (CL-003, D35–D39), a verification harness that records what a device
actually does without interpreting it (CL-004, D40), and the QT Py marker
firmware with the probe that would measure it (CL-007-A, D41–D42).

What does **not** exist, deliberately: any device adapter, any BLE or serial
transport, any reconstructed timing or cross-device alignment, and any analysis.
The recorder is defined against an abstract source and exercised by a
deterministic synthetic one.

**Nothing has been measured.** No device has been connected on any host, the
marker firmware has never been flashed, and every row in
[Hardware status](#hardware-status) still reads *pending verification*. Code
existing is not a device behaving.

---

## Installation from a fresh clone

Prerequisites: [`uv`](https://docs.astral.sh/uv/) and `git`. You do **not** need
a system Python 3.11 — `uv` provisions the interpreter pinned in
`.python-version`.

```bash
git clone <repository-url> consciousness-lab
cd consciousness-lab
uv sync --locked --all-groups
```

That single command creates `.venv/`, installs the exact versions recorded in
`uv.lock`, and installs the project itself in editable mode. `--locked` makes
`uv` refuse to proceed if `uv.lock` has drifted from `pyproject.toml`, so a
successful install is proof that the environment matches the lock.

Verify the install:

```bash
uv run consciousness-lab version
```

## Running tests

```bash
uv run pytest
```

## Lint and type-check

```bash
uv run ruff check .          # lint
uv run ruff format --check . # formatting (drop --check to reformat)
uv run mypy                  # strict type check over src/ and tests/
```

Optionally install the git hooks that run the same checks before each commit:

```bash
uv run pre-commit install
```

## Full local validation

The same four gates CI runs, in order:

```bash
uv sync --locked --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

---

## Study 001 scope (as of CL-001)

In scope for the study:

- Recording raw physiological data from the devices listed in
  [`docs/HARDWARE.md`](docs/HARDWARE.md).
- Preserving that raw data unmodified, with its timing provenance intact.
- Offline, reproducible analysis of recorded sessions.

Explicitly **not** in Study 001:

- **No live neurofeedback.** Nothing in this system feeds a signal back to the
  participant during a session.
- **No EEG-band feedback.** No band power is computed, displayed or acted on in
  real time.
- **No Focus-state interpretation in acquisition code.** Acquisition records;
  it does not classify, score or label mental state. Interpretation, if any,
  happens offline against raw data.
- **No scientific thresholds in this repository** unless a named human
  researcher has supplied them and they are recorded in
  [`docs/DECISIONS.md`](docs/DECISIONS.md) with attribution.

## Hardware status

**No hardware adapter in this repository has been verified against a physical
device.** The Muse S Athena and Polar H10 integrations are planned, not proven.
Every statement about how those devices behave — sampling rates, packet timing,
timestamp semantics, reconnection behaviour — is an assumption until it is
measured on real hardware and recorded in [`docs/HARDWARE.md`](docs/HARDWARE.md).

Treat any hardware-dependent code you find here as unverified unless
`docs/HARDWARE.md` explicitly marks it verified, with a date and a method.

## Repository layout

| Path | Contents |
|---|---|
| `src/consciousness_lab/` | Python package. Module boundaries in `docs/ARCHITECTURE.md`. |
| `protocols/` | Session protocol definitions and operator-facing materials (not Python). |
| `firmware/` | Microcontroller firmware (QT Py marker device). |
| `tests/` | Test suite. |
| `docs/` | Engineering and study documentation. Read before modifying anything. |
| `data/` | Local recorded data. Contents are git-ignored and immutable. |

## For coding agents

Read [`AGENTS.md`](AGENTS.md) before making any change. It is not optional
context; it is the contract.
