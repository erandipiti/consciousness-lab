# Changelog

All notable changes to this project are recorded here. Structural decisions and
their rationale live in [`docs/DECISIONS.md`](docs/DECISIONS.md); this file
records what changed and when.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning policy is unresolved — see `docs/OPERATIONS.md`.

## [Unreleased]

### Added — CL-002B-R2: Session Package v2 implemented

`src/` now writes and verifies **Session Package v2** (`schema_version = "2.0"`).
The v1 implementation is replaced, not carried alongside: no Study 001 recording
exists under v1, so backward compatibility was not a requirement, and a v2 reader
meeting a v1 package raises `UnsupportedSchemaVersionError` rather than guessing
(D27). No legacy or debug reader was built, because nothing needed one.

**Removed from the production writer** — each was a persisted second copy of a
fact with another authority (D28, D29):

- per-chunk `NNNNNN.commit.json` sidecars; `chunks.jsonl` is the sole commit
  authority
- `ChunkArtifact` entirely — artifact `path` and `bytes` are gone, replaced by
  `artifact_sha256`, a map of kind to digest
- `first_packet_seq` / `last_packet_seq` / `descriptor_sha256` from the commit
  record, and `record_sha256` from `chunks.jsonl`
- `ManifestStream`, `manifest.session_id`, `manifest.scope_note`, the flat
  `inventory`, the `schemas` list and the `events_seal` wrapper
- `payload_ref` — from the packets Arrow schema, the writer, the reader and the
  synthetic source. The packets schema is now identical at every capture level.

**Added**

- `raw/<stream_id>/stream_close.json`, written once per opened stream via
  tmp → fsync → rename → fsync(dir) and immutable thereafter (D33). It is the
  sole persisted closure authority; the manifest seals it and does not repeat it.
  `RECOVERED_UNCLEAN` joins the status domain.
- `manifest.control_sha256`, a map keyed on a **derived** control set —
  `allocation.json`, `run.json`, each stream's three control files, and exactly
  the schema snapshots the sealed events log references. Raw artifact hashes
  appear once, in `chunks.jsonl` (D30).
- `storage/package_layout.py`: the closed v2 file set, the derived control set,
  the referenced-schema set, and canonical-on-disk verification for documents
  and JSONL regions.
- Physical Arrow schema conformance and observation row semantics validated on
  **read** by the same shared validator the writer uses (D31).
- `recovery.resume_finalization`: a durable terminal lifecycle record with no
  manifest pair is `INTERRUPTED_FINALIZATION` and may be resumed without
  altering the recorded outcome. Resuming an ABORTED package seals ABORTED.

**Fixed while porting**

- `recovery.scan` and the derived registry read the **whole** `lifecycle.jsonl`,
  while the verifier reads only the sealed prefix. A record appended past
  `sealed_len` therefore changed the outcome those two reported. Both now use
  `lifecycle.authoritative_records`, so every reader agrees where the authority
  stops.
- The registry's stream rows were built from `manifest.streams`, which v2 does
  not have; they now come from the physical stream directories and each
  stream's own `stream_close.json`.

**Tests: 340 → 349.** Four v1 files were deleted outright, having existed only to
reconcile representations v2 removed: `test_equivalence_matrix.py` (the R01–R34
matrix), `test_canonical_and_physical.py` (sidecar and summary reconciliation),
`test_leaf_integrity.py` (inventory `bytes`, duplicate inventory paths) and
`test_referential_integrity.py` (manifest ↔ filesystem). Six replaced them:
`test_relations_v01_v14.py`, `test_v2_contract.py`, `test_layout_closure.py`,
`test_arrow_conformance.py`, `test_stream_closure.py`,
`test_finalization_and_resume.py`, `test_canonical_on_disk.py` and
`test_mutation_properties.py`. Adversarial tests mutate physical bytes and then
restore every hash a forger could restore, so what survives is the conformance
rule rather than an incidental hash mismatch.

**One deliberate non-enforcement, stated rather than hidden.** A deleted
`observations` row is not detected, because §10.3 names no observation primary
key and forbids inventing one — observations are not a complete set by contract.
`test_mutation_properties.py` asserts that boundary explicitly instead of
implying the class is covered.

### Fixed — CL-002A-R3-APPROVAL-ERRATA: stale implementation pointer (documentation only)

`docs/SESSION_FORMAT.md` §"What implementation owes this document" still read
"CL-002B implements `SESSION_SCHEMA_PROPOSAL.md` as approved", pointing the live
implementation obligation at the **superseded v1** specification and contradicting
the freeze recorded at the top of the same document. It now points at
`SESSION_SCHEMA_V2_PROPOSAL.md` and **CL-002B-R2**, and states that CL-002B's v1
obligation is discharged and historical — `src/` writing v1 today is the gap
CL-002B-R2 closes, not a defect against the frozen v2 specification.

Documentation only. No architecture, decision, schema semantics, accounting,
recovery behaviour or scientific scope was touched.

### Approved — CL-002A-R3-APPROVAL: Session Package v2 frozen (documentation only)

**Human approval, 2026-08-31.** Session Package v2 architecture is **APPROVED**;
draft records **D27–D34 are APPROVED** and are now recorded in
[`docs/DECISIONS.md`](docs/DECISIONS.md) as decisions, not drafts. No further
architecture or adversarial review is required. Production code and tests are
unchanged — the implementation in `src/` still writes v1, and CL-002B-R2 is the
task that migrates it.

**Frozen**

- `docs/SESSION_SCHEMA_V2_PROPOSAL.md` — now **APPROVED AND FROZEN**, the
  authoritative specification for `schema_version = "2.0"`. A future
  implementation discrepancy is a bug unless a later decision record supersedes
  it. §16 no longer holds drafts; `DECISIONS.md` governs.
- `docs/PACKAGE_INTEGRITY_V2.md`, `docs/V1_TO_V2_SIMPLIFICATION.md` — frozen with
  it.
- `docs/SESSION_SCHEMA_PROPOSAL.md` — marked **SUPERSEDED BY SESSION PACKAGE v2,
  historical record**, per D27. Nothing is deleted; its approval history stands
  as recorded, and it remains accurate about v1. `docs/CHUNK_EQUIVALENCE.md`
  stays as the v1 implementation audit.
- `docs/SESSION_FORMAT.md` — points at v2 as the current contract, keeps the v1
  baseline and its audit as history, and states plainly that `src/` still writes
  v1.

**Three historical/accounting wording corrections required by the approval.**
None changes the 34 → 14 accounting or any v2 semantics.

- **Scope.** "v2 relations with no v1 ancestor" now reads "with no **R01–R34**
  ancestor" everywhere. R01–R34 is the numbered v1 equivalence matrix, not an
  inventory of every normative invariant the approved v1 specification states.
- **V11 history corrected.** v1 §12.2 already required exactly one canonical
  record per line, terminated by a newline, in `lifecycle.jsonl`,
  `annotations.jsonl`, `chunks.jsonl` and `events/events.jsonl`. V11 is the
  numbered form of that existing rule, strengthened to define torn tails
  explicitly and to reject trailing bytes in a finalized package. The prior claim
  that "v1 never stated it" was wrong.
- **V14 history corrected.** v1 §12.2 already defines `record_sha256`
  verification — parse, remove `record_sha256`, canonicalize, hash, compare. V14
  promotes that existing requirement into the numbered matrix and gives it its
  explicit pre-seal role. The prior claim that v1 "never required a reader to
  check" the values was wrong.
- **V13 ancestry clarified.** R34 is the ancestor of V13's closed-filesystem
  direction only. The requirement that the physical `schemas/` set equal
  **exactly** the schema ids the sealed events log references is a v2
  strengthening R34 did not enforce.
- **V12 wording tightened** in the same pass, for accuracy rather than by
  request: v1 verified by re-canonicalizing from the parsed object, which
  tolerated a non-canonical on-disk spelling that re-canonicalized to the same
  content. V12 removes that latitude. The earlier phrasing ("v1 required
  canonical bytes only between two copies") described the mechanism wrongly.

**Counts unchanged:** v1 **34** (R01–R34) → v2 **14** (V01–V14); 17 removed
outright, 17 surviving consolidated into 11, 3 with no R01–R34 ancestor.

**Next:** CL-002B-R2 is unlocked. CL-003 remains locked.

### Proposed — CL-002A-R3-R1a: documentation consistency cleanup (documentation only)

**Status: PROPOSAL. Not approved.** No architecture, authority, recovery
semantics, minimality decision, draft record or scientific scope was reopened.
`DECISIONS.md` is untouched; production code and tests are unchanged.

- **The v1 baseline was wrong.** Every v2 document quoted "35" v1 relations. The
  normative v1 matrix (`CHUNK_EQUIVALENCE.md` §3) has always contained **34**,
  R01–R34, as the CL-002B-R1-C3 changelog entry itself records. The figure was
  introduced by CL-002A-R3 and repeated four times. Corrected everywhere.
- **The counts did not reconcile.** "35 → 14, 30 removed, 4 added" cannot sum,
  because "removed" and "added" were counted in prose phrases from the
  disposition table while the totals counted numbered relations. Replaced with a
  single unit — one numbered relation in a normative list — and a per-relation
  mapping in `PACKAGE_INTEGRITY_V2.md` **§4.1**: of v1's 34, **17 are removed
  outright** and **17 survive, consolidating into 11** v2 relations; **3** v2
  relations have no **R01–R34** ancestor (V11, V12, V14). `17 + 17 = 34`;
  `11 + 3 = 14`.
- **Stale "9" removed.** `PACKAGE_INTEGRITY_V2.md` still said "It needs 9" and
  `V1_TO_V2_SIMPLIFICATION.md` still reported v2 = 9, both left over from a draft
  whose list ended at V09. Neither matched the list shipped in the same commit.
- **V10 and V13 reclassified as reformulations, not additions.** Both are
  remaining directions of v1's R34 inventory bijection, restated now that v2
  persists no flat inventory. R34 is the one v1 relation that fans out (into V04,
  V10 and V13) rather than merging.
- **The two §3 physical conformance contracts** — Arrow schema equality and
  observation row semantics on read — are now explicitly labelled as a different
  counting unit and are deliberately not among the 14.
- **The DAG claim is scoped.** "Every byte in the sealed package is reachable
  from `manifest.sha256`" now reads "every byte **within the manifest integrity
  scope**", since `annotations.*`, `logs/` and `data/derived/` are outside the
  DAG by design and carry their own mechanisms.
- **D27–D32 → D27–D34** where a document refers to the current complete draft
  set. References describing what an earlier round contained are left as
  historical record.
- One malformed Markdown table row in `SESSION_SCHEMA_V2_PROPOSAL.md`
  (`payload_ref.length`, two cells in a three-column table) repaired; wording
  unchanged.

### Proposed — CL-002A-R3-R1: pre-seal durability and closure authority (documentation only)

Human review of R3 returned **GO WITH REQUIRED CHANGES**. The simplification
direction is accepted; five narrow corrections were applied before freeze. Still
docs-only: no production code, no test, no `DECISIONS.md` change.

- **R1-1 — stream closure is now durable before the manifest.** R3 gave
  `manifest.stream_close_status` authority over terminal closure, which meant
  the fact lived only in RAM until the final seal: a crash after the terminal
  lifecycle record but before `manifest.json` lost every per-stream status.
  Replaced by `raw/<stream_id>/stream_close.json`, written once and immutable,
  sealed through `control_sha256`. `RECOVERED_UNCLEAN` added to the status
  domain as an operational observation, never mapped silently to `FAILED` or
  `DISCONNECTED`. **The v2 manifest now owns no semantic stream fact at all.**
- **R1-2 — `record_sha256` retained in `lifecycle.jsonl` and
  `events/events.jsonl`.** R3 removed them on consistency grounds; review
  reversed that, correctly. Before a manifest exists there is no whole-file
  seal, and recovery must read exactly those logs to learn what durably
  happened. A per-record hash there is integrity metadata protecting the sole
  authority, not a competing one. `chunks.jsonl` still drops its self-hash.
  Final disposition: **chunks removed, lifecycle retained, events retained,
  annotations retained.**
- **R1-3 — interrupted finalization is resumable.** Explicit crash-window table
  and finalization order. A package with a durable terminal lifecycle record but
  no manifest pair is `INTERRUPTED_FINALIZATION` — not `UNREADABLE`, and not
  automatically `RECOVERED_UNCLEAN`. Recovery may complete the interrupted
  sealing transaction without altering the outcome already written; that is not
  promotion to `COMPLETED`. Contradictory durable state reports `BLOCKED`.
- **R1-4 — the package layout is closed.** The file set is exhaustively defined
  for the root, `events/`, `schemas/` and `raw/<stream>/`. An unexpected
  immutable file invalidates a sealed package even if it is simply absent from
  `control_sha256`, and the `schemas/` set must equal the schema ids the sealed
  events log references. Enforced inside conditions 2 and 7 — **still eight
  conditions**.
- **R1-5 — canonical means canonical on disk.** Re-canonicalizing a parsed
  document must reproduce the physical bytes exactly. "It parses and would
  canonicalize to the same content" is not sufficient: one record must have one
  spelling.

Draft decisions **D33** (per-stream durable closure authority) and **D34**
(pre-seal logs retain local record integrity) added. D27–D32 revised where
affected. All remain drafts.

Relation count reconciled against the normative lists, in one unit. v1 has
**34** numbered relations (R01–R34); v2 has **14** (V01–V14). Of the 34, **17 are
removed outright** and **17 survive, consolidating into 11**; **3** v2 relations
have no **R01–R34** ancestor (V11, V12, V14). `17 + 17 = 34`, `11 + 3 = 14`.
R01–R34 is the numbered v1 matrix, not an inventory of every invariant the
approved v1 specification states. None of the
additions is a relation between two persisted copies. Two further checks were
added as §3 physical conformance contracts and are deliberately not counted among
the 14. `PACKAGE_INTEGRITY_V2.md` §4.1 names every relation on both sides.
Earlier drafts reported "35 → 14, 30 removed, 4 added"; that baseline was off by
one and mixed counting units, and did not sum.

### Proposed — CL-002A-R3: Session Package v2 simplification (documentation only)

**Status: PROPOSAL. Not approved.** `DECISIONS.md` is untouched; production code
and tests are unchanged. Draft decision records D27–D32 await human approval.

**Why the design was reopened.** v1 survived five adversarial review rounds and
never converged, because each round found a *different* independently falsifiable
representation of the same fact: manifest ↔ filesystem (R1), sidecar ↔ chain
(C1), parsed model ↔ canonical bytes and summary ↔ physical rows (C2), byte
frame / row set / list (C3), Arrow schema and row semantics (C4). That is a
recursion with no floor, because v1 persists the same fact in several places by
design. v2 removes the duplicates instead of enforcing them.

**New documents**

- `docs/SESSION_SCHEMA_V2_PROPOSAL.md` — the complete v2 data contract,
  `schema_version = "2.0"`, specific enough to implement without inventing
  semantics.
- `docs/PACKAGE_INTEGRITY_V2.md` — authority by fact, the integrity DAG,
  physical conformance contracts, and **11** referential relations where v1
  needed 34. (Revised to 14 by CL-002A-R3-R1 and reconciled by CL-002A-R3-R1a;
  the "9" first published here never matched the list this commit shipped.)
- `docs/V1_TO_V2_SIMPLIFICATION.md` — per-representation disposition table with
  the integrity relations each removal eliminates.

`docs/CHUNK_EQUIVALENCE.md` is marked **SUPERSEDED FOR V2** and kept intact as
the v1 implementation audit.

**Principal changes proposed**

- Chunk sidecars removed; `chunks.jsonl` is the only persisted commit authority.
- Artifact paths, artifact byte counts, chunk packet ranges and chunk descriptor
  hashes removed — all deterministically derivable.
- `ManifestStream` replaced by a `stream_close_status` map; closure status is the
  only stream fact with no other on-disk home.
- `manifest.inventory` narrowed to control files and turned into a **map**, so
  duplicate paths are structurally impossible; raw artifacts are sealed
  transitively through the chunk chain, hashed once rather than twice.
- Hierarchical integrity DAG: manifest seals control files and stream indexes;
  each index seals its own artifacts.
- **Added deliberately:** Arrow physical schema conformance and observation row
  semantics on read, closing C4's G1 and G2. Neither is a relation between two
  persisted copies.
- `sparse_long` primary key resolved as
  `(packet_seq, sample_index_in_packet, channel_id)`, closing the one open
  specification blocker from C4.

**26 equivalence relations removed, 2 added.** No provenance fact is removed —
every deletion is a copy or a derivable summary. v1 is not to be used for Study
001 acquisition; a v2 reader fails closed on v1 and no migration is designed.

No scientific threshold, minimum data rule, hardware assumption or Focus
semantics was introduced.

### Fixed — CL-002B-R1-C4: leaf-level integrity closure

Previous Codex verdict: **`NO-GO`**, three confirmed finding groups.

| Finding | Attack | Root |
|---|---|---|
| **F1** | payload frame `packet_seq` disagrees with the packet row that references it | byte-framed artifact treated as opaque |
| **F2a/F2b** | all sample rows deleted; duplicate sample key replacing another | row set treated as an artifact, `n_samples` used only as an upper bound |
| **F3a/F3b** | artifact `bytes` lies while SHA is correct; duplicate inventory entry accepted | `bytes` unchecked; list collapsed to a set |

**Common cause.** C3 decomposed *records* into fields but treated three things
as atomic that are themselves multi-field representations — a byte-framed
artifact, a row set, and a list. The enumeration was one level too shallow in
exactly those places.

**Leaf decomposition.** `docs/CHUNK_EQUIVALENCE.md` gains recursive decomposition
of every compound representation, authority for each leaf, collection-type
classification (multiplicity and order are part of the type), and an explicit
threat-model boundary.

**Newly enforced**

- **Artifact `bytes`** alongside `sha256`, for `packets`, `observations`,
  `samples`, `payloads` and every inventory entry. A matching SHA does not
  validate a separate claim about the same file.
- **Payload frame identity** — each frame's own `packet_seq` must equal the
  referencing packet row's, `payload_len` must equal `payload_ref.length`, and
  frames ↔ packet rows is a bijection: no shared frame, none unreferenced. A
  valid CRC proves a frame is intact, not that it belongs to that packet.
- **Dense sample key-set identity** — for `n_samples = N` the keys must be
  exactly `(packet_seq, 0) … (packet_seq, N-1)`, each once, compared as a
  **multiset** so a duplicate cannot mask a missing key. `n_samples = 0` expects
  an empty set.
- **Inventory bijection** — duplicate paths rejected before any set comparison.

**Deliberately not invented:** no observation cardinality or uniqueness rule
(the schema names no observation primary key), and no `sparse_long` cardinality
rule — see the specification blocker below.

**SPECIFICATION BLOCKER — `sparse_long` sample identity.** §9.1 calls
`(packet_seq, sample_index_in_packet)` "the sample primary key" immediately after
defining sparse rows as `packet_seq, sample_index_in_packet, channel_id, value`.
Those are inconsistent for that layout: the pair cannot be the key if a row also
carries `channel_id`. No cardinality or uniqueness rule is enforced for sparse
streams and none was invented; only the unambiguous structural references are
checked. Needs a human decision. No Study 001 stream is sparse today, and dense
streams are unaffected.

**Tests.** L1–L20 in `tests/test_leaf_integrity.py` plus payload-ref leaf and
frame-bijection cases (29). The property sweep grew from 15 to **28 mutation
dimensions**; an audit confirms 50 of 56 dimension × capture-level combinations
apply and **all 50 are caught**, with 6 genuinely inapplicable. 340 total.

**Threat-model boundary, stated explicitly.** This establishes structural,
referential and cross-representation integrity — not cryptographic authenticity.
An actor who coherently rewrites every artifact and all metadata cannot be
distinguished from the original package without signatures or append-only media,
and that is out of scope for Session Package v1.

### Added — CL-002B-R1-C3: chunk representation equivalence matrix

**Why single-attack patching stopped.** Three review cycles produced the same
shape of defect, never the same bug twice: manifest vs missing raw directory,
sidecar vs `chunks.jsonl`, parsed-model equality vs canonical record identity,
asymmetric null handling, then chain ordering and symmetric null. Each fix
closed the reported attack and left an adjacent state of the *same relation*
unenumerated. The failure was never a missing `if`; it was an unenumerated
relation. So this round enumerated them before touching code.

**`docs/CHUNK_EQUIVALENCE.md`** records the 10 representations of a committed
chunk, field-by-field authority (no artifact is universally authoritative — that
assumption caused two of the defects), 34 required relations R01–R34, the
nullability matrix including symmetric-invalid states, the collection/order
matrix, the structural foreign keys actually enforced, zero-chunk semantics, and
what is deliberately not enforced.

**Newly enforced matrix rows**

- **R12 payload key *presence*** — §12.2 requires the `payloads` key omitted at
  `library_decoded` / `synthetic`, "never written as a null". Two records both
  carrying an explicit null agree with each other and both violate the contract;
  pairwise equality is blind to it, so key presence is now checked directly.
- **R19 / R24 / R25 ordering** — `packet_seq` is strictly increasing within a
  chunk and across the chain (§9.1), and `chunk_id` strictly increases along the
  append-only chain. A reversed or swapped chain previously passed because every
  chunk still matched its own artifact. Contiguity is **not** required for
  either: a gap is a device fact for a later ticket.
- **R20–R23 structural foreign keys** — `samples.packet_seq` and
  `observations.packet_seq` must reference a packet in the same chunk, and
  sample indices must fall within that packet's `n_samples`. These follow from
  §9.1's own statements and are structural, not scientific.

**Property-based mutation testing.** A Hypothesis sweep over 15 mutation
dimensions × capture level × chunk position asserts one property: mutate any
single representation so a required relation is violated, recompute every
attacker-controlled hash, and `is_completed` must be false. An explicit audit
confirmed 28 of 30 mutation/capture-level combinations apply and **all 28 are
caught**; the 2 skips are genuinely inapplicable.

**Explicit regressions.** E1–E25 name the high-value attack classes, plus
adjacent-swap, duplicate chunk id, per-position (first/middle/last) forging,
cardinality 0/1/2/3/5, and finalizer parity for ordering and key presence.

**Test-fixture integrity.** The new foreign-key checks caught the C2-7 "consistent
rewrite" fixture, which shifted the packets artifact but not the samples and
observations referencing it. The *fixture* was inconsistent, not the production
code. It was corrected rather than the check weakened.

**Closed from the previous review:** both remaining BLOCKING findings — reordered
chain, and symmetric `"payloads": null` — each with named tests (E12, E7).

311 tests. No scientific completeness rule: zero-chunk streams remain valid and
no minimum chunk, packet, sample or duration criterion exists.

### Fixed — CL-002B-R1-C2: canonical record and physical packet reconciliation

Previous Codex verdict: **`GO WITH REQUIRED CHANGES`**, three BLOCKING findings.

- **B1** — sidecar reconciliation compared parsed `ChunkCommit` models rather
  than the same on-disk record. `chunks.jsonl` omits `payloads` as §12.2
  requires; a sidecar carrying `"payloads": null`, re-sealed with a refreshed
  inventory and manifest, parsed to an equal model and passed.
- **B2** — a forged `chunks.jsonl` plus matching sidecars could lie about
  artifact metadata: `first_packet_seq` / `last_packet_seq` were only ever
  compared manifest ↔ chain, never against the packets artifact, and two
  commits could claim the same artifact paths.
- **B3** — the finalizer preflight checked which chunk ids were present but
  never whether the records matched what the writer committed, so a chain and
  sidecars rewritten under the same ids could seal an immutable `COMPLETED`.

**Shared root cause.** Interpreted representations were compared where the
actual bytes and the actual data were required: parsed models stood in for
on-disk record identity, and summaries were checked against other summaries.

**Canonical full-record comparison.** `ChunkRecordOnDisk` retains the parsed
model *and* the canonical bytes of the complete parsed document, canonicalized
from the JSON — never from `model_dump()`, which would discard exactly the
fields the model ignores. Sidecar ↔ chain identity now compares those bytes, so
an ignored unknown field or an omitted-versus-null key is detected even when the
models are equal.

**Physical packet-derived range.** Each committed chunk's `packets` artifact is
opened and its real `packet_seq` column read. Every chunk's claimed
`first_packet_seq` / `last_packet_seq` is checked against those rows — for every
chunk, not just the stream endpoints, so a forged middle chunk cannot hide.
`ManifestStream` packet range now derives from the physical rows. Artifact paths
must be each chunk's own canonical names and may not be reused across chunks.
`packet_seq` must be strictly increasing within a chunk, which spec §9.1 already
states; consecutiveness is deliberately **not** required, as a gap is a device
fact for a later ticket.

**Finalizer.** The preflight compares writer-memory committed records to the
disk chain by canonical bytes, rejects disk chunk ids the writer never
committed, and applies the same physical packet check before sealing
`COMPLETED`.

**One source of truth.** All of it lives in `storage/stream_state.py`; the
verifier and the finalizer consume it and neither re-derives packet ranges.

**Tests.** 15 in `tests/test_canonical_and_physical.py` — the exact B1/B2/B3
reproductions plus C2-1 through C2-10, including a positive control that rewrites
the packets artifact *and* every dependent summary and asserts the package still
verifies. Every negative test recomputes record hashes, chain links, sidecars,
manifest summaries, inventory and `manifest.sha256`, so none passes because a
hash was left stale. 275 total.

No scientific completeness rule was added: a zero-chunk stream remains valid and
no minimum packet, chunk, sample or duration criterion exists.

### Fixed — CL-002B-R1: manifest / raw referential integrity

**A BLOCKING false-complete.** `manifest.streams` was built entirely from the
writer's in-memory state, while verification iterated the raw directories that
happened to exist. Neither view proved anything about the other, so deleting a
required stream's whole raw directory before finalization produced a package
where **all eight completion conditions returned true** while
`raw/<stream_id>/` did not exist. Reproduced before the fix; 20 of the 23
initial regression tests fail against `ab7ea9c`.

**The fix is one source of truth, not a third view.**
`storage/stream_state.py` derives chunk count, chain head, packet range,
descriptor hash and sidecars from disk, and both the verifier and the finalizer
preflight use it.

*Verifier.* Condition 5 owns semantic required-stream closure — run declares it,
the manifest agrees, the raw directory exists, and it closed `CLEAN` — plus the
rule `manifest.required == (stream_id in run.required_streams)` for every
stream. Condition 7 owns physical integrity: bidirectional manifest ↔ raw set
reconciliation, duplicate ids, descriptor presence / `stream_id` / hash of the
**stored bytes**, per-chunk `descriptor_sha256`, sidecar ↔ `chunks.jsonl`
agreement, and `chunk_count` / chain head / packet range against the actual
chain. No ninth condition was added; `is_completed()` is still all eight.

*Finalizer.* Stream summaries are derived from physical state instead of writer
memory, and sealing `COMPLETED` is refused when required streams, descriptors,
committed chunks, their artifacts or their sidecars are not on disk.

`chunks.jsonl` is now created empty at stream open, so a zero-chunk stream is
structurally identical to any other and "index absent" never has to be
interpreted.

**New verifier findings:** `MANIFEST_STREAM_MISSING_RAW`,
`RAW_STREAM_MISSING_MANIFEST`, `DUPLICATE_MANIFEST_STREAM`,
`REQUIRED_FLAG_MISMATCH`, `MISSING_DESCRIPTOR`, `DESCRIPTOR_STREAM_ID_MISMATCH`,
`MANIFEST_DESCRIPTOR_HASH_MISMATCH`, `CHUNK_DESCRIPTOR_HASH_MISMATCH`,
`CHUNK_COUNT_MISMATCH`, `CHAIN_HEAD_MISMATCH`, `PACKET_RANGE_MISMATCH`,
`MISSING_STREAM_STRUCTURE`, `SIDECAR_MISMATCH`.

**Tests.** 28 in `tests/test_referential_integrity.py` (R1–R15 plus sibling
cases). Every tamper test recomputes record, chain, inventory and manifest
hashes, so a stale hash is never the only defence and each test asks whether an
internally self-consistent but semantically false package can pass. 260 total.

**Codex review: `NO-GO`**, one BLOCKING finding — sidecars were never
reconciled against `chunks.jsonl`, so an added or contradicting
`NNNNNN.commit.json` survived once the inventory was updated and the manifest
re-hashed. Independently reproduced, then fixed in this ticket as a same-class
relationship, with five regression tests. Codex was **not** re-run, to avoid a
review loop; the recorded verdict remains NO-GO and the fix is unreviewed by it.

**No scientific completeness rule was added.** A zero-chunk stream remains
structurally valid; whether it is scientifically usable is a future protocol
decision.

### Added — CL-002B: Session Package v1 implementation

Implements the approved schema (`DECISIONS.md` D8–D26,
`SESSION_SCHEMA_PROPOSAL.md`). No hardware adapter, no analysis, no timing
reconstruction, no Focus logic. **Zero specification deviations.**

**storage/** — `integer_types` (`int64_decimal` / `uint64_decimal`, refusing
JSON Numbers on disk), `canonical_json` (RFC 8785 JCS + the `record_sha256`
procedure), `payload` (PYLD framing + CRC-32C), `safe_paths` (traversal and
symlink refusal), `arrow_schema` (packets / samples / observations),
`observations` (exactly-one-value-column rule), `chunk_writer` (atomic commit +
hash chain), `checksums` (atomic and no-clobber writes), `paths`, `verifier`
(the eight-condition predicate with structured findings), `reader`
(read + replay).

**session/** — `model` (typed on-disk contract, capture-level invariant),
`allocator` (mkdir-first ordering), `lifecycle`, `annotations` (effective
outcome, fail-closed), `writer`, `finalizer`, `registry` (derived SQLite,
rebuildable), `recovery` (reports, never repairs).

**synthetic/** — a deterministic seeded source, scoped to exercising v1.
The asynchronous multi-device recorder is CL-003 and is deliberately absent.

**232 tests**, including 6 property tests, fault injection at every chunk
commit stage, and a regression for every constructed false-complete package.

### Implementation review — CL-002B

Three read-only Codex passes. All findings fixed, each with a regression test.

| Pass | Finding | Sev | Disposition |
|---|---|---|---|
| 1 | Immutable package content overwritable via public APIs (sealed reopen, `run.json`, descriptors, chunk 0 restart) | BLOCKING | Fixed: `SealedPackageError` guards, `atomic_write_new`, `ChunkWriter` refuses an occupied stream |
| 1 | Verifier wrote a temp file inside the sealed package | SERIOUS | Fixed: sealed prefix parsed in memory |
| 1 | `finalize()` could seal `COMPLETED` when not clean | SERIOUS | Fixed: refuses before writing the terminal record |
| 1 | `read_sessions()` could be read as truth | SERIOUS | Fixed: documented as cache, added `query_sessions()`, wired the derived upsert into allocation and finalization |
| 1 | int64/uint64 accepted JSON Numbers on disk | SERIOUS | Fixed: `load_on_disk()` on every disk read |
| 1 | Observation rows unvalidated | SERIOUS | Fixed: enum + exactly-one-value-column checks before write |
| 1 | `hardware_verification` shape mismatch | SERIOUS | Fixed: `HardwareVerification{status, ref}` |
| 1 | Reader did not verify; no minor-version tolerance; replay/synthetic origins unconstrained | MODERATE | Fixed |
| 1 | Unknown git provenance recorded as clean; event payloads unvalidated | MINOR | Fixed |
| 2 | **Extra immutable files added after sealing were invisible** | BLOCKING | Fixed: condition 2 now checks both directions |
| 2 | **A symlink could stand in for a moved raw artifact** | BLOCKING | Fixed: symlinks rejected in sealed content and at seal time |
| 2 | **Annotation files parsed non-strictly** | BLOCKING | Fixed: `load_on_disk()` |
| 2 | Corrupt Arrow raised out of the verifier | SERIOUS | Fixed: `UNREADABLE_CHUNK_ARTIFACT` finding |
| 2 | Path traversal in artifact paths and `payload_ref.file` | SERIOUS | Fixed: `safe_paths`, and `payload_ref.file` must name its own chunk |
| 2 | `ENOSPC` escaped as a bare `OSError` | SERIOUS | Fixed: closes `CLEAN` / `TECHNICAL_FAILURE` with a reason |
| 2 | `recovery.scan` called any manifest pair sealed | SERIOUS | Fixed: requires verifier condition 1 |
| 3 | `raw_ref.packet_seq` written as a JSON Number | SERIOUS | Fixed: typed `RawRef` with `Int64Decimal` |
| 3 | Manifest parsed non-strictly | SERIOUS | Fixed: `load_on_disk(Manifest, …)` |
| 3 | Per-record hashes inside the sealed lifecycle and events regions unverified | SERIOUS | Fixed: `_verify_sealed_jsonl()` in condition 3 |

Pass 2 constructed three packages that reported `is_completed() == True` while
missing or carrying tampered data. Each has a named regression test.

### Notes

- No hardware adapter, BLE, serial, MNE, HRV, feature extraction, timing
  reconstruction, cross-device sync, Focus logic, ML, UI or cloud code exists.
- No scientific threshold was introduced.
- No `D8`–`D26` decision was changed.
- `pyproject.toml`: `pyarrow` added to the untyped-import override; it ships no
  `py.typed`.

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
