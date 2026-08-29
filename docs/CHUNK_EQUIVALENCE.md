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

## 8. Deliberately NOT enforced

- **`packet_seq` contiguity.** §9.1 requires strictly increasing, not
  consecutive. A gap is a device fact for a later ticket; requiring
  `seq[n+1] == seq[n] + 1` would be a packet-loss criterion.
- **`chunk_id` contiguity.** Strictly increasing along the chain is required;
  gaps are not rejected.
- **Any minimum** chunk, packet, sample or duration count.
- **Any sample-rate tolerance, packet-loss threshold or signal-quality rule.**
- **`close_status` against physical data** — it has no on-disk representation
  and is a writer observation by design.
