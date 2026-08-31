# PACKAGE_INTEGRITY_V2 — authority, integrity DAG and physical conformance

> **Status: PROPOSAL**, part of CL-002A-R3. Companion to
> `SESSION_SCHEMA_V2_PROPOSAL.md`. Supersedes `CHUNK_EQUIVALENCE.md` for v2;
> that document remains the v1 implementation audit and historical record.

This document is deliberately short. The v1 equivalence matrix needed 35
pairwise relations because v1 persisted the same fact repeatedly. **If v2 still
needed dozens of such relations, the simplification would have failed.** It needs
9.

---

## 1. Authority by fact

Exactly one persisted authority per fact. Everything else is derived at read
time.

| Fact | Authority | Derived from it |
|---|---|---|
| Session identity, protocol, participant, environment | `allocation.json` | — |
| Declared required/optional streams, devices, writer config | `run.json` | whether a stream is required |
| Stream configuration, capture level, channels, layout | `raw/<id>/descriptor.json` | expected Arrow schema; whether a payload artifact must exist |
| Whether a chunk is committed | `raw/<id>/chunks.jsonl` | chunk count, chain head, chunk id set |
| Raw artifact content | the physical artifact file | its length; its SHA |
| Packet sequence values, `n_samples` | `packets/NNNNNN.arrow` | stream packet range, per-chunk packet range |
| Sample and observation rows | their Arrow artifacts | referential integrity |
| Transport bytes, and which packet each frame belongs to | `payloads/NNNNNN.bin` | the packet → frame index |
| What happened; sealed outcome | `lifecycle.jsonl` (sealed prefix) | — |
| Post-seal downgrades | `annotations.jsonl` + `annotations.head.json` | effective outcome |
| System/operator events | `events/events.jsonl` | — |
| Terminal stream closure | **`raw/<id>/stream_close.json`** | — |
| Which control files were sealed, and their bytes | `manifest.control_sha256` | — |
| That the package was finalized | `manifest.json` + `manifest.sha256` | — |

**The manifest owns no semantic fact about any stream.** Its role is exactly
two things: finalization marker and integrity root. Terminal closure lives in a
per-stream durable file, because a fact required to resume finalization must not
exist only in RAM until the final seal.

## 2. Integrity DAG

```text
manifest.sha256
   └── manifest.json
         ├── control_sha256 ──┬── allocation.json
         │                    ├── run.json
         │                    ├── raw/<stream>/descriptor.json
         │                    ├── raw/<stream>/stream_close.json
         │                    ├── raw/<stream>/chunks.jsonl ──┐
         │                    └── schemas/<schema_id>.json    │
         ├── lifecycle_seal ───── lifecycle.jsonl[:sealed_len]│
         └── events_sha256 ────── events/events.jsonl         │
                                                              │
                    each chunks.jsonl (hash-chained records) ─┘
                          └── artifact_sha256 ──┬── packets/NNNNNN.arrow
                                                ├── observations/NNNNNN.arrow
                                                ├── samples/NNNNNN.arrow
                                                └── payloads/NNNNNN.bin
```

Every byte in the sealed package is reachable from `manifest.sha256` by exactly
one path. No hash is persisted twice.

**Outside the DAG by design:** `annotations.jsonl` and `annotations.head.json`
(written after sealing; verified by their own hash chain and head pointer),
`logs/` (operational, never authoritative), and `data/derived/` (regenerable).

**Two integrity layers, different temporal scope.** `lifecycle.jsonl` and
`events/events.jsonl` carry a per-record `record_sha256` *and* sit under a
whole-file manifest hash. That is not duplication: the record hash is the only
integrity available **before** a manifest exists, which is exactly when recovery
reads those logs; the manifest hash seals the finalized file afterwards.
`chunks.jsonl` needs no self-hash — its records are cross-checked against
physical artifacts recovery reads anyway.

## 3. Physical format contracts

Identity is not conformance. A file whose SHA matches is the file that was
sealed; it still has to *be* a valid v2 artifact.

| Artifact | Contract |
|---|---|
| `packets` | Arrow schema equals the v2 packets schema exactly — names, order, types, nullability |
| `observations` | schema equality, plus per-row semantics: enum domains, and exactly one value column non-null, the one named by `value_type` |
| `samples` (dense) | schema equality including `values: fixed_size_list<float32>[n_channels]` from the descriptor |
| `samples` (sparse) | schema equality: `packet_seq`, `sample_index_in_packet`, `channel_id`, `value` |
| `payloads` | framing: `magic`, `packet_seq`, `payload_len`, CRC-32C, contiguous frames with no trailing garbage |

Unrecognised Arrow **schema metadata** is non-contractual and ignored. Row
semantics use **one shared validator** invoked on both write and read; two
definitions of "valid observation" is the defect class this design removes.

## 4. Referential relations — the complete list

Fourteen. That is the whole of it.

| # | Relation | Authority |
|---|---|---|
| V01 | each record's `prev_record_sha256` equals the SHA-256 of the previous record's canonical bytes | `chunks.jsonl` |
| V02 | `chunk_id` strictly increases along the chain | `chunks.jsonl` |
| V03 | every committed artifact exists at its deterministic path and hashes to `artifact_sha256` | physical file |
| V04 | no raw artifact exists that no commit references | filesystem + chain |
| V05 | `artifact_sha256` key set matches the capture level (`payloads` iff `transport_payload`) | descriptor |
| V06 | `packet_seq` strictly increasing within a chunk and across the chain | `packets` rows |
| V07 | sample/observation `packet_seq` and `sample_index_in_packet` reference a real packet position; dense sample keys are exactly the expected multiset; sparse triples are unique | `packets` + `samples` / `observations` rows |
| V08 | the multiset of frame `packet_seq` values equals the multiset of `packets.packet_seq` values — a bijection by identity, with no persisted reference to keep in step | frame bytes + `packets` rows |
| V09 | every physical stream directory is declared in `run.required_streams ∪ optional_streams`; each has a valid `stream_close.json`; every required id is present with `close_status == "CLEAN"` | `run.json` + `stream_close.json` |
| V10 | `control_sha256`'s key set equals the derived expected control set exactly, in both directions | derived from the filesystem and the sealed events log |
| V11 | every JSONL record is one complete canonical object terminated by `\n`; no trailing bytes after the last newline in a finalized package | the file bytes |
| V12 | every canonical document is **canonical on disk** — re-canonicalizing the parsed document reproduces the physical bytes exactly | the file bytes |
| V13 | the package contains no immutable file outside its exhaustively defined layout; the `schemas/` set equals the schema ids referenced by the sealed events log | the defined layout |
| V14 | `lifecycle.jsonl` and `events/events.jsonl` records verify their own `record_sha256` (pre-seal integrity layer) | the record bytes |

Compare v1: **35 relations, 52 verifier findings.** The reduction comes from
deleting duplicates, not from checking less. V10–V14 were all *added* by review,
and **none is a relation between two persisted copies of a fact**: they pin a
derived key set, define what a record is, require canonical bytes, close the
layout, and verify a pre-seal integrity layer. Those are conformance checks of a
single artifact against its contract, which do not compound the way duplicate
representations do.

## 5. Collection semantics

Multiplicity and order are part of the type.

| Collection | Semantics | Duplicates possible? |
|---|---|---|
| `chunks.jsonl` | append-only ordered chain, `chunk_id` strictly increasing | detected by V02 |
| `artifact_sha256` | JSON object | **no** — impossible by construction |
| `control_sha256` | JSON object | **no** — impossible by construction |
| `stream_close.json` per stream | one immutable file per stream directory | **no** — one path, one file |
| packets rows | ordered sequence, `packet_seq` unique and increasing | detected by V06 |
| samples rows (dense) | multiset of keys equal to the expected set | detected by V07 |
| samples rows (sparse) | set of unique `(packet_seq, index, channel_id)` triples | detected by V07 |
| observations rows | collection with references; no cardinality, no uniqueness | n/a by contract |
| payload frames | bijection with packet rows | detected by V08 |
| `run.required_streams` / `optional_streams` | disjoint sets | model validator |

Three of v1's duplicate-detection relations vanish because the corresponding
lists became **maps**, where a duplicate key cannot exist.

## 6. Deliberately derived, not persisted

| Value | Derived from |
|---|---|
| artifact path | `chunk_id` + kind |
| artifact byte length | the physical file |
| chunk count, chain head, chunk id set | `chunks.jsonl` |
| per-chunk and per-stream packet range | `packets` rows |
| whether a stream is required | `run.json` |
| descriptor hash per chunk | the stream's single descriptor |
| packet → payload frame mapping | parsing the frames by `packet_seq` |
| a record's own hash | its canonical bytes |
| expected Arrow schema | descriptor `layout` + `channels` |

Each line here is a v1 persisted field that could disagree with reality. A value
that cannot be persisted wrongly cannot be persisted wrongly.

## 7. Threat-model boundary

This establishes **internal structural integrity, referential integrity and
cross-representation consistency**.

It does **not** establish cryptographic authenticity. Without signatures,
append-only media, an external transparency log or WORM storage, an actor who
coherently rewrites *every* artifact and *all* metadata into a self-consistent
alternative package cannot be distinguished from the original. That is out of
scope for Session Package v2 and is a separate, future human decision.

The standard held instead:

> If one or more representations contradict their physical or canonical
> authority, recomputing ordinary hashes must not hide the contradiction.

v2 makes that standard easier to meet by having far fewer representations that
*can* contradict one another.
