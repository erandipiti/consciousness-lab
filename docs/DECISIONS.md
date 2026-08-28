# DECISIONS

Structural decisions, with their rationale and the alternatives rejected. Add
an entry in the same change that makes the decision (`AGENTS.md` §9).

Format for each entry: what was decided, why, what was rejected, and what
would justify revisiting it. Superseded entries are marked superseded, not
deleted — the record of a reversal is as useful as the reversal.

**Scientific values do not belong in this file unless a named human supplied
them.** When one arrives, record the value, the person, and the date.

---

## D1 — Python 3.11, locked with `uv`

**Decided (CL-001).** The project targets Python 3.11 exactly
(`requires-python = ">=3.11,<3.12"`, `.python-version = 3.11`), with all
versions frozen in `uv.lock`.

**Why.** Reproducibility is an acceptance criterion of the study, not a
convenience. A range of interpreter versions is a range of possible numerical
and dependency-resolution outcomes; pinning the series removes that axis. `uv`
was chosen because it locks the interpreter and the dependency graph together
and can provision the interpreter itself, so a fresh clone needs one command
and no system Python.

**Rejected.** Poetry and pip-tools — neither manages the interpreter. An
unpinned `>=3.11` — permits a future 3.12+ resolution to differ silently from
the one the study was run on.

**Revisit if.** A required dependency drops 3.11 support, or a device library
requires a newer interpreter.

## D2 — `src/` layout with a single installable package

**Decided (CL-001).** Code lives in `src/consciousness_lab/`, built with
hatchling.

**Why.** A `src/` layout means tests import the *installed* package, not the
working directory. That makes packaging errors fail in the test run rather than
after deployment — relevant here because analysis must be re-runnable from a
clean install.

**Rejected.** Flat layout — imports the source tree implicitly and hides
packaging mistakes.

## D3 — Layered module boundaries, acquisition isolated from interpretation

**Decided (CL-001).** Five layers with downward-only dependencies; acquisition
may not depend on anything that interprets state. See `ARCHITECTURE.md`.

**Why.** If acquisition cannot reach interpretation, then interpretation cannot
influence what is recorded and cannot leak back to the participant. Enforcing
this as a structural boundary rather than a convention means a violation shows
up as an import, which is reviewable.

**Rejected.** Organising by device instead of by concern — it puts device I/O
and everything built on it in the same namespace and makes the boundary
invisible. Deferring the boundary until there is code to separate — boundaries
are cheap now and expensive after the first violation.

## D4 — No schema at CL-001

**Decided (CL-001).** `SESSION_FORMAT.md` records requirements and open
questions; it defines no fields. The schema is CL-002 work.

**Why.** The data contract is the most expensive thing in the project to
change, because every recording made under it inherits it. Several inputs it
depends on — device timing behaviour, marker semantics, the Phase 0 protocol —
do not exist yet. A schema written now would encode guesses that later readers
could not distinguish from decisions.

**Rejected.** A provisional schema "to be refined later" — provisional
schemas acquire recordings, and then stop being provisional.

## D5 — Raw data is not versioned in git

**Decided (CL-001).** `data/` is tracked as a directory; its contents are
ignored.

**Why.** Raw physiological data is large and binary, and git history can be
rewritten — the wrong custody model for data that must be immutable. Keeping
the directory tracked fixes the layout without implying git is the archive.

**Open.** Where recordings actually live and how they are preserved is
unresolved: `SESSION_FORMAT.md` Q8, `OPERATIONS.md`, `SAFETY.md`.

## D6 — Dependencies declared unpinned in the manifest, pinned in the lock

**Decided (CL-001).** `pyproject.toml` lists dependency names without version
constraints; `uv.lock` holds the exact resolution, and CI installs with
`--locked`.

**Why.** Two files claiming to pin versions is one file too many — they drift,
and then it is unclear which one is authoritative. The lock is authoritative;
the manifest states intent. Constraints will be added to the manifest only
where a real incompatibility is found, and the reason recorded here.

**Rejected.** Pinning exact versions in both — duplicated truth, and every
upgrade becomes a two-file edit with a chance of disagreement.

## D7 — Toolchain smoke tests are kept

**Decided (CL-001).** `tests/test_toolchain.py` exercises hypothesis and
pytest-asyncio even though neither has a real subject yet.

**Why.** Both are configured in `pyproject.toml`. A configuration that is never
exercised is unverified — a wrong `asyncio_mode` would sit silently until the
first async test, and then look like a bug in that test.

**Revisit when.** Real property-based and async tests exist; the smoke tests
can be deleted then.

---

## Awaiting a named human

Recorded here so the gap is visible rather than implicit. Nothing in this list
may be answered by a coding agent (`AGENTS.md` §6).

- The Phase 0 protocol in full — `PHASE0_PROTOCOL.md`.
- Any signal-quality, artefact or impedance criterion.
- Any frequency band definition, epoch length or baseline window.
- Any operational definition of Focus, or of any other state.
- Any timing tolerance, drift budget or jitter limit — `TIMING.md`.
- Consent, retention, withdrawal and access policy — `SAFETY.md`.
