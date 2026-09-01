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

## D4 — No schema at CL-001 — **SUPERSEDED by D8–D26 (CL-002A-APPROVAL, 2026-08-28)**

**Decided (CL-001), superseded 2026-08-28.** The reasoning below was correct for
CL-001 and is kept as the record of why the schema was deferred; the schema now
exists and is approved. Entry retained, not deleted — the record of a reversal
is as useful as the reversal.

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

# Session Package v1 — approved decisions (CL-002A-APPROVAL)

**Human approval recorded 2026-08-28.** These decisions were designed in CL-002A
and corrected in CL-002A-R1 and CL-002A-R2, reviewed adversarially across five
independent passes, and are now **authoritative for Study 001**.

**Approved design baseline:** `c5e6a9e712cd4a214162bec57d99ea89c33b01e4`

**Authoritative specification:** [`SESSION_SCHEMA_PROPOSAL.md`](SESSION_SCHEMA_PROPOSAL.md).
This file is the decision *index*, not the specification. Where implementation
detail is needed, the proposal governs; the section references below are the
binding pointers. An implementation that diverges from the proposal is a bug,
unless a later decision record here explicitly supersedes it.

D4 ("No schema at CL-001") is **superseded** by D8–D26.

---

## D8 — The session package is the authority; the registry is not

**Decided.** A recording session is a **directory**, `data/sessions/<session_id>/`.
That package is authoritative for everything about the session.

**Why.** A directory fails file by file; a single container puts a central
structure in the crash path where one bad write loses everything. And a single
authority removes the class of bug where two stores disagree and nobody knows
which is right.

**Constrains.** The package must stay technically and scientifically
interpretable with `registry.sqlite` deleted. Nothing may be knowable only from
the registry.

**Open.** Nothing.

**Source.** Proposal §3, §6.

## D9 — Session identity is an opaque UUIDv4

**Decided.** UUIDv4, canonical lowercase, as the directory name. Uniqueness is
enforced by `os.mkdir` failing atomically on an existing path.

**Why.** Any identifier carrying a timestamp creates a second chronology that
outlives every promise not to parse it, and a wrong host clock at allocation
would contaminate identity permanently. UUIDv7 has the same defect and is
rejected for the same reason.

**Constrains.** No chronology, participant identity or protocol meaning may be
encoded in a session id. Chronology lives in explicit timestamp fields carrying
their own provenance, and in the derived registry index.

**Open.** Nothing.

**Source.** Proposal §4.

## D10 — Raw continuous data is immutable Arrow IPC chunks

**Decided.** Raw streams are written as immutable chunk files in **Arrow IPC
stream** format. **Parquet is not the canonical acquisition format**; derived
datasets may use it.

**Why.** A truncated IPC stream still yields every complete record batch. A
truncated Parquet file never got its footer and is a total loss. Derived data is
re-derivable, so query speed wins there instead.

**Constrains.** A committed raw chunk is never reopened for modification.

**Open.** Compression codec (LZ4 / ZSTD / none) — a per-chunk property,
changeable without a schema change.

**Source.** Proposal §9, §12.2.

## D11 — Raw streams separate packets, samples and observations

**Decided.** Three tables per stream. Packet-level timing is **not** duplicated
onto every sample row. Device-provided times and counters are **observation
rows** (`name`, `unit`, `clock_id`, `applies_to`, `provenance`, `status`), not
singular generic timestamp or counter columns.

**Why.** Denormalising costs ~44–59 MB per stream-hour at 256 Hz, and — the
expensive part — a packet timestamp copied onto 24 sample rows will eventually
be read as 24 per-sample measurements. Singular columns silently discard a
second device-provided quantity if one exists, and no device has been measured,
so we do not know how many exist.

**Constrains.** No reconstructed timing is stored under `raw/` — not as a
convenience column, not as a cache.

**Open.** How many timing quantities each device actually exposes.

**Source.** Proposal §9.1, §9.2, §9.3.

## D12 — Timing provenance is preserved quantity by quantity

**Decided.** The schema preserves separately: host monotonic arrival time, host
UTC arrival time, device-provided time(s), device counters, packet ordering,
sample position within packet, clock identity, and the provenance of each.

**Why.** `TIMING.md` exists because these cannot be reconstructed once
collapsed.

**Constrains.** Reconstructed sample time, offset, drift, packet-arrival jitter
and sample-timing uncertainty are **derived**, live only under `data/derived/`,
and are never written into `raw/`.

**Open.** Every device-dependent timing fact — see the hardware list below. **No
timing threshold is frozen by this approval.**

**Source.** Proposal §10; `TIMING.md`.

## D13 — Raw capture levels, and the v1 preservation invariant

**Decided.** Canonical raw is *the lowest-level representation actually observed
at our acquisition boundary, preserved without scientific transformation by our
code*. Three levels: `transport_payload`, `library_decoded`, `synthetic`.

For schema v1 the invariant is exact:

```text
transport_payload  <=>  transport_payload_preserved = true
library_decoded     =>  transport_payload_preserved = false
synthetic           =>  transport_payload_preserved = false
```

**If transport bytes reach our acquisition code, they MUST be preserved. There
is no payload-disable exception in v1.**

**Why.** Locking the two fields removes a state in which two implementers would
disagree about whether a payload artifact must exist. Mandating preservation
only at the boundary the bytes actually cross keeps the rule implementable
against a backend that never exposes them.

**Constrains.** If an upstream library decodes before our boundary, the stream
records `library_decoded` and the missing transport bytes are **never
fabricated**. Chunk shape follows the level, with exactly one valid shape each.

**Open.** The Muse S Athena backend and the capture level available in practice
— hardware validation.

**Source.** Proposal §9.0, §9.1.

## D14 — Every stream records its acquisition provenance

**Decided.** Each stream descriptor records backend name, backend version,
adapter version, `raw_capture_level`, `transport_payload_preserved`, a prose
`decode_boundary`, and a verification status.

**Why.** A session read years later must be able to answer where decoding
happened and what was between the device and our recorder.

**Constrains.** Every hardware-dependent claim is a `{value, status, source,
observed_at}` quad. **Today every `status` reads `assumed` and this approval
promotes none of them to `verified`.**

**Source.** Proposal §8, §9.0.1.

## D15 — The registry is SQLite and fully derived

**Decided.** `data/registry.sqlite`, WAL, stdlib `sqlite3` — no new dependency.
It is a derived index, fully rebuildable by scanning session packages.

**Why.** Cross-session queries need an index; truth does not need a second home.

**Constrains.** Allocation order must guarantee the package exists durably
before any registry row is required, so no state exists that lives only in the
database. A registry/package disagreement resolves **in favour of the package**
and must be reported, never silently reconciled.

**Open.** Registry indexes and query surface — derived, changeable at will.

**Source.** Proposal §6, §12.1.

## D16 — Three lifecycle fields, and no guessing

**Decided.** `lifecycle_state`, `closure_condition` and `recording_outcome` are
separate fields.

**Why.** Operational closure and scientific meaning are different facts.
Collapsing them is how false-complete states get built.

**Constrains.** A crashed session is **never** silently classified:
`UNCLASSIFIED` means the outcome is genuinely unknown. A **known** technical
failure — `ENOSPC`, an I/O fault — is recorded as `TECHNICAL_FAILURE` with a
reason, because the process was alive and knew why it was dying.

**Open.** Whether `UNCLASSIFIED` sessions are categorically excluded from
analysis, or handled per protocol.

**Source.** Proposal §5, §12.3.

## D17 — Sealed outcome versus effective outcome

**Decided.** `sealed_recording_outcome` is immutable, inside the hash-protected
prefix. `effective_recording_outcome` is that outcome after applying valid
post-seal downgrade annotations.

**Constrains.** **All user-facing readers, reports, registry rebuilds and
analysis selection use the effective outcome.** The sealed outcome is an input,
never the answer on its own.

**Source.** Proposal §5.1.

## D18 — `COMPLETED` may be created only at clean finalization

**Decided.** `COMPLETED` is creatable only during clean finalization, inside the
sealed lifecycle prefix. No post-seal annotation may create or restore it.

Permitted post-seal transitions in v1 are exactly four:

```text
COMPLETED    -> ABORTED
COMPLETED    -> TECHNICAL_FAILURE
UNCLASSIFIED -> ABORTED
UNCLASSIFIED -> TECHNICAL_FAILURE
```

Anything targeting `COMPLETED` is invalid and must be rejected and reported.

**Why.** Without this, appending one line to a text file promotes an aborted
session to a completed one without invalidating any hash. Adversarial review
constructed that attack against an earlier draft, and constructed it again
through `UNCLASSIFIED` when the first fix was worded loosely.

**Source.** Proposal §5, §14.

## D19 — Lateral outcome reclassification is forbidden in v1

**Decided (human decision, CL-002A-APPROVAL).** Session Package v1 does **not**
allow `ABORTED -> TECHNICAL_FAILURE` or `TECHNICAL_FAILURE -> ABORTED` after
classification. The annotation model stays downgrade-only, with exactly the four
transitions in D18.

**Why.** Minimising mutable scientific and operational state is preferred for
Study 001; the sealed record and its annotation audit trail should stay
conservative; and this flexibility is not required to begin the study.

**Constrains.** A session classified as an operator abort cannot later be
corrected sideways to a technical failure. That cost is accepted knowingly.

**Revisit if.** Experience shows lateral correction is genuinely necessary —
then in a future schema revision, not by widening the v1 rule.

**This closes the open question that stood in `SESSION_SCHEMA_PROPOSAL.md` §22.**

## D20 — Annotation integrity fails closed

**Decided.** Post-seal annotations use `annotations.jsonl` (append-only,
hash-chained) plus `annotations.head.json` (atomically replaced after each valid
append, recording expected chain state).

**Why.** A hash chain proves the records present are intact; it cannot prove
none was **removed**. Deleting or boundary-truncating the log would otherwise
leave a valid shorter chain and silently restore a sealed `COMPLETED`.

**Constrains.** Corruption, truncation, deletion or head mismatch is
`INDETERMINATE` and fails closed. **A reader must never fall back to a sealed
`COMPLETED` when annotation integrity is indeterminate.**

**Open.** The tampering threat model — the head pointer does not defeat an actor
who rewrites both files consistently, and whether that warrants append-only
media or signatures is a study-operations question.

**Source.** Proposal §5.1, §14.

## D21 — The manifest pair is a finalization marker, not a completion marker

**Decided.** A matching `manifest.json` + `manifest.sha256` pair means exactly
one thing: **the package was cleanly finalized and sealed.**

**Constrains.** It is **not** evidence of `COMPLETED`. A cleanly finalized
`ABORTED` or `TECHNICAL_FAILURE` package has an identical, fully valid pair.
Completion is determined only by the full predicate (D22).

**Source.** Proposal §13, §14.

## D22 — The eight-condition completion predicate

**Decided.** `is_completed()` is the eight-condition predicate specified in
**`SESSION_SCHEMA_PROPOSAL.md` §14**, evaluating the **effective** outcome.

**The predicate text is deliberately not reproduced here.** Copying it would
create a second specification that could drift from the first, and a divergent
completion rule is precisely the failure this design spent five review passes
eliminating. §14 governs; this record binds the implementation to it.

**Source.** Proposal §14.

## D23 — Post-seal mutability is a closed list

**Decided.** Inside a sealed acquisition package, exactly three objects may
change: `annotations.jsonl` (append-only), `annotations.head.json` (atomic
whole-file replacement), and `logs/` (non-authoritative operational data).

Everything else is immutable after sealing — `allocation.json`, `run.json`,
every stream descriptor, the sealed prefix of `lifecycle.jsonl`,
`events/events.jsonl`, everything under `raw/`, everything under `schemas/`,
`manifest.json` and `manifest.sha256`.

Outside the package, `registry.sqlite` and `data/derived/` are freely
mutable and rebuildable.

**Constrains.** "Mutable" means two different things on the two sides of that
boundary, and the distinction is load-bearing: inside, it is a narrow verifiable
exception carved out of a sealed unit; outside, it is the normal state of a
cache.

**Source.** Proposal §14.1.

## D24 — RFC 8785 (JCS) for hashed JSON

**Decided.** Every JSON object whose canonical bytes are hashed is canonicalized
per **RFC 8785**. Ordinary `json.dumps(sort_keys=True, …)` is **not** sufficient
and is explicitly rejected as an implementation.

**Why.** JCS sorts keys by UTF-16 code units where Python sorts by code point —
they diverge above the BMP — and JCS mandates ECMAScript number formatting.
Independent implementations would not agree.

**Constrains.** CL-002B implements or vendors a conforming helper and tests it
against the RFC's published vectors. No dependency is mandated. `NaN`,
`Infinity` and `-0.0` are rejected at write time.

**Source.** Proposal §12.2.

## D25 — Exact integer semantics in JSON

**Decided.** Any field whose **declared semantic domain** is `int64` or `uint64`
is represented in JSON/JSONL as a canonical decimal **string**, via the logical
types `int64_decimal` and `uint64_decimal`. Such values must never transit an
IEEE-754 float, not even transiently.

**Why.** RFC 8785 constrains JSON Numbers to doubles, exact only within
±(2^53 − 1). Our UTC nanosecond timestamps sit near 1.8e18, so
`1787923530123456789` written as a Number rounds to `…456768` in any conforming
parser — silently wrong, in values that feed provenance and hashes.

**Constrains.** The rule keys off the declared domain, not the current value, so
representation never shifts when a counter crosses a threshold. Widening a
bounded field to int64 changes its representation and is a **major** version
bump. **Arrow is unaffected** and keeps native exact `int64`/`uint64`.

**Source.** Proposal §12.2.1.

## D26 — Participant, device, derived, replay and events

**Decided**, five smaller records that share one theme — keeping identity and
interpretation out of the raw path:

- **Participant pseudonym** is generated and validated against `^P[0-9]{3,6}$`.
  Real identity never appears in raw paths or session metadata; the mapping
  lives outside the repository and outside the session data tree. (§18)
- **Device identity** uses study-local aliases (`muse-01`). Raw serial numbers
  are never written into a session package. (§18)
- **Derived artifacts** live outside the sealed package at
  `data/derived/<session_id>/<artifact_id>/`, and record enough provenance —
  input hashes, pipeline version, commit, environment — to identify exactly
  which raw bytes and which code produced them. Derived data never replaces or
  modifies raw. (§15)
- **Replay** happens at the storage/read boundary and must not impersonate live
  hardware in a way that erases provenance. Replay-origin packages identify
  their origin explicitly; synthetic sessions record a deterministic generator
  seed. (§16)
- **Events** use one shared `events/events.jsonl`. A semantic event may
  *reference* a raw observation without duplicating the measurement. Payload
  schemas are versioned independently and snapshotted into each package. **No
  Focus-specific semantics are added to the generic acquisition layer.** (§11)

## D27 — Session Package v2 supersedes v1, before any Study 001 acquisition

**Decided (CL-002A-R3-APPROVAL).** Session Package **v2** (`schema_version =
"2.0"`) is the acquisition data contract. Session Package v1 is superseded and
retained as the historical design record; a v2 reader fails closed on a v1
package rather than guessing.

**Why.** v1 survived five adversarial review rounds without converging: each
round found a *different* independently falsifiable representation of the same
fact — manifest ↔ filesystem, sidecar ↔ chain, parsed model ↔ canonical bytes,
summary ↔ physical rows, byte frame / row set / list, Arrow schema / row
semantics. That is a recursion with no floor, because v1 persists one fact in
several places by design. v2 deletes the duplicates instead of enforcing them:
the numbered equivalence matrix goes from **34 relations (R01–R34) to 14
(V01–V14)**.

**Rejected.** A sixth correction round (C5). The escalation rule in the C4
ticket applies: when successive rounds keep finding new instances of one defect
class, the defect is architectural.

**What makes this cheap.** No Study 001 recording exists. The cost of superseding
is zero today and a permanent enforcement burden if deferred.

**Source.** `docs/SESSION_SCHEMA_V2_PROPOSAL.md`, `docs/PACKAGE_INTEGRITY_V2.md`,
`docs/V1_TO_V2_SIMPLIFICATION.md`.

## D28 — `chunks.jsonl` is the only persisted chunk commit authority

**Decided (CL-002A-R3-APPROVAL).** Per-chunk `NNNNNN.commit.json` sidecars are
removed. A chunk is committed **iff** its record is durably present in the
hash-chained log. Any per-chunk index built for performance is derived and
rebuildable, never sealed acquisition truth.

**Why.** The sidecar was a complete second copy of a record that had to stay
canonically identical to the chain forever. It produced two BLOCKING findings on
its own and four of the relations v2 deletes (R01–R04).

**Source.** Proposal v2 §6, §12.

## D29 — A persisted summary must earn an authority or operational role

**Decided (CL-002A-R3-APPROVAL).** A value deterministically derivable from an
authoritative representation is derived at read time, not stored again.

Removed on this basis: artifact paths, artifact byte counts, chunk packet ranges,
chunk descriptor hashes, and every `ManifestStream` summary field.

**Not covered by this rule:** integrity metadata protecting a sole authority — see
D34 — and summaries with a semantic role, which are listed and justified
individually (`lifecycle_seal.sealed_len`, `annotations.head.json`, payload frame
`payload_len`).

**Why.** Each stored summary is a place where two persisted claims about one fact
can disagree, and every one of them must be reconciled by the verifier forever.

**Source.** `docs/V1_TO_V2_SIMPLIFICATION.md`.

## D30 — Hierarchical integrity; a raw artifact hash is persisted exactly once

**Decided (CL-002A-R3-APPROVAL).** `manifest.sha256` seals `manifest.json`; the
manifest seals the control files and each stream's `chunks.jsonl`; each chain
seals its own raw artifacts. Transitive protection is protection.

**Scope.** The reachability claim covers every byte **within the manifest
integrity scope**. `annotations.jsonl` / `annotations.head.json` (written after
sealing), `logs/` (never authoritative) and `data/derived/` (regenerable) are
outside the DAG by design and carry their own mechanisms.

**Why.** v1 hashed raw artifacts twice — in the commit and in a flat manifest
inventory — which is pure duplication with a permanent reconciliation cost.

**Source.** `docs/PACKAGE_INTEGRITY_V2.md` §2.

## D31 — Physical Arrow schema and row semantics are part of package validity

**Decided (CL-002A-R3-APPROVAL).** A matching SHA proves identity, not
conformance. Field names, order, types and nullability are contractual;
unrecognised Arrow **schema metadata** is not. Observation row semantics are
validated by **one shared validator** invoked on both write and read.

**Why.** CL-002B-R1-C4 deleted an entire `values` column from an artifact and the
package still completed; a `uint64` observation carrying `value_f64` verified
clean. Two definitions of "valid observation" is the defect class this removes.

**Source.** `docs/PACKAGE_INTEGRITY_V2.md` §3.

## D32 — `sparse_long` sample primary key

**Decided (CL-002A-R3-APPROVAL).** For `sparse_long`, the sample primary key is
`(packet_seq, sample_index_in_packet, channel_id)`. Dense stays
`(packet_seq, sample_index_in_packet)`. For sparse, `n_samples` means logical
sample positions, not long-format rows, and complete channel coverage is **not**
required.

**Why.** v1 named one sample primary key while sparse rows also carry
`channel_id` — an internal contradiction, and the one open specification blocker
carried out of CL-002B-R1-C4. This resolves it.

**Source.** Proposal v2 §8.

## D33 — Stream closure is a per-stream durable authority

**Decided (CL-002A-R3-APPROVAL).** `raw/<stream_id>/stream_close.json` is the
**sole** persisted authority for terminal stream closure: written once,
immutable, one file per opened stream, sealed by the manifest through
`control_sha256` and never repeated in it. Status domain: `CLEAN`,
`DISCONNECTED`, `RECONFIGURED`, `FAILED`, `RECOVERED_UNCLEAN`.

**Why.** A fact required to resume finalization must not live only in RAM until
the final seal, which is what a `manifest.stream_close_status` map would have
meant: a crash between lifecycle `CLOSED` and the manifest pair would have lost
it. Closure is now durable *before* `FINALIZING` is appended.

**`RECOVERED_UNCLEAN`** records that the process vanished with no durable close
record. It is an operational observation and is **never** silently mapped to
`FAILED` or `DISCONNECTED`.

**Constrains.** The v2 manifest owns no semantic stream fact at all; it is a
finalization marker and an integrity root, nothing else.

**Source.** Proposal v2 §7.1, §9.4.

## D34 — Pre-seal logs retain local record integrity

**Decided (CL-002A-R3-APPROVAL).** `lifecycle.jsonl` and `events/events.jsonl`
retain a per-record `record_sha256`; `annotations.jsonl` retains its own;
`chunks.jsonl` drops it.

**Why.** Those logs must be individually verifiable **before a manifest exists**,
which is exactly when recovery reads them to learn what durably happened. The
whole-file manifest hash serves the distinct post-finalization sealing role.
Complementary layers with different temporal scope, not competing authorities —
so D29 does not apply. `chunks.jsonl` needs no self-hash because its records are
cross-checked against physical artifacts recovery reads anyway.

**Source.** `docs/PACKAGE_INTEGRITY_V2.md` §2, Proposal v2 §6.1.

---

# CL-003 — asynchronous multi-stream recorder

**Decided (CL-003).** Engineering decisions only. None of the entries below is a
scientific value, a threshold, or an operational definition of anything; those
still require a named human (`AGENTS.md` §6) and none was supplied.

## D35 — The recorder fans in to a single writer thread

**Decided (CL-003).** Sources run concurrently, one thread each, and may only
put packets on a bounded queue. Exactly one thread — the one that called
`Recorder.run()` — ever calls `SessionWriter`. Descriptors are opened from that
thread before any source thread starts.

**Why.** `SessionWriter` was written synchronous and single-threaded and its
docstring says so. Making it thread-safe would put locks around the chunk hash
chain, `event_seq`, the lifecycle log and the schema set — four independent
ordering guarantees, each of which becomes a correctness question under
concurrency. Fanning in keeps every one of those guarantees exactly as CL-002B
tested them, and moves all the concurrency to a layer where the worst failure is
a stalled producer rather than a corrupt package.

**Rejected.** A lock inside `SessionWriter` — spreads the concurrency question
across every method that touches durable state, forever. One writer thread per
stream — the events log, the lifecycle log and the schema set are shared, so it
merely relocates the problem. An async event loop — a blocking device library
call would stall every other stream, and the planned libraries (`brainflow`,
`bleak` backends, `pyserial`) are not uniformly async.

**Revisit if.** A verified device requires an ordering the queue cannot express.

## D36 — Apparatus failure ends the session; device failure ends one stream

**Decided (CL-003).** A chunk commit that fails for any reason closes the whole
session `CLEAN / TECHNICAL_FAILURE` through the CL-002B path. A source that
stops abnormally closes only its own stream, and the other streams keep
recording. A stream closes `DISCONNECTED` **only** when the source raises
`SourceDisconnectedError`; every other exception closes it `FAILED`.

**Why.** The two failures have different blast radii. A failed commit can leave
raw artifacts that no commit record names, and such a package can never be
sealed — continuing to record into it would produce data that cannot be
verified. A device going away costs one stream and nothing else; destroying the
other recordings would discard good data over a fact the finalizer already
evaluates. The `DISCONNECTED` / `FAILED` split is not inferred because "the
device went away" and "our code raised" are different claims, and choosing
between them without evidence would put an unverified hardware claim into
immutable data (`AGENTS.md` §7).

**Rejected.** Ending the session on any source failure — throws away good data
from healthy devices. Treating a commit failure as a stream-level fault — leaves
an unsealable package that keeps accepting writes. Inferring `DISCONNECTED` from
a transport-shaped exception — a guess recorded as a device fact.

**Constrains.** The recorder never chooses a `RecordingOutcome` and never calls
`finalize()`. Whether a session with a failed stream is still completable is
decided by the existing eight-condition predicate (D22), not here.

## D37 — Recorder scheduling comes from `writer_config`; nothing new is persisted

**Decided (CL-003).** Chunk boundaries and the periodic clock snapshot are
driven by `run.json.writer_config` — `chunk_max_rows`, `chunk_max_seconds`,
`clock_snapshot_interval_seconds` — which the approved schema already labels
writer configuration and explicitly not a scientific parameter. `chunk_max_rows`
is applied to **every** raw table. What the recorder observes about *itself* —
how long a producer was blocked, whether a source stopped when asked — is
returned in memory and persisted nowhere.

> ### Corrected (CL-003-R3, 2026-09-01)
>
> As first written this record claimed "**so no table in a chunk exceeds it**".
> That was not true and could not have been. The recorder appended a whole
> packet and only then tested the threshold, so a single packet carrying more
> than `chunk_max_rows` rows produced an oversized chunk — reproduced at a limit
> of 4 with 16-sample packets, which yielded chunks of 16. The original wording
> is kept above the strike so the overclaim is on the record.
>
> **The actual rule.** A chunk is cut on a **packet boundary**. The bound is now
> enforced *before* a packet is appended, so no chunk exceeds `chunk_max_rows`
> in any raw table — with exactly one exception:
>
> > A packet whose own rows exceed `chunk_max_rows` becomes an oversized chunk
> > of its own, and is committed immediately so the overflow is one packet wide
> > and never drags others with it.
>
> **Why the exception is not optional.** The alternative is splitting a packet
> across chunks. The packet→sample grouping is a preserved acquisition fact —
> v1 spec §19 lists dropping it among the things that would make future
> Athena/H10 timing analysis impossible — so raw packet contents are not
> reinterpreted to satisfy a storage bound. Chunk boundaries, by contrast, carry
> no analytical meaning at all (v1 spec §10.2), so moving them is free.
>
> The frozen spec is unaffected: §12.2 says only "whichever comes first" and
> never claimed a hard cap. The overclaim was in this record, not in the schema.
>
> **Source.** ChatGPT review of PR #3, routed by the Handoff review bridge.

**Why.** The scheduling policy already existed in the frozen schema and had no
implementation, because nothing ran a loop. Reading it from there means CL-003
introduces no policy value of its own. The spec says "rows" without naming a
table; rather than pick one and call the choice a definition, the bound is
applied to all of them, which satisfies the stated purpose — bounding how much
in-flight data a crash can cost — under either reading. Back-pressure statistics
are host-side engineering facts, not acquisition facts; persisting them would
create a package authority with no approved decision behind it (D29), and where
data was actually lost the device counter is the authority (`TIMING.md`).

**Rejected.** New recorder constants for chunk size — the schema already carries
them. A persisted recorder summary or a new event schema for back-pressure — a
new persisted fact needs an approved decision, and this one has no reader yet.

**Open.** Whether host-side back-pressure should eventually be recorded in the
package, and in what representation, is a question for the ticket that has a
consumer for it.

## D38 — Acceptance and shutdown are one atomic step, not two

**Decided (CL-003-R1).** The fan-in hand-off is not a `queue.Queue` guarded by a
flag. `_Handoff` decides acceptance and lowers the shutdown barrier under one
lock, and `close()` sets the barrier **and** takes everything still queued in the
same lock hold.

**Why.** With a queue plus a flag, "is the recorder still consuming?" and
"enqueue" are two operations, and teardown lands between them: a producer checks
the flag, blocks inside `put`, and its packet is accepted *after* teardown's
final drain. That packet is acquired data that is never written and never
reported — the exact silent loss back-pressure exists to prevent. The invariant
now holds by construction:

> once teardown has decided that no more packets will be consumed, no producer
> can subsequently make a new packet visible to the fan-in; and every packet
> successfully submitted before that barrier is either written to the package or
> reported as an explicit failure — never silently abandoned.

**Rejected.** A second flag check *after* `put` — it moves the window rather than
closing it, and by then the packet is already visible to a consumer that has
stopped looking. Removing the packet after the fact — that races the consumer.
An unbounded queue — that deletes back-pressure instead of making it safe.

**Consequences.** A producer refused at the barrier closes its stream `FAILED`
with the count, because a stream that lost acquired data is not clean. The
fan-in loop also gained a wall-clock grace period after `request_stop()`: a
source that never notices `stop` — a vendor SDK inside a blocking read — used to
make `run()` unreturnable, and a barrier that can never be reached is not a
barrier. That timer is real time on purpose, so an injected scheduling clock
cannot defer it.

**Source.** Human review of CL-003; the first task carried end to end by the
Handoff/ChatGPT review bridge.

## D39 — Every accepted packet has exactly one owner

**Decided (CL-003-R1-R2).** At any instant an accepted packet is in exactly one
of four places, and the fatal path walks all three unwritten ones:

```text
_Handoff queue  ->  stream.pending  ->  stream.in_flight  ->  written
```

`in_flight` is a field on the stream, not a local in `_commit()`. Entering a
fatal path counts and **clears** all three unwritten places, which is what makes
the count exactly once, and the identity `submitted == written + dropped` holds
on every path.

**Why.** The batch handed to `commit_chunk` used to live in a local variable. If
that write failed, the batch vanished with the stack frame, and packets accepted
onto *other* streams sat unseen in their pending accumulators — so a session
could lose two whole chunks of acquired data and still report `dropped == 0`.
Reproduced before the fix: 5 packets submitted on one stream, 2 durable, 1
counted; a second stream with 3 accepted packets reported 0. That contradicts
D38's guarantee that every submitted packet is either durable or explicitly
surfaced.

**Rejected.** Deriving `dropped` as `submitted - written`. It is self-consistent
whatever actually reached the disk, so it would report a sound-looking balance
while proving nothing about which packets were lost. The identity is *checked*
against an independent ownership count, never used as its source.

**Rejected.** Counting the failed batch at the call site in `_commit()`. It
closes one hole and leaves the other two, and puts the accounting rule in three
places instead of one.

**Source.** ChatGPT review of PR #2, routed automatically by the Handoff review
bridge.

---

## Awaiting a named human

Recorded here so the gap is visible rather than implicit. Nothing in this list
may be answered by a coding agent (`AGENTS.md` §6).

- The Phase 0 protocol in full — `PHASE0_PROTOCOL.md`.
- Any signal-quality, artefact or impedance criterion.
- Any frequency band definition, epoch length or baseline window.
- Any operational definition of Focus, or of any other state.
- Any timing tolerance, drift budget or jitter limit — `TIMING.md`. **The
  CL-002A approval froze no timing threshold; it froze only how the raw
  quantities are preserved.**
- Consent, retention, withdrawal and access policy — `SAFETY.md`.
- Retention period, backup cadence and offsite backup location.
- Withdrawal or deletion policy versus raw immutability — these are in direct
  conflict and the approval did not resolve it.
- Which streams are `required` for each protocol or session type. **The
  completion predicate (D22) depends on this set**, so it is the one open item
  that gates a working `Finalizer`.
- Whether `UNCLASSIFIED` sessions are categorically excluded from analysis or
  handled by protocol-specific rules.
- The annotation tampering threat model (D20).

**Closed by CL-002A-APPROVAL:** lateral outcome reclassification (now D19,
forbidden in v1) and whether transport payload capture may be disabled when
bytes are available (now D13, no).
