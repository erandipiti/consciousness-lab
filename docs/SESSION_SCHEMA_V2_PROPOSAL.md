# SESSION_SCHEMA_V2_PROPOSAL — Session Package v2

> ## Status: APPROVED AND FROZEN — Session Package v2
>
> **Human approval recorded in:** CL-002A-R3-APPROVAL, 2026-08-31.
> **Decision index:** [`DECISIONS.md`](DECISIONS.md) D27–D34, which supersede
> nothing in D8–D26 and are recorded there as approved decisions.
>
> This document is the **authoritative specification** for Session Package v2.
> **A future implementation discrepancy is a bug, unless a later decision record
> in `DECISIONS.md` explicitly supersedes this specification.**
>
> No production code or test was changed by the tickets that produced or froze
> this document. **CL-002B-R2 implemented it**; `src/` now writes
> `schema_version = "2.0"`, and a divergence from this document is a bug.

**Target:** `schema_version = "2.0"`
**Supersedes:** Session Package v1 (`SESSION_SCHEMA_PROPOSAL.md`), which remains
the historical design record.

---

## 1. Why v1 is being superseded

v1 survived five adversarial review rounds. Each one found a different
independently falsifiable representation of the same underlying fact:

| Round | Representation pair that could disagree |
|---|---|
| R1 | manifest ↔ filesystem |
| C1 | sidecar ↔ `chunks.jsonl` |
| C2 | parsed model ↔ canonical bytes; summary ↔ physical packet rows |
| C3 | byte frame, row set, list — each treated as atomic |
| C4 | Arrow schema; row value semantics |

That is not a sequence of unrelated bugs converging on zero. **It is a recursion
with no floor, because v1 persists the same fact in several places by design.**
Every persisted copy creates a relation that must hold forever, and the verifier
grew to 52 distinct findings enforcing 34 documented relations (R01–R34).

v2 does not add a sixth round of enforcement. It removes the duplicates.

**The governing question for every persisted field:**

> If this field disagreed with another persisted field, which one would win?

If the answer is obvious — one is authoritative, the other is convenient — the
convenient one is deleted, and its value is derived at read time instead.

---

## 2. What stays

- Directory per session; the package is the authority, the registry derived.
- Opaque UUIDv4 session ids.
- Arrow IPC stream files for raw chunks; append-only hash-chained `chunks.jsonl`.
- Separate `packets` / `samples` / `observations` tables.
- Transport payloads as canonical raw where the boundary exposes them.
- Lifecycle / `closure_condition` / `recording_outcome`; sealed vs effective
  outcome; downgrade-only annotations with the head pointer.
- RFC 8785 canonicalization; `int64_decimal` / `uint64_decimal`.
- The manifest pair as a **finalization** marker, never a completion marker.
- The eight-condition completion predicate, restated for v2 in §9.

**None of D8–D26 is contradicted.** v2 changes which *copies* are persisted, not
what is authoritative, and not what the study records.

---

## 3. Package layout

```text
data/sessions/<session_id>/
    allocation.json
    run.json
    lifecycle.jsonl
    annotations.jsonl
    annotations.head.json
    events/events.jsonl
    schemas/<schema_id>.json
    raw/<stream_id>/
        descriptor.json
        chunks.jsonl
        stream_close.json
        payloads/NNNNNN.bin        # transport_payload only
        packets/NNNNNN.arrow
        observations/NNNNNN.arrow
        samples/NNNNNN.arrow
    manifest.json
    manifest.sha256
    logs/                          # non-authoritative, outside integrity
```

**`NNNNNN.commit.json` does not exist in v2.**

---

## 4. V2-1 — chunk sidecars removed

`chunks.jsonl` is already the sole authority for whether a chunk is committed. A
sidecar was a complete second copy whose only stated purposes were convenience
for recovery and for single-chunk verification. It cost a permanent canonical
identity relation and produced two BLOCKING findings on its own.

### 4.1 What counts as a record

Applies to `chunks.jsonl`, `lifecycle.jsonl`, `events/events.jsonl` and
`annotations.jsonl`.

> A record is **one complete canonical JSON object terminated by `\n`**.
> Any non-empty bytes after the last newline are **not** a record.

During recording, such trailing bytes are a torn tail from an interrupted append:
recovery reports them and the chunk they would have described is **not**
committed. In a **finalized** package they make the file invalid, because
finalization is supposed to have sealed a complete file — and the byte-range
hash in `control_sha256` (or `lifecycle_seal`) would in any case have been
computed over them.

Without this rule two implementations could disagree about whether a
syntactically valid but unterminated final object is a commit.

**Chunk write ordering in v2:**

```text
payloads/NNNNNN.bin.part      -> fsync -> rename -> fsync(dir)   # transport only
packets/NNNNNN.arrow.part     -> fsync -> rename -> fsync(dir)
observations/NNNNNN.arrow.part-> fsync -> rename -> fsync(dir)
samples/NNNNNN.arrow.part     -> fsync -> rename -> fsync(dir)
append one canonical ChunkCommit record to chunks.jsonl -> fsync
```

Before the append lands the artifact files may exist, and **the chunk is not
committed**. They are orphans: recovery reports them, never adopts them, never
invents a commit. No sidecar is needed to reach that decision — the chain alone
decides.

**Recovery capability lost: none.** Every v1 recovery action already keyed off
`chunks.jsonl`; the sidecar was read only to be compared against it. A per-chunk
index for read performance, should one ever be wanted, belongs in
`registry.sqlite` as a **derived, rebuildable** index — never in the sealed
acquisition format.

---

## 5. V2-2 — deterministic artifact paths

An artifact's path is fully determined by `stream_id`, `chunk_id` and kind:

```text
chunk_id = 123  ->  packets/000123.arrow
                    observations/000123.arrow
                    samples/000123.arrow
                    payloads/000123.bin      (transport_payload only)
```

The reader derives it. Persisting it again in every commit record bought nothing
and created four relations that all had to be enforced: path ↔ `chunk_id`, path ↔
kind, path uniqueness within a stream, and path traversal safety.

**Consequence: `payload_ref` is removed from the packets schema entirely.**

A frame already carries its own `packet_seq`, and parsing
`payloads/NNNNNN.bin` yields the complete offset/length index deterministically.
`payload_ref.file`, `.offset` and `.length` were therefore a persisted second
copy of what parsing produces. A reader builds the packet → frame map by
`packet_seq`.

This removes three relations at once — `payload_ref.file` ↔ committed payload
path, `.offset` ↔ frame boundary, `.length` ↔ framed length — and one more as a
side effect: the packets Arrow schema no longer varies by capture level, so
"`payload_ref` nullability iff `transport_payload`" disappears too.

The cost is random access: reading one packet's bytes means walking the chunk's
payload file. Payload files are per-chunk and bounded by the writer's chunk size,
so this is "less direct to read", not "cannot be determined".

---

## 6. V2-3 — minimal ChunkCommit

```json
{
  "chunk_id": "123",
  "prev_record_sha256": "…",
  "artifact_sha256": {
    "packets": "…",
    "observations": "…",
    "samples": "…",
    "payloads": "…"
  }
}
```

**No persisted `record_sha256`.** A record's own hash is derivable from its
canonical bytes, and the next record's `prev_record_sha256` is exactly that
value, so a reader computes it regardless. Persisting it was a second copy of a
derived value.

> *Design-review note.* This was raised as "circular authority". It was not
> circular — the hash domain explicitly excluded the key itself, which v1
> specified precisely. It was simply **redundant**, which is enough under D29.

`prev_record_sha256` is defined as the SHA-256 of the previous record's canonical
bytes; the first record uses `"0" * 64`. The chain's last record is protected by
the whole-file hash in `control_sha256`, so nothing is left unsealed.

**Scope: `chunks.jsonl` only.** An earlier draft extended this to
`lifecycle.jsonl` and `events/events.jsonl` on consistency grounds. Human review
reversed that, and correctly:

| Log | `record_sha256` | Why |
|---|---|---|
| `chunks.jsonl` | **removed** | its meaningful fields are reconciled against the previous chain record, the capture shape, deterministic paths and physical artifact hashes; after finalization the whole chain is sealed by `control_sha256` |
| `lifecycle.jsonl` | **retained** | see §6.1 |
| `events/events.jsonl` | **retained** | see §6.1 |
| `annotations.jsonl` | **retained** | written after sealing, outside the manifest DAG, and `annotations.head.json.head_record_sha256` names the value |

### 6.1 Why lifecycle and events keep theirs

The minimality rule is "do not persist a second **authority** for a fact". A
per-record hash is not a second authority over a record's meaning — it is
**integrity metadata protecting the sole authority**, and the two have different
temporal roles:

```text
record_sha256          protects an individual durable record BEFORE sealing
manifest whole-file    seals the finalized log as a complete byte sequence
```

Before a manifest exists there is no whole-file seal. Recovery must nevertheless
read `lifecycle.jsonl` and `events/events.jsonl` to determine what durably
happened before the process disappeared — and it must be able to tell an intact
record from a corrupt one while doing so. Removing the per-record hash would
leave the pre-seal window with no integrity at all over exactly the records
recovery depends on.

`chunks.jsonl` is different: its records are cross-checked against physical
artifacts that recovery reads anyway, so a self-hash adds nothing there.

These are complementary layers, not competing authorities.

| Key | Rule |
|---|---|
| `packets` | required |
| `observations` | required |
| `samples` | required |
| `payloads` | present **iff** `descriptor.acquisition.raw_capture_level == "transport_payload"`, absent otherwise |

**Removed from v1's ChunkCommit**

| Field | Why it goes |
|---|---|
| artifact `path` | deterministic from `chunk_id` + kind |
| artifact `bytes` | SHA-256 over the whole file already fixes byte content and therefore length; the size was a second claim about the same file, and it was unchecked until C4 |
| `first_packet_seq` | the physical `packets` rows are authoritative |
| `last_packet_seq` | same |
| `descriptor_sha256` | a stream has exactly one immutable descriptor, sealed by the manifest; a chunk cannot be under a different one |

The `payloads` key presence remains the **one** key-presence relation in v2, and
it is unavoidable: the payload artifact's hash has to live somewhere. Its
authority is the descriptor's capture level.

`artifact_sha256` is a JSON object, so duplicate keys are impossible by
construction under RFC 8785 — the duplicate-entry class of defect cannot recur
here.

---

## 7. V2-4 — minimal manifest

```json
{
  "schema_name": "session_package",
  "schema_version": "2.0",
  "sealed_at": { "utc_ns": "…", "monotonic_ns": "…",
                 "utc_clock_id": "CLOCK_REALTIME",
                 "monotonic_clock_id": "CLOCK_MONOTONIC",
                 "utc_quality": "unknown" },
  "lifecycle_seal": { "sealed_len": "8421", "sealed_sha256": "…" },
  "events_sha256": "…",
  "control_sha256": {
    "allocation.json": "…",
    "run.json": "…",
    "raw/muse.eeg/descriptor.json": "…",
    "raw/muse.eeg/chunks.jsonl": "…",
    "raw/muse.eeg/stream_close.json": "…",
    "raw/polar.ecg/descriptor.json": "…",
    "raw/polar.ecg/chunks.jsonl": "…",
    "raw/polar.ecg/stream_close.json": "…",
    "schemas/block_start.v1.json": "…"
  }
}
```

**No `session_id`** — identity is owned by `allocation.json`, whose hash is in
`control_sha256`, and the directory name. A manifest swapped between packages is
caught because the allocation hash would not match. Three copies of one identity
is the pattern this redesign removes.

**No `scope_note`** — it owned no package fact. It restated a contract that
belongs in this specification and in the verifier, where it cannot drift away
from the code that enforces it.

**`ManifestStream` is deleted, and so is the `stream_close_status` map that an
earlier draft put in its place.**

That draft argued closure status had no other on-disk home. Human review found
the real problem: *it should have one*. Terminal closure lived only in process
memory until the final manifest was written, so this crash window existed —

```text
all streams closed
      -> lifecycle CLOSED / CLEAN / COMPLETED written durably
            -> PROCESS DIES
                  -> manifest never written, per-stream statuses lost with RAM
```

— and a restarted process could not safely reconstruct them. **A fact required to
resume finalization must not live only in RAM until the final seal.** See §7.1.

**The v2 manifest therefore owns no semantic stream fact at all.** Its role is
exactly two things: finalization marker, and integrity root.

| v1 `ManifestStream` field | v2 authority instead |
|---|---|
| `required` | `run.json.required_streams` |
| `close_status` | **`raw/<id>/stream_close.json`** (§7.1) |
| `descriptor_sha256` | `control_sha256["raw/<id>/descriptor.json"]` |
| `chunk_count` | `chunks.jsonl` |
| `chunk_chain_head_sha256` | `chunks.jsonl` |
| `first_packet_seq` / `last_packet_seq` | the physical `packets` artifacts |

### 7.1 `raw/<stream_id>/stream_close.json` — durable per-stream closure

Exactly one immutable file per opened stream, written once when that stream
terminates:

```json
{ "close_status": "CLEAN" }
```

No `stream_id` — the path already owns identity. No convenience summaries.

**Status domain**

| Status | Meaning |
|---|---|
| `CLEAN` | the stream closed normally |
| `DISCONNECTED` | the device went away |
| `RECONFIGURED` | the configuration changed; a new `stream_id` with a new descriptor takes over |
| `FAILED` | a diagnosed stream failure |
| `RECOVERED_UNCLEAN` | **new in v2** — the process disappeared while this stream had no durable terminal close record |

`RECOVERED_UNCLEAN` is an **operational fact, not a scientific classification**:
recovery observed that the stream did not close normally, and says exactly that.
It must never be silently mapped to `FAILED` or `DISCONNECTED`, which would
assert a device-specific cause nobody observed.

**Write discipline**

```text
stream_close.json.tmp -> fsync -> rename -> fsync(stream directory)
```

Only after that durable file exists may finalization treat the stream as closed.
A stream that closed earlier already has its file and **must not have it
rewritten** — it is immutable once written.

**Authority.** `stream_close.json` is the sole persisted authority for terminal
closure. The manifest seals it through `control_sha256` and does not repeat it,
so no second copy exists to drift.

**Other manifest reductions**

- `inventory` (a list of `{path, bytes, sha256}`) becomes `control_sha256`, a
  **map** of path → SHA. Duplicate paths become structurally impossible, and
  `bytes` disappears for the reason in §6.
- `schemas` (a list of `{schema_id, path, sha256}`) folds into `control_sha256`
  under its deterministic path `schemas/<schema_id>.json`.
- `events_seal` (`{path, bytes, sha256}`) becomes a single `events_sha256`; the
  path is fixed and the length adds nothing over the hash.
- `lifecycle_seal` keeps `sealed_len` — that length is **semantic**, marking the
  prefix boundary that annotations append beyond — and drops its fixed `path`.

---

## 8. V2-5 / V2-6 — hierarchical integrity

Integrity is an explicit DAG. Nothing is hashed twice.

```text
manifest.sha256
   └── manifest.json
         ├── control_sha256 ─── allocation.json
         │                 ├── run.json
         │                 ├── raw/<stream>/descriptor.json
         │                 ├── raw/<stream>/stream_close.json
         │                 ├── raw/<stream>/chunks.jsonl ──┐
         │                 └── schemas/<schema_id>.json    │
         ├── lifecycle_seal ── lifecycle.jsonl[:sealed_len] │
         └── events_sha256 ─── events/events.jsonl          │
                                                            │
                        each chunks.jsonl (hash-chained) ───┘
                              └── artifact_sha256 ── packets/NNNNNN.arrow
                                                  ├── observations/NNNNNN.arrow
                                                  ├── samples/NNNNNN.arrow
                                                  └── payloads/NNNNNN.bin
```

The manifest seals each stream's chunk index; each index seals its own
artifacts. Raw artifact hashes therefore appear **once**, not once in a commit
record and again in a manifest inventory.

**"Not in `control_sha256`" does not mean "not integrity protected."** Raw
artifacts are protected transitively, and the verifier still scans the raw tree
and rejects unreferenced artifacts, unexpected files and missing committed
artifacts (§9, condition 7).

---

## 9. V2-11 — completion predicate

Eight conditions, same shape as v1.

> `is_completed(package)` is true **iff all eight hold**:
>
> 1. `manifest.json` exists and `manifest.sha256` matches it; `schema_version`
>    has major 2.
> 2. **the package file set is closed and sealed.** `control_sha256`'s key set
>    **equals** the derived expected control set exactly, every entry resolves to
>    a regular file hashing to the recorded value, and the package contains no
>    unexpected immutable file anywhere (§9.2). The expected set is *derived*,
>    never read from the manifest:
>
>    ```text
>    allocation.json
>    run.json
>    raw/<id>/descriptor.json      for every physical stream directory
>    raw/<id>/chunks.jsonl         for every physical stream directory
>    raw/<id>/stream_close.json    for every physical stream directory
>    schemas/<schema_id>.json      for every payload_schema referenced in the
>                                  sealed events log
>    ```
>
>    Equality in both directions. (Duplicates are impossible: it is a map.)
> 3. the first `lifecycle_seal.sealed_len` bytes of `lifecycle.jsonl` hash to
>    `lifecycle_seal.sealed_sha256`, and `events/events.jsonl` hashes to
>    `events_sha256`. Within both sealed ranges every line must be one complete
>    canonical JSON object terminated by `\n` (§4.1), be **canonical on disk**
>    (§9.3), validate against its model, **and verify its own
>    `record_sha256`** — the pre-seal integrity layer of §6.1.
> 4. within the sealed prefix the terminal state is `CLOSED`, with
>    `closure_condition = CLEAN` and **sealed** `recording_outcome = COMPLETED`.
> 5. **stream set agreement and closure**, in three parts:
>    a. every physical `raw/<id>/` directory is declared in
>       `run.required_streams ∪ run.optional_streams` — a stream outside the run
>       contract is not a valid part of the package;
>    b. **every physical stream directory has a valid `stream_close.json`**,
>       whatever its status;
>    c. every id in `run.required_streams` has a physical directory whose
>       `stream_close.json` reads `close_status == "CLEAN"`.
>
>    A declared **optional** stream that was never opened has no directory and no
>    close file; that is valid. A required stream recovered as
>    `RECOVERED_UNCLEAN` fails this condition, as it should.
> 6. no `.part`, `.tmp` or `.open` file exists anywhere in the package.
> 7. every raw stream passes **physical integrity** (§10): descriptor valid;
>    chain valid and ordered; every committed artifact exists at its
>    deterministic path and hashes to `artifact_sha256`; no unreferenced or
>    unexpected raw artifact; Arrow schema conformant; row semantics valid;
>    payload framing valid where applicable; referential integrity valid; and
>    the stream directory contains **no file outside its defined set** (§9.2).
> 8. the **effective** recording outcome (sealed outcome after applying valid
>    downgrade annotations, failing closed on an indeterminate annotation log) is
>    `COMPLETED`.

Condition 5 now also carries the stream-set agreement, which v1 split across
conditions 5 and 7. **Still eight conditions** — the file-set closure of §9.2
lands inside conditions 2 and 7 rather than becoming a ninth. No scientific
completeness requirement is added anywhere.

### 9.2 The package file set is closed, not open-ended

The integrity DAG claims every immutable sealed byte is reachable from the
manifest. That claim is only true if the package cannot contain a file the DAG
never mentions — otherwise an unexpected immutable file simply sits outside it,
unhashed and unnoticed. So the layout is **exhaustively defined**, and a
finalized package with anything else in it is invalid.

| Location | Exactly these files |
|---|---|
| package root | `allocation.json`, `run.json`, `lifecycle.jsonl`, `annotations.jsonl`, `annotations.head.json`, `manifest.json`, `manifest.sha256` |
| `events/` | `events.jsonl` |
| `schemas/` | `<schema_id>.json` for exactly the schema ids referenced by the sealed events log — no more, no fewer |
| `raw/<stream_id>/` | `descriptor.json`, `chunks.jsonl`, `stream_close.json`, plus `packets/`, `observations/`, `samples/` and — only at `transport_payload` — `payloads/` |
| `raw/<stream_id>/<kind>/` | exactly the artifacts named by committed chunks |
| `logs/` | anything; non-authoritative, outside the DAG by design |

Directories permitted at the root: `events/`, `schemas/`, `raw/`, `logs/`.

Each of these MUST invalidate a sealed package:

```text
mystery.bin
schemas/unreferenced.json
events/extra.json
raw/<stream>/foo.bin
any structural JSON file v2 does not define
```

**Schema snapshot set.** The physical `schemas/*.json` set must equal the set of
`payload_schema` ids referenced by the sealed events log, and every one of those
paths must appear in `control_sha256`. No unreferenced snapshot may sit silently
outside the integrity DAG.

Enforcement placement: root, `events/` and `schemas/` under condition 2; the raw
tree under condition 7.

### 9.3 Canonical means canonical on disk

Where the contract says a document is RFC 8785 canonical, that is a statement
about the **bytes on disk**, not merely about what they parse to.

```text
parse the physical bytes
canonicalize the complete logical document
require: canonical bytes == physical bytes
```

For JSONL, each line's object bytes must equal its exact expected canonical
bytes, followed by exactly one `\n`.

"It parses, and canonicalizing it would produce equivalent content" is **not**
sufficient. Two byte sequences that parse alike are still two different files,
and a format that tolerates both has two spellings for one record — the same
class of ambiguity that produced several v1 findings.

Accounting per file: `lifecycle.jsonl`, `events/events.jsonl` and
`annotations.jsonl` carry a persisted `record_sha256` and it is part of the
canonical document; `chunks.jsonl` records have none in v2, so their canonical
form excludes it. This is a **conformance** rule, not a new source of truth.

---

## 9.4 Finalization order and crash windows

Dependencies made explicit, because R1-1's crash window came from an implicit
one:

```text
 1. finish and commit the final raw chunks
 2. durably write stream_close.json for every opened stream lacking one
 3. append FINALIZING to lifecycle.jsonl          (with record_sha256)
 4. append terminal CLOSED to lifecycle.jsonl     (with record_sha256)
 5. seal events/events.jsonl                      (every record has record_sha256)
 6. snapshot the referenced event schemas
 7. initialise annotations.head.json if absent
 8. verify raw physical conformance and every durable pre-manifest fact
 9. construct manifest.control_sha256
10. write manifest.json atomically
11. write manifest.sha256 atomically      <-- FINALIZATION MARKER
12. registry upsert (derived)
```

Step 2 precedes step 3 deliberately: after the terminal lifecycle record is
durable, per-stream closure must already be on disk, or a crash between them
loses it.

### 9.4.1 Crash windows

| Crash point | Lifecycle ends at | Recovery |
|---|---|---|
| before FINALIZING | `ALLOCATED` or `RECORDING` | write `RECOVERED_UNCLEAN` closure for every physical stream lacking a close file; append `FINALIZING`; append `CLOSED` / `RECOVERED_UNCLEAN` / `UNCLASSIFIED`; then it may finalize and seal the recovered package |
| during FINALIZING, before CLOSED | `FINALIZING` | same. The intended scientific outcome is **never** guessed |
| after CLOSED, before a valid manifest pair | `CLOSED` | **`INTERRUPTED_FINALIZATION`** — see below |

### 9.4.2 CLOSED without a manifest is resumable, not unreadable

A package whose terminal lifecycle record is durable but whose manifest pair is
missing is **`INTERRUPTED_FINALIZATION`**. It is *not* `UNREADABLE`, and it is
*not* automatically `RECOVERED_UNCLEAN` — the process already durably wrote the
terminal fact, and throwing that away would discard something it observed.

Recovery verifies the durable inputs: lifecycle records **and their record
hashes**, event records **and their record hashes**, `stream_close.json` for
every physical stream, raw structural integrity, and the control files.

If those are internally consistent, recovery may **resume the interrupted
sealing transaction**: write any remaining deterministically recoverable
structures, then `manifest.json`, then `manifest.sha256` — **without altering
the already-written lifecycle outcome**.

This is not promotion to `COMPLETED`. It completes a sealing transaction for an
outcome that was already durably decided. If the recorded outcome was
`ABORTED`, resuming produces a sealed `ABORTED` package.

If the durable pre-manifest state is contradictory or insufficient to reconstruct
finalization, recovery reports **`BLOCKED` / `UNREADABLE`**. It does not guess,
and it never rewrites lifecycle history.

This is the concrete payoff of R1-1 and R1-2 together: resumption is possible
only because per-stream closure is durable and the pre-seal logs are
individually verifiable.

## 10. V2-8 — physical format conformance

**A matching SHA proves identity, not conformance.** It proves the bytes are the
bytes that were sealed; it says nothing about whether those bytes are a valid
Session Package v2 artifact. C4's G1 exploited exactly that gap: a `samples`
artifact with its entire `values` column deleted verified clean.

Read-time validation, applied to every committed artifact:

### 10.1 Arrow schema

The physical `table.schema` must equal the v2 contract schema for that artifact
kind: **field names, field order, Arrow types and nullability** are all
contractual. Unrecognised Arrow **schema metadata** (key-value metadata) is
non-contractual and ignored, unless a future version defines it.

`samples` is schema-derived from the descriptor:

- `layout = "dense_fixed_list"` → `packet_seq int64`,
  `sample_index_in_packet int32`, `values fixed_size_list<float32>[n_channels]`
  where `n_channels = len(descriptor.channels)`.
- `layout = "sparse_long"` → `packet_seq int64`,
  `sample_index_in_packet int32`, `channel_id string`, `value float64`.

A dense `samples` artifact missing its `values` column **fails verification**
even when every key row is intact.

### 10.2 Row semantics — one validator, write and read

`observations` rows must satisfy, **on read as well as on write**: `kind`,
`applies_to`, `provenance` and `status` in their enum domains; `value_type` in
its domain; and **exactly one** value column non-null, the one named by
`value_type`.

v1 enforced this only in the writer, which is how C4's G2 passed. v2 requires a
**single shared validator** invoked from both paths. Two definitions of "valid
observation" is the same defect class this whole redesign exists to remove.

### 10.3 Referential integrity

- `packets.packet_seq` strictly increasing within a chunk and across the chain.
- `samples.packet_seq` and `observations.packet_seq` reference a packet in the
  same chunk.
- `sample_index_in_packet` within `[0, n_samples)` for that packet.
- dense: the sample key multiset is exactly
  `{(packet_seq, 0) … (packet_seq, N-1)}` for `n_samples = N`, each once.
- sparse: see §11.
- observations: no cardinality and no uniqueness rule — the schema names no
  observation primary key, and none is invented.

---

## 11. V2-9 — `sparse_long` primary key resolved

v1 called `(packet_seq, sample_index_in_packet)` "the sample primary key"
immediately after defining sparse rows as carrying `channel_id` as well. Those
statements are inconsistent: the pair cannot be the key if a row also carries a
channel. That contradiction is the one open `SPECIFICATION BLOCKER` from C4.

**v2 resolution.**

| Layout | Logical primary key |
|---|---|
| `dense_fixed_list` | `(packet_seq, sample_index_in_packet)` |
| `sparse_long` | `(packet_seq, sample_index_in_packet, channel_id)` |

Sparse structural rules:

- `packet_seq` references a physical packet in the same chunk;
- `0 <= sample_index_in_packet < packet.n_samples`;
- `channel_id` exists in `descriptor.channels`;
- the triple is **unique**.

**Deliberately not required:** complete channel coverage. No rule demands
`n_channels` rows per sample position, or every channel at every position.
Sparse means absence may be meaningful.

**Clarification that removes the contradiction:** for `sparse_long`,
`packets.n_samples` means **logical sample positions carried in the packet**, not
the number of long-format rows.

---

## 12. V2-7 — `bytes` audit

| Field | Disposition | Reason |
|---|---|---|
| `ChunkArtifact.bytes` | **removed** | SHA over the whole file fixes content and length |
| `manifest.inventory[].bytes` | **removed** | same |
| `events_seal.bytes` | **removed** | same |
| `lifecycle_seal.sealed_len` | **kept** | **semantic** — it defines the prefix boundary beyond which annotations legitimately append |
| `annotations.head.json.bytes` | **kept** | **semantic** — the anti-deletion pointer; a hash chain cannot prove no record was removed |
| payload frame `payload_len` | **kept** | **semantic** — part of the framing that locates the next record |
| `payload_ref.length` | **removed** | removed with `payload_ref` itself (§5) — the frame's own `payload_len` carries the extent |

The distinction is: a length that *defines a range or a boundary* stays; a length
that merely *summarises a whole file already fixed by its hash* goes.

---

## 13. V2-10 — payload contract

Unchanged from v1 in substance, minus the packet-side reference:

| `raw_capture_level` | payload artifact |
|---|---|
| `transport_payload` | required |
| `library_decoded` | must not exist |
| `synthetic` | must not exist |

**There is no `payload_ref` in v2** (§5). The packets schema is therefore
identical at every capture level, and the packet → payload relation is carried
entirely by the frames themselves.

Physical validation of a payload artifact:

- the file is a contiguous sequence of framed records with **no trailing bytes**;
- every frame has valid `magic`, a `payload_len` consistent with its extent, and
  a valid CRC-32C over its payload;
- the multiset of frame `packet_seq` values **equals** the multiset of
  `packets.packet_seq` values for that chunk — a bijection by identity, with no
  persisted pointer to keep in step.

---

## 14. V2-12 — structural validity vs scientific usability

**MAY be enforced (structural):** required column exists, Arrow type correct,
primary key unique, foreign key valid, frame CRC valid, payload belongs to its
packet.

**MUST NOT be enforced (scientific/protocol):** minimum EEG samples, minimum
duration, minimum packets, minimum amplitude, HRV quality, artefact percentage,
sample-rate tolerance.

A structurally valid zero-chunk stream remains a valid storage object. Whether a
required zero-data stream makes a session scientifically usable is a future
protocol decision, not a package-integrity one.

---

## 15. V2-14 — v1 compatibility policy

- A v2 reader opens v2 packages.
- A v2 reader encountering a v1 package **fails closed** with
  `UnsupportedSessionPackageVersionError`.
- **No in-place migration**, and no acquisition package is ever mutated.
- **v1 is not to be used for Study 001 acquisition.** No production Study 001
  recording exists, so nothing requires migration, and backward compatibility is
  not a reason to carry v1 complexity into the acquisition contract before
  recording begins.
- If a v1 development fixture ever needs inspecting, a separate, explicitly
  versioned legacy/debug reader may be built. It must not constrain v2.

---

## 16. Decision records — D27–D34

> **APPROVED** by CL-002A-R3-APPROVAL and recorded in
> [`DECISIONS.md`](DECISIONS.md), which is the authoritative index. The summaries
> below are kept for readability in context; where the wording differs,
> `DECISIONS.md` governs.

**D27 — Session Package v2 supersedes v1 before Study 001 acquisition.**
v1 survived five review rounds without converging because it persists the same
fact in several places. No Study 001 recording exists, so the cost of superseding
is zero and the cost of keeping v1 is a permanent enforcement burden. v1 remains
the historical design record. A v2 reader fails closed on v1.

**D28 — `chunks.jsonl` is the only persisted chunk commit authority.**
Per-chunk `NNNNNN.commit.json` sidecars are removed. A chunk is committed iff its
record is durably present in the chain. Any per-chunk index for performance is
derived and rebuildable, never sealed acquisition truth.

**D29 — a persisted summary must earn an authority or operational role.**
A value deterministically derivable from an authoritative representation is
derived at read time, not stored again. Removed on this basis: artifact paths,
artifact byte counts, chunk packet ranges, chunk descriptor hashes, and every
`ManifestStream` summary. Integrity metadata protecting a sole authority is not
a summary and is not covered by this rule — see D34.

**D30 — hierarchical integrity.**
`manifest.sha256` seals `manifest.json`; the manifest seals the control files and
each stream's `chunks.jsonl`; each chain seals its own raw artifacts. A raw
artifact hash is persisted exactly once. Transitive protection is protection.

**D31 — physical Arrow schema and row semantics are part of package validity.**
A matching SHA proves identity, not conformance. Field names, order, types and
nullability are contractual; unrecognised Arrow schema metadata is not.
Observation row semantics are validated by one shared validator on both write and
read.

**D32 — `sparse_long` primary key is
`(packet_seq, sample_index_in_packet, channel_id)`.**
Dense stays `(packet_seq, sample_index_in_packet)`. For sparse, `n_samples` means
logical sample positions, not long-format rows, and complete channel coverage is
not required. This resolves the v1 contradiction.

**D33 — stream closure is a per-stream durable authority.**
`raw/<stream_id>/stream_close.json` is the sole persisted authority for terminal
stream closure, written once and immutable. The manifest seals it through
`control_sha256` and does not repeat it. A fact required to resume finalization
must not live only in RAM until the final seal, which is what
`manifest.stream_close_status` would have meant. Adds `RECOVERED_UNCLEAN` to the
status domain: an operational observation, never silently mapped to `FAILED` or
`DISCONNECTED`.

**D34 — pre-seal logs retain local record integrity.**
`lifecycle.jsonl` and `events/events.jsonl` retain `record_sha256` because they
must be individually verifiable **before a manifest exists**, which is exactly
when recovery reads them. Whole-file manifest hashes serve the distinct
post-finalization sealing role. These are complementary layers with different
temporal scope, not competing authorities, so D29 does not apply.
`chunks.jsonl` drops its self-hash because its records are cross-checked against
physical artifacts recovery reads anyway.
