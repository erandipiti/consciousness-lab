# CHUNK_EQUIVALENCE — representation equivalence matrix

**Status: implementation-integrity map, not a new specification.** It records
which representations of a committed chunk exist, which one is authoritative for
each fact, and which relations between them are enforced. It must not, and does
not, contradict `DECISIONS.md` D8–D26 or `SESSION_SCHEMA_PROPOSAL.md`.

## Why this document exists

Three consecutive review cycles found the same shape of defect, never the same
bug twice:

| Round | Attack | What was actually missing |
|---|---|---|
| R1 | manifest describes a stream whose raw directory was deleted | manifest ↔ filesystem relation |
| R1-C1 | sidecar contradicts `chunks.jsonl` | sidecar ↔ chain relation |
| R1-C2 | sidecar `payloads: null` vs chain omitting the key | canonical-record identity, not model equality |
| R1-C2 | forged chain + sidecars lie about packet range | summary ↔ **physical data** relation |
| R1-C3 | reordered chain; symmetric `payloads: null` | chain-level ordering; key **presence** |

Each fix closed the reported attack and left an adjacent state of the same
relation unenumerated. The failure was never a missing `if`; it was an
unenumerated relation. This document enumerates them.

---

## 1. Representations of one committed chunk

| | Representation | Nature |
|---|---|---|
| A | `chunks.jsonl` canonical commit record | **authoritative commit log** |
| B | `NNNNNN.commit.json` sidecar | convenience copy, never independent authority |
| C | `packets/NNNNNN.arrow` | physical packet rows |
| D | `samples/NNNNNN.arrow` | physical sample rows |
| E | `observations/NNNNNN.arrow` | physical observation rows |
| F | `payloads/NNNNNN.bin` | physical transport bytes (`transport_payload` only) |
| G | `descriptor.json` | stream configuration, stored bytes |
| H | `ManifestStream` summary | derived summary, sealed |
| I | `manifest.inventory` entry | byte inventory, sealed |
| J | writer in-memory state | **pre-seal only**, never authority after sealing |

## 2. Authority by field

Authority is **field-specific**. No artifact is universally authoritative, and
treating one as such is what produced two of the defects above.

| Fact | Authority |
|---|---|
| Whether a chunk is committed | A |
| Exact commit record identity | A, as canonical bytes |
| `packet_seq` values physically stored | C |
| Sample rows, observation rows | D, E |
| Artifact bytes | the physical file |
| Artifact hash | hash of the physical bytes |
| Descriptor identity | G, as **stored bytes** |
| Capture level / payload expectation | G |
| Stream summary (`H`) | **derived** from verified A + C |
| Which files exist | filesystem, reconciled with I |
| `required` flag | `run.json` |
| Sidecar | never an authority; must equal A |
| Writer memory | pre-seal only; must equal A at seal time |

## 3. Required relations

| # | Dimension | A ↔ B | Relation | Source of truth |
|---|---|---|---|---|
| R01 | `chunk_id` | chain ↔ sidecar | exact | chain |
| R02 | canonical full record | chain ↔ sidecar | **exact canonical bytes** | chain |
| R03 | sidecar filename | filename ↔ `chunk_id` | `NNNNNN` == `chunk_id` | chain |
| R04 | sidecar set | sidecars ↔ chain ids | bijection | chain |
| R05 | `descriptor_sha256` | commit ↔ descriptor bytes | exact | physical descriptor |
| R06 | `descriptor_sha256` | manifest ↔ descriptor bytes | exact | physical descriptor |
| R07 | `descriptor.stream_id` | descriptor ↔ directory name | exact | directory |
| R08 | artifact paths | commit ↔ canonical name for `chunk_id` | exact | naming rule |
| R09 | artifact path reuse | commit ↔ commit | unique per stream | chain |
| R10 | artifact existence | commit ↔ filesystem | exists | filesystem |
| R11 | artifact hash | commit ↔ physical bytes | exact | physical bytes |
| R12 | payload **key presence** | commit ↔ descriptor capture level | iff | descriptor |
| R13 | payload artifact presence | filesystem ↔ capture level | iff | descriptor |
| R14 | `payload_ref` nullability | packets rows ↔ capture level | iff | descriptor |
| R15 | `payload_ref.file` | packets rows ↔ commit payload path | exact | commit |
| R16 | `payload_ref` resolution | offset/length ↔ framed bytes | resolves, CRC ok | physical bytes |
| R17 | `first_packet_seq` | commit ↔ packets rows | exact | **packets Arrow** |
| R18 | `last_packet_seq` | commit ↔ packets rows | exact | **packets Arrow** |
| R19 | intra-chunk packet order | packets rows | strictly increasing | §9.1 |
| R20 | `samples.packet_seq` | samples ↔ packets of same chunk | foreign key | §9.1 |
| R21 | `sample_index_in_packet` | samples ↔ `n_samples` | `0 <= i < n_samples` | §9.1 |
| R22 | `observations.packet_seq` | observations ↔ packets of same chunk | foreign key | §9.1 |
| R23 | observation sample index | observations ↔ `n_samples` | null, or in range | §9.1 |
| R24 | chain `chunk_id` order | chain sequence | strictly increasing | append-only chain |
| R25 | chain packet order | chain sequence | `next.first > prev.last` | §9.1 |
| R26 | `prev_record_sha256` | chain sequence | hash chain | §12.2 |
| R27 | `chunk_count` | manifest ↔ chain | exact | chain |
| R28 | `chunk_chain_head_sha256` | manifest ↔ chain | exact | chain |
| R29 | stream `first/last_packet_seq` | manifest ↔ **physical packets** | exact | packets Arrow |
| R30 | `required` | manifest ↔ `run.json` | `required == (id in required_streams)` | `run.json` |
| R31 | stream set | manifest ↔ filesystem | bijection | both |
| R32 | duplicate stream ids | manifest | unique | manifest |
| R33 | writer commits | memory ↔ chain, pre-seal | exact canonical bytes | chain |
| R34 | inventory | manifest ↔ filesystem | bijection over immutable files | both |

**R02 is canonical bytes, never `ChunkCommit == ChunkCommit`.** Model equality
proves the known fields overlap; minor-version tolerance (§17) discards unknown
fields and an omitted key normalizes to the same value as an explicit null, so
two different documents can compare equal as models. Canonical bytes are
computed from the full parsed document, never from `model_dump()`.

**R29 compares a summary to physical data, not to another summary.** Every row
above whose source of truth is a physical artifact is checked against that
artifact, not against a second claim about it.

## 4. Nullability matrix

For any nullable pair, all five states are enumerated, not only the asymmetric
ones — symmetric agreement on an *invalid* value was a live defect.

| Field | null valid when | non-null required when | zero-chunk |
|---|---|---|---|
| `payloads` (commit) | never as an explicit null — the key is **omitted** at `library_decoded` / `synthetic` (§12.2) | `transport_payload` | n/a |
| `payload_ref` (packets row) | capture level is not `transport_payload` | `transport_payload` | n/a |
| `first/last_packet_seq` (commit) | never — a committed chunk always carries packets | always | n/a |
| `chunk_chain_head_sha256` (manifest) | zero committed chunks | ≥1 chunk | `null` |
| `first/last_packet_seq` (manifest) | zero committed chunks | ≥1 chunk | `null` |
| `record_sha256` | never on disk | always | n/a |

**Symmetric-invalid is still invalid.** Both sides carrying `"payloads": null`
at a non-payload capture level agree with each other and violate §12.2; R12
tests key *presence*, which pairwise equality cannot see.

| A | B | Verdict |
|---|---|---|
| null / null | both omit the key | valid where the field is optional |
| null / null | both write explicit null where the key must be absent | **invalid** (R12) |
| null / value | — | invalid (R02) |
| value / null | — | invalid (R02) |
| value / same value | — | valid |
| value / different value | — | invalid (R02) |

## 5. Collection and ordering matrix

| State | Chain records | Sidecars | Streams | Verdict |
|---|---|---|---|---|
| zero items | valid (zero-chunk stream) | none | valid | positive |
| one item | valid | one | valid | positive |
| many items | valid | one each | valid | positive |
| missing | — | missing sidecar → invalid (R04) | manifest without raw → invalid (R31) | negative |
| extra | chunk the writer never committed → invalid pre-seal (R33) | extra sidecar → invalid (R04) | raw without manifest → invalid (R31) | negative |
| duplicate | duplicate `chunk_id` → invalid (R24) | impossible: filename derives from id (R03) | duplicate stream id → invalid (R32) | negative |
| same set, different order | reordered chain → invalid (R24, R25) | n/a — a set | n/a | negative |
| different set | invalid | invalid | invalid | negative |

Position mutations — **first, middle and last** — are each tested, because
per-chunk validation demonstrably did not imply chain-level validation.

## 6. Physical foreign keys actually enforced

- `samples.packet_seq` ∈ `packets.packet_seq` of the same chunk (R20)
- `0 <= samples.sample_index_in_packet < packets.n_samples` for that packet (R21)
- `observations.packet_seq` ∈ `packets.packet_seq` of the same chunk (R22)
- `observations.sample_index_in_packet` is null, or in range for that packet (R23)

These are structural consequences of §9.1's own statements — that
`(packet_seq, sample_index_in_packet)` is the sample primary key, and that
`n_samples` is "samples carried in this packet". They are **not** scientific
criteria.

## 7. Zero-chunk semantics

A stream with zero committed chunks is **structurally valid**. `chunks.jsonl`
exists and is empty, `chunk_count` is 0, and `chunk_chain_head_sha256`,
`first_packet_seq` and `last_packet_seq` are all null. Whether zero data is
scientifically usable is a future protocol decision and is deliberately not
decided here.

## 8. Leaf-level decomposition (CL-002B-R1-C4)

C3 decomposed *records* into fields but treated three things as atomic that are
themselves multi-field representations: a **byte-framed artifact**, a **row
set**, and a **list**. That was one level too shallow, and produced F1, F2 and
F3. Every compound representation is decomposed here until the remaining leaves
are physical bytes, scalars, set/list membership, ordering, cardinality or a
foreign key.

### 8.1 Artifact references — three leaves, not one

`ChunkArtifact` carries `path`, `sha256` **and** `bytes`. All three are
independently falsifiable and all three are checked:

| Leaf | Authority |
|---|---|
| `path` | the canonical name for this `chunk_id` |
| `sha256` | SHA-256 of the physical file bytes |
| `bytes` | the physical file's `stat().st_size` |

A matching SHA does **not** validate the record: `bytes` is a separate claim
about the same file, and it was previously unchecked (F3a). This applies to
`packets`, `observations`, `samples`, `payloads` and every `manifest.inventory`
entry.

### 8.2 Payload frame — F is not opaque bytes

Each framed record decomposes into `magic`, `packet_seq`, `payload_len`,
payload bytes, `crc32c`, frame offset and total extent.

| Leaf | Authority | Relation |
|---|---|---|
| `magic` | physical bytes | present at `payload_ref.offset` |
| `packet_seq` | physical frame bytes | **equals the referencing packet row's `packet_seq`** |
| `payload_len` | physical frame bytes | equals `payload_ref.length` |
| `crc32c` | physical frame bytes | matches the payload |
| frame ↔ packet row | both | **bijection** |

**A valid CRC proves the frame is internally intact. It does not prove the frame
belongs to the packet that references it** — that was F1. §9.1 defines the log
as records "each tagged with its `packet_seq`", one per received packet, so no
frame may be shared between packet rows and none may be unreferenced.

### 8.3 Samples — a row SET with layout-specific identity

**`dense_fixed_list`.** For a packet declaring `n_samples = N`, the sample keys
must be exactly `(packet_seq, 0) … (packet_seq, N-1)`, **each exactly once**.
Comparison is by **multiset**, not set: a duplicated key would otherwise mask a
missing one, which is precisely how a deleted sample survived (F2b). This
detects missing rows, duplicate keys, extra rows, out-of-range indices and rows
referencing an absent packet in one relation. `n_samples = 0` expects an empty
key set. Storage integrity, **not** a minimum-sample threshold.

**`sparse_long`.** Cardinality and uniqueness are **NOT enforced**. See §10.

### 8.4 Observations — references only

Observations are not a complete set by contract, and the schema names no
observation primary key, so **no cardinality and no uniqueness rule is
invented**. Only the structural references are checked: `packet_seq` must
reference a packet in the same chunk, and a non-null `sample_index_in_packet`
must be in range. A regression test pins that removing an observation row is
still valid.

### 8.5 Inventory — a bijection, not a set

`manifest.inventory` is a list representing a bijection over in-scope immutable
files. Duplicate paths are rejected **before** any set comparison; collapsing it
with `{e.path for e in inventory}` is what let a duplicate entry disappear
(F3b).

## 9. Collection semantics

Multiplicity and order are part of the type. Using a set where a bijection or a
sequence is meant is a defect, not a shortcut.

| Collection | Semantics |
|---|---|
| `chunks.jsonl` | append-only ordered chain, `chunk_id` strictly increasing |
| sidecars | mapping `chunk_id` → canonical copy, bijection with the chain |
| packets rows | ordered sequence, `packet_seq` strictly increasing |
| samples rows (dense) | **multiset** of keys, identical to the expected key set |
| samples rows (sparse) | unresolved — see §10 |
| observations rows | collection with structural references, no cardinality rule |
| payload frames | bijection with packet rows |
| `Manifest.streams` | keyed collection, `stream_id` unique |
| `Manifest.inventory` | **bijection** by path |
| `Run.required_streams` / `optional_streams` | sets, disjoint |

## 10. SPECIFICATION BLOCKER — `sparse_long` sample identity

§9.1 states `(packet_seq, sample_index_in_packet)` is "the sample primary key",
immediately after defining `sparse_long` rows as
`packet_seq, sample_index_in_packet, channel_id, value`.

Those two statements are **inconsistent for the sparse layout**: if that pair
were the primary key, a sparse stream could hold only one row per sample
position, which contradicts carrying a `channel_id` column at all. The key
presumably includes `channel_id`, but the specification does not say so.

**Consequently no cardinality or uniqueness rule is enforced for
`sparse_long`,** and none is invented. What *is* enforced for sparse streams is
only what is unambiguous: `packet_seq` references a packet in the same chunk,
and a non-null `sample_index_in_packet` is within `n_samples`.

Resolving this needs a human decision on the sparse-long identity contract. It
does not affect dense streams, and no Study 001 stream is sparse today.

## 11. Threat-model boundary

This verifier establishes **internal structural integrity, referential integrity
and cross-representation consistency**.

It does **not** establish cryptographic authenticity. Without signatures,
append-only media, an external transparency log or WORM storage, an actor who
coherently rewrites *every* raw artifact and *all* metadata into a fully
self-consistent alternative package cannot be distinguished from the original.
That is not a Session Package v1 defect and is out of scope here.

The standard this document holds itself to is narrower and testable:

> If one or more representations contradict their physical or canonical
> authority, recomputing ordinary hashes must not hide the contradiction.

## 12. Deliberately NOT enforced

- **`packet_seq` contiguity.** §9.1 requires strictly increasing, not
  consecutive. A gap is a device fact for a later ticket; requiring
  `seq[n+1] == seq[n] + 1` would be a packet-loss criterion.
- **`chunk_id` contiguity.** Strictly increasing along the chain is required;
  gaps are not rejected.
- **Any minimum** chunk, packet, sample or duration count.
- **Any sample-rate tolerance, packet-loss threshold or signal-quality rule.**
- **`close_status` against physical data** — it has no on-disk representation
  and is a writer observation by design.
