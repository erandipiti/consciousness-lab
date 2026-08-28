# ARCHITECTURE

Scope of this document at CL-001: **module boundaries only.** It says where
code is allowed to live and what each area may and may not know about. It does
not design components that have not been ticketed.

## Status

Nothing described below is implemented. `src/consciousness_lab/` currently
contains the package marker and a single-command CLI. The boundaries are
declared now so that the first code to arrive lands in the right place.

---

## The one structural rule

**Acquisition knows nothing about interpretation.**

Code that talks to a device, receives samples and writes them to disk must not
import, call, or depend on anything that classifies, scores, labels or reasons
about mental state. Not for display, not for a quality indicator, not for a
"just informational" readout.

This is the boundary that keeps Study 001 honest: if acquisition cannot see an
interpretation, an interpretation cannot influence what gets recorded, and it
cannot leak back to the participant.

## Layers

Dependencies point downward only. A layer may import from the layers below it,
never above.

```
  analysis          offline, reproducible, reads raw + derived, writes derived
  ─────────────────────────────────────────────────────────────────────────────
  session           session lifecycle, allocation, metadata, run records
  ─────────────────────────────────────────────────────────────────────────────
  storage           writing raw, writing derived, immutability enforcement
  ─────────────────────────────────────────────────────────────────────────────
  acquisition       device adapters, streams, timing capture
  ─────────────────────────────────────────────────────────────────────────────
  core              shared types, timing vocabulary, config, logging
```

### core

Shared primitives: configuration (pydantic-settings), structured logging, and
the timing types defined in `TIMING.md`. Depends on nothing in this package.

### acquisition

Device adapters — one per device family — plus the transport code beneath them.
Responsibilities: connect, stream, capture every timing quantity the device
makes available, hand samples to storage.

Explicitly forbidden here: filtering for anything other than transport
correctness, band computation, state classification, feedback of any kind, and
any threshold that carries a scientific claim.

Each adapter's real-device status is tracked in `HARDWARE.md`, not here.

### storage

Writes raw streams; writes derived artefacts to a separate location. Owns the
immutability guarantee: raw files are written once and never reopened for
write. Owns the link from a derived artefact back to the raw input that
produced it.

### session

The lifecycle of a recording session and its metadata. This layer must
eventually preserve **completed**, **aborted** and **technical-failure**
sessions alike — an aborted or failed session is a record, not a deletion. How
that is represented is open; see `SESSION_FORMAT.md`.

### analysis

Offline only. Reads raw and derived data, produces derived outputs. Must be
re-runnable from a locked environment plus raw inputs, with no dependency on
the machine that did the recording. This is the only layer where
interpretation may live, and only under the constraints in `AGENTS.md` §6.

## Non-Python areas

- `protocols/` — session protocol definitions and operator-facing material.
  Not importable Python; the package must not read from it implicitly.
- `firmware/` — microcontroller source for the QT Py marker device. Built and
  flashed independently of the Python package.

## Out of scope at CL-001

Not designed, not stubbed, not directory-reserved: machine-learning pipelines,
iOS or any mobile client, web services, task runners, plugin systems,
message buses, and any component identified only by a future ticket number.
When such a ticket is opened, its architecture is designed then — with the
information available then.
