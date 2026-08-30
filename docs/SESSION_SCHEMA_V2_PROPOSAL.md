# SESSION_SCHEMA_V2_PROPOSAL — Session Package v2

> **Status: PROPOSAL. Not approved.** `DECISIONS.md` is untouched; draft records
> D27–D32 at the end await human approval. No production code was changed by the
> ticket that produced this document.

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
grew to 52 distinct findings enforcing 35 documented relations.

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

**Applied consistently:** `lifecycle.jsonl` and `events/events.jsonl` records
also drop their persisted `record_sha256` in v2, for the same reason — both live
inside the manifest-sealed region. `annotations.jsonl` **keeps** it: that file is
written after sealing, sits outside the manifest DAG, and
`annotations.head.json.head_record_sha256` names the value directly, so there it
is load-bearing rather than derived.

> This last point extends slightly beyond the field list the R3 ticket
> enumerated. It is applied because leaving per-record hashes in some sealed
> logs and not others would be an inconsistency of exactly the kind this
> redesign exists to remove. Trim it if the human review prefers the narrower
> scope.

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
  "stream_close_status": { "muse.eeg": "CLEAN", "polar.ecg": "CLEAN" },
  "control_sha256": {
    "allocation.json": "…",
    "run.json": "…",
    "raw/muse.eeg/descriptor.json": "…",
    "raw/muse.eeg/chunks.jsonl": "…",
    "raw/polar.ecg/descriptor.json": "…",
    "raw/polar.ecg/chunks.jsonl": "…",
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

**`ManifestStream` is deleted.** It is replaced by `stream_close_status`, a
mapping whose only job is the terminal closure state — the one fact about a
stream that has **no** on-disk representation anywhere else. Being a map, it
cannot carry duplicate stream ids.

| v1 `ManifestStream` field | v2 authority instead |
|---|---|
| `required` | `run.json.required_streams` |
| `descriptor_sha256` | `control_sha256["raw/<id>/descriptor.json"]` |
| `chunk_count` | `chunks.jsonl` |
| `chunk_chain_head_sha256` | `chunks.jsonl` |
| `first_packet_seq` / `last_packet_seq` | the physical `packets` artifacts |

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
         │                 ├── raw/<stream>/chunks.jsonl ──┐
         │                 └── schemas/<schema_id>.json    │
         ├── lifecycle_seal ── lifecycle.jsonl[:sealed_len] │
         ├── events_sha256 ─── events/events.jsonl          │
         └── stream_close_status (terminal fact, no other home)
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
> 2. `control_sha256`'s key set **equals** the expected control set exactly, and
>    every entry resolves to a regular file inside the package hashing to the
>    recorded value. The expected set is *derived*, not read from the manifest:
>
>    ```text
>    allocation.json
>    run.json
>    raw/<id>/descriptor.json   for every physical stream directory
>    raw/<id>/chunks.jsonl      for every physical stream directory
>    schemas/<schema_id>.json   for every payload_schema referenced in the
>                               sealed events log
>    ```
>
>    Equality in both directions: a missing key means required interpretive
>    metadata is absent, and an extra key means an unexpected control file.
>    (Duplicates are impossible: it is a map.)
> 3. the first `lifecycle_seal.sealed_len` bytes of `lifecycle.jsonl` hash to
>    `lifecycle_seal.sealed_sha256`, and `events/events.jsonl` hashes to
>    `events_sha256`. Within both sealed ranges every line must be one complete
>    canonical JSON object (§9.1), parse, and validate against its model. **No
>    per-record `record_sha256` is required or permitted** in these logs — the
>    byte-range hash is what protects them.
> 4. within the sealed prefix the terminal state is `CLOSED`, with
>    `closure_condition = CLEAN` and **sealed** `recording_outcome = COMPLETED`.
> 5. **stream set agreement**, in three parts:
>    a. every physical `raw/<id>/` directory and every `stream_close_status` key
>       is declared in `run.required_streams ∪ run.optional_streams` — a stream
>       outside the run contract is not a valid part of the package;
>    b. the key set of `stream_close_status` equals the set of physical stream
>       directories;
>    c. every id in `run.required_streams` has a physical directory and
>       `stream_close_status[id] == "CLEAN"`.
>
>    A declared **optional** stream that was never opened has no directory and no
>    close-status entry; that is valid.
> 6. no `.part`, `.tmp` or `.open` file exists anywhere in the package.
> 7. every raw stream passes **physical integrity** (§10): descriptor valid;
>    chain valid and ordered; every committed artifact exists at its
>    deterministic path and hashes to `artifact_sha256`; no unreferenced or
>    unexpected raw artifact; Arrow schema conformant; row semantics valid;
>    payload framing valid where applicable; referential integrity valid.
> 8. the **effective** recording outcome (sealed outcome after applying valid
>    downgrade annotations, failing closed on an indeterminate annotation log) is
>    `COMPLETED`.

Condition 5 now also carries the stream-set bijection, which v1 split across
conditions 5 and 7. No scientific completeness requirement is added anywhere.

---

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
| `payload_ref.length` | **removed with `payload_ref` itself** (§5) — the frame's own `payload_len` carries the extent |

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

## 16. Draft decision records — D27–D32

> **DRAFTS.** Not inserted into `DECISIONS.md`. They require human approval.

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
`ManifestStream` summary except terminal closure status.

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
