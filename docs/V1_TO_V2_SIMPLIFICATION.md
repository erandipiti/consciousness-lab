# V1_TO_V2_SIMPLIFICATION — what is removed, and why

> **Status: PROPOSAL**, part of CL-002A-R3. Companion to
> `SESSION_SCHEMA_V2_PROPOSAL.md` and `PACKAGE_INTEGRITY_V2.md`.

Every row is a persisted representation in Session Package v1, the reason it
existed, and its v2 disposition. The final column names the integrity relations
that disappear — relations the verifier had to enforce forever, each one a place
where two persisted copies could disagree.

---

## Disposition table

| v1 representation | Why it existed | v2 disposition | Reason | Integrity relations removed |
|---|---|---|---|---|
| `raw/<id>/NNNNNN.commit.json` sidecar | convenience for recovery and single-chunk verification | **removed** | `chunks.jsonl` is already the sole commit authority; the sidecar was a complete second copy that had to stay canonically identical forever. It produced two BLOCKING findings on its own | sidecar ↔ chain canonical identity; sidecar filename ↔ `chunk_id`; sidecar set ↔ chain id set (bijection); sidecar `payloads` key presence |
| `ChunkArtifact.path` | explicit reference to the artifact file | **removed** | deterministic from `chunk_id` + kind; the reader derives it | path ↔ `chunk_id`; path ↔ artifact kind; path uniqueness within a stream; path traversal safety |
| `ChunkArtifact.bytes` | convenience file-size summary | **removed** | SHA-256 over the whole file already fixes content and therefore length. It was a second claim about the same file, and went unchecked until C4 (F3a) | artifact `bytes` ↔ physical file size |
| `ChunkCommit.first_packet_seq` | quick per-chunk packet range | **removed** | the physical `packets` rows are authoritative; C2 had to add a check that the summary matched them | commit range ↔ physical packet rows (per chunk) |
| `ChunkCommit.last_packet_seq` | same | **removed** | same | same |
| `ChunkCommit.descriptor_sha256` | detect a chunk written under a different descriptor | **removed** | a stream has exactly one immutable descriptor, sealed by the manifest; a chunk cannot be under another one | commit `descriptor_sha256` ↔ physical descriptor bytes |
| `ManifestStream.required` | read a stream's requiredness without opening `run.json` | **removed** | `run.json` is the declaration of record | manifest `required` ↔ `run.required_streams` |
| `ManifestStream.descriptor_sha256` | seal the descriptor | **folded into `control_sha256`** | the descriptor is a control file; sealing it once is enough | manifest descriptor hash ↔ physical descriptor bytes (as a separate relation) |
| `ManifestStream.chunk_count` | read chunk count without parsing the chain | **removed** | `chunks.jsonl` is authoritative and cheap to count | manifest `chunk_count` ↔ chain length |
| `ManifestStream.chunk_chain_head_sha256` | verify the chain without walking it | **removed** | the chain is walked anyway for condition 7 | manifest chain head ↔ chain final record |
| `ManifestStream.first_packet_seq` / `last_packet_seq` | stream-level packet range | **removed** | derived from physical `packets` rows | manifest range ↔ physical packet rows |
| `ManifestStream` (as a list) | per-stream summary block | **deleted outright** | every field found another authority; closure moved to a per-stream durable file rather than a manifest map, because a fact needed to resume finalization must not live only in RAM until the final seal | duplicate `stream_id` detection; manifest ↔ stream close status |
| `ManifestStream.close_status` | terminal closure | **moved to `raw/<id>/stream_close.json`** | not a removal — a *relocation* to durable, pre-manifest storage. It was the one stream fact with no other home, and the fix was to give it one, not to keep it in the seal | none removed; **one authority relocated** |
| `manifest.inventory` covering raw artifacts | one flat integrity list | **narrowed to `control_sha256` (control files only)** | raw artifacts are sealed transitively through `chunks.jsonl`; hashing them in both places was pure duplication | raw artifact hash in commit ↔ same hash in inventory |
| `manifest.inventory` as a **list** of `{path, bytes, sha256}` | inventory entries | **map** `path → sha256` | duplicate paths become structurally impossible; `bytes` removed with the rest | duplicate inventory path detection; inventory `bytes` ↔ file size |
| `manifest.schemas` list of `{schema_id, path, sha256}` | snapshot inventory | **folded into `control_sha256`** | the path is deterministic (`schemas/<schema_id>.json`) | schema `path` ↔ `schema_id` |
| `events_seal` `{path, bytes, sha256}` | seal the event log | **`events_sha256` only** | path is fixed; length adds nothing over the hash | events `path` and `bytes` relations |
| `payload_ref` entirely (`file`, `offset`, `length`) | locate a packet's transport bytes | **removed** | frames carry their own `packet_seq`; parsing the chunk's payload file yields the offset/length index deterministically. Side effect: the packets Arrow schema no longer varies by capture level | `payload_ref.file` ↔ payload path; `.offset` ↔ frame boundary; `.length` ↔ framed length; `payload_ref` nullability ↔ capture level |
| `record_sha256` in `chunks.jsonl` | per-record identity | **removed** | derivable from the record's canonical bytes, and the next record's `prev_record_sha256` is that value; its fields are cross-checked against physical artifacts anyway | record self-hash ↔ record bytes, in one log |
| `record_sha256` in `lifecycle.jsonl` / `events/events.jsonl` | per-record identity | **retained** | integrity metadata protecting the sole authority, not a second authority. Before a manifest exists there is no whole-file seal, and recovery must read exactly these logs to learn what durably happened | none — a distinct pre-seal layer |
| `manifest.session_id` | bind manifest to package | **removed** | identity is owned by `allocation.json` (sealed in `control_sha256`) and the directory name; a swapped manifest is caught by the allocation hash | manifest `session_id` ↔ allocation `session_id` |
| `manifest.scope_note` | remind the reader what is out of scope | **removed** | prose owning no package fact; it belongs in the specification and the verifier, where it cannot drift from the code | — |
| `sparse_long` primary key | undefined in v1 | **`(packet_seq, sample_index_in_packet, channel_id)`** | v1 named one sample primary key while sparse rows also carry `channel_id` — an internal contradiction, and the one open specification blocker from C4 | resolves a blocker; adds one uniqueness rule |
| Arrow physical schema | not validated in v1 | **validated (new)** | a matching SHA proves identity, not conformance. C4's G1 deleted an entire `values` column and the package still completed | **adds** schema conformance per artifact kind |
| Observation row semantics on read | validated on write only | **validated on read too, shared validator (new)** | C4's G2: `value_type="uint64"` with `value_f64` populated verified clean | **adds** row semantics on read |

---

## Counting the change

| | v1 | v2 |
|---|---|---|
| Documented equivalence relations | **35** | **9** |
| Persisted copies of a record's own hash | 1 per record, 4 logs | 3 logs — dropped only where it added nothing (`chunks.jsonl`) |
| Verifier finding types | 52 | fewer, and mostly conformance rather than reconciliation |
| Persisted copies of a raw artifact hash | 2 (commit + inventory) | **1** |
| Persisted copies of an artifact path | 1 explicit + 1 implicit | **0** — derived |
| Collections where a duplicate must be *detected* | 4 | **1** (the chunk chain) |

**Relations removed: 30** — 26 from the first draft, plus 6 from the
authority/minimality review (`payload_ref`'s four, per-record self-hashes,
manifest identity), minus 2 restored by human review, which reinstated the
per-record hashes in `lifecycle.jsonl` and `events/events.jsonl` for their
pre-seal role. Two are **added** deliberately — Arrow schema
conformance and observation row semantics on read — because those close C4's
G1/G2, and neither is a relation between two persisted copies. They are checks of
a single physical artifact against its contract, which is the kind of check that
does not compound.

The point is not that v2 checks less. It is that v2 has far fewer places where
two persisted representations of one fact can drift apart.

---

## What is deliberately **not** removed

| Kept | Why |
|---|---|
| `lifecycle_seal.sealed_len` | semantic — the prefix boundary annotations append beyond |
| `annotations.head.json` (`bytes`, `record_count`, head hash) | semantic — a hash chain cannot prove no record was *removed* |
| payload frame `payload_len` | semantic — framing; locates the next record |
| `record_sha256` in `annotations.jsonl` | load-bearing — outside the manifest DAG; `annotations.head.json` names the value |
| `record_sha256` in `lifecycle.jsonl` / `events/events.jsonl` | load-bearing — the only integrity over those logs during the pre-seal window, which is exactly when recovery reads them |
| Transport payload capture | canonical raw where the boundary exposes it; unchanged from D13 |
| Separate packets / samples / observations tables | D11; denormalising implies per-sample timing that does not exist |
| Sealed vs effective outcome, downgrade-only annotations | D17, D18, D19 |
| Directory per session, UUIDv4, derived registry, RFC 8785, decimal integers | D8, D9, D15, D24, D25 |

**No provenance fact is removed.** Everything deleted above is a *copy* or a
*derivable summary*, never an observation about the study, the hardware or the
session.

---

## Added by review, not removed

Four checks were **added** deliberately. None is a relation between two persisted
copies of a fact, so none compounds the way the removals did.

| Added | Why |
|---|---|
| Arrow physical schema conformance | a matching SHA proves identity, not conformance (C4 G1) |
| Observation row semantics on read | validated on write only in v1 (C4 G2) |
| Derived control key set, exact in both directions | a missing control file could otherwise go unnoticed |
| Canonical-bytes-on-disk; closed package layout | one record must have one spelling, and the DAG's "every sealed byte is reachable" claim has to be literally enforceable |

## Relationship to D8–D26

v2 contradicts none of them. D8–D26 say what is authoritative and what the study
records; v2 changes only which redundant copies are persisted alongside. The one
v1 statement v2 supersedes is the sidecar's existence, which was an
implementation detail of §12.2 rather than a decision record.

Draft records D27–D32 in `SESSION_SCHEMA_V2_PROPOSAL.md` §16 capture what a human
would need to approve.
