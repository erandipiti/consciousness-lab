# SESSION_SCHEMA_PROPOSAL — Session Package v1 and Session Registry

**Ticket:** CL-002A (design only). **Status of this document: a PROPOSAL.**
Nothing here is a decision. `docs/DECISIONS.md` is deliberately untouched; it is
updated only after a human approves this design.

Every major design point carries one of these labels:

| Label | Meaning |
|---|---|
| **PROPOSED** | Recommended here, awaiting human approval |
| **ALREADY DECIDED** | Settled in `docs/DECISIONS.md` before this ticket; restated, not re-decided |
| **OPEN — HUMAN DECISION REQUIRED** | A person must choose; no coding agent may |
| **OPEN — HARDWARE VALIDATION REQUIRED** | Depends on device behaviour never physically measured |
| **DEFERRED SAFELY** | Not decided now, and deferring costs nothing later |

The design goal this document is written against:

> A session recorded two years from now should still be interpretable by someone
> who did not run it, without needing undocumented assumptions from the original
> machine or chat history.

---

## 1. Executive recommendation

**PROPOSED.** A session is a **directory** on disk. It is the only authority.
Everything else — including the registry — is a derived index that can be
deleted and rebuilt.

Inside the package, raw data is written as **immutable, hash-chained chunks**,
and the exact transport bytes the device sent are kept as canonical raw
alongside the decoded view. Timing is never collapsed: host arrival lives in the
packet row, and every device-provided time or counter is a **row in an
observations table**, not a fixed column, because we do not yet know how many
such quantities each device exposes.

A session is `COMPLETED` only when a sealed manifest, a sealed lifecycle prefix,
a verified chunk chain and a clean close for every required stream all agree.
Nothing appended after sealing can ever promote a session to `COMPLETED`.

Ten bullets, in the order they matter:

1. Directory per session; the package is the record, the registry is a cache.
2. Raw chunks are Arrow IPC stream files — a truncated one still yields every
   complete batch. Parquet's footer makes a truncated chunk a total loss.
3. Exact device transport bytes are canonical raw for every stream. No decoder
   is written yet and no device is verified; a decode bug is recoverable with
   payloads and permanent without them.
4. `packets` and `samples` are separate tables. Denormalising packet metadata
   onto every sample row costs ~44–59 MB per stream-hour at 256 Hz **and**
   falsely implies per-sample host timing precision.
5. Device times and counters are **observations** (`name`, `unit`, `clock_id`,
   `applies_to`, `provenance`, `status`), not fixed columns. A device that turns
   out to expose two timestamps must not force us to silently pick one.
6. `lifecycle_state`, `closure_condition` and `recording_outcome` are three
   separate fields. A crash produces `RECOVERED_UNCLEAN` + `UNCLASSIFIED`; it
   never guesses between an operator abort and a power failure.
7. `COMPLETED` is establishable **only inside the sealed prefix**. Post-seal
   annotations may downgrade an outcome, never upgrade it.
8. Session IDs are opaque UUIDv4. Any timestamp inside an identifier is a second
   chronology that outlives every promise not to parse it.
9. Derived artifacts live **outside** the sealed package, so a manifest written
   once at finalization never goes stale.
10. Every hardware-dependent claim is stored as `{value, status: verified |
    assumed, source, observed_at}`. Today every one of them reads `assumed`.

---

## 2. Design principles

These are restatements of existing repository decisions, not new ones.

1. **Raw is immutable.** Written once, never reopened for write. — ALREADY DECIDED
2. **Derived never replaces raw.** — ALREADY DECIDED
3. **Acquisition does not interpret.** No classification, no scoring, no
   thresholds, no feedback. — ALREADY DECIDED
4. **Timing provenance survives.** The eight quantities in `docs/TIMING.md` stay
   distinguishable. — ALREADY DECIDED
5. **Every allocated session is preserved**, completed, aborted or failed. — ALREADY DECIDED
6. **No invented science.** No threshold, band, duration or state definition
   originates here. — ALREADY DECIDED

And four that this design adds:

7. **One authority per question.** PROPOSED. If two files can answer the same
   question, one of them is wrong eventually.
8. **Null means "not provided".** PROPOSED. Never zero, never a default, never
   an interpolation, never the host clock standing in for a device clock.
9. **Acquisition is dumb on purpose.** PROPOSED. No dedup, no reordering, no
   repair, no gap flags. A flag computed by a buggy acquisition build would
   freeze a wrong observation into immutable data.
10. **Recovery reports; it does not repair.** PROPOSED. Orphan files are
    surfaced, never silently adopted.

---

## 3. Session package directory structure

**PROPOSED.** Q1 answer: directory per session.

```text
data/
  sessions/<session_id>/                  # THE sealed acquisition package
    allocation.json                       # immutable after allocation
    run.json                              # sealed at RECORDING_START
    lifecycle.jsonl                       # append-only; SEALED at finalization
    annotations.jsonl                     # post-seal, hash-chained, downgrade-only
    events/
      events.jsonl                        # sealed before the manifest
    schemas/
      <schema_id>.json                    # snapshot of every schema used
    raw/<stream_id>/
      descriptor.json                     # sealed at stream open
      payloads/000000.bin                 # canonical raw transport bytes
      packets/000000.arrow
      samples/000000.arrow
      observations/000000.arrow           # device times + counters
      chunks.jsonl                        # hash-chained commit index
      000000.commit.json                  # per-chunk sidecar
    manifest.json                         # written once, at finalization
    manifest.sha256
    logs/                                 # OUT of manifest scope, non-authoritative

  derived/<session_id>/<artifact_id>/     # OUTSIDE the sealed package
    artifact.json
    <outputs>

  registry.sqlite                         # fully derived index
```

Why not a single container file (HDF5, zip, one SQLite blob): a container puts a
central directory structure in the crash path, where one bad write can make a
whole session unreadable. A directory fails file by file. Every file here is
independently closed, independently hashed and independently readable.

`logs/` is deliberately outside manifest scope. Logs are often still being
written when finalization runs; including them would either break the hashes or
make the inventory incomplete. They are operational, never authoritative.

---

## 4. Session ID strategy

**PROPOSED.** Q3 answer: **UUIDv4**, canonical lowercase, as the directory name.

```text
data/sessions/9f2c1e40-6b3a-4d51-8e77-0a1b2c3d4e5f/
```

| Property | Answer |
|---|---|
| Format | RFC 4122 UUIDv4, stdlib `uuid.uuid4()`, no dependency |
| Uniqueness | 122 random bits, **plus** `os.mkdir` fails atomically if the path exists |
| Sortable by time | **No, deliberately** |
| Timestamp part of identity | **No, deliberately** |
| Collision behaviour | `mkdir` raises; regenerate and retry; never merge |
| Reveals participant | No |

The decisive argument is against embedding time, and it applies equally to
UUIDv7 and to a `20260828T142530Z-3f9a1c2b` scheme: a rule saying "nothing may
parse the ID for time" is unenforceable across years and across people. A wrong
host clock at allocation would then leak permanently into directory names,
sorted listings and backup ordering. Chronology belongs in a field that can
carry its own provenance — `allocation.json.allocated_at` — and in the registry
index, both of which can be corrected without touching identity.

Uniqueness is enforced by the filesystem rather than a database constraint
because the filesystem is where the data lives and the database is disposable.

---

## 5. Session lifecycle

**PROPOSED.** Three orthogonal fields. Collapsing them is how false-complete
states get built.

```text
lifecycle_state    ALLOCATED -> RECORDING -> FINALIZING -> CLOSED
closure_condition  CLEAN | RECOVERED_UNCLEAN                 (set at close)
recording_outcome  COMPLETED | ABORTED | TECHNICAL_FAILURE | UNCLASSIFIED
```

`UNCLASSIFIED` is **not** a fourth scientific outcome. It means "no human has
classified this yet". The three outcomes the repository requires stay fully
distinguishable; `UNCLASSIFIED` exists so that recovery never has to guess
between "the operator stopped it" and "the power died" — different facts that
only a person knows.

**Valid transitions**

| From | To | Trigger |
|---|---|---|
| ALLOCATED | RECORDING | first stream opened |
| ALLOCATED | FINALIZING | aborted before any device connected |
| RECORDING | FINALIZING | stop requested, or fatal error |
| FINALIZING | CLOSED | manifest sealed |

**Invalid, and rejected loudly:** anything out of `CLOSED`; `RECORDING ->
ALLOCATED`; skipping `FINALIZING`; any transition written by a process that did
not allocate the session.

**What a crash does:** nothing. A crash writes no transition at all. That
absence is the signal. A recovery pass discovers the session and closes it as
`closure_condition=RECOVERED_UNCLEAN, recording_outcome=UNCLASSIFIED`.

**Append-only or mutable?** Append-only, in `lifecycle.jsonl`, which is sealed
at finalization. The registry's `sessions` row is a materialized cache of that
file and is never consulted for truth.

**Reclassification, and the rule that closes the false-complete hole:** a human
may reclassify later. That is a new hash-chained record in `annotations.jsonl`,
carrying actor, timestamp and reason. The original record is never edited. But:

> **A post-seal annotation may only DOWNGRADE an outcome.** It may move
> `UNCLASSIFIED` to any of the three, and it may move `COMPLETED` to `ABORTED`
> or `TECHNICAL_FAILURE`. It may **never** produce `COMPLETED`. `COMPLETED` is
> establishable only inside the sealed lifecycle prefix, at a clean
> finalization.

Without that rule, appending one line to a text file turns an aborted session
into a completed one without invalidating any hash. Codex constructed exactly
that attack in Pass 2 (finding G1).

---

## 6. Registry design

**PROPOSED.** SQLite (`sqlite3`, Python standard library — **no new
dependency**), WAL mode, at `data/registry.sqlite`. **Fully derived.**

Rejected: DuckDB — an analytical engine, not an operational state store, and it
is already a dependency for analysis where it belongs. JSONL — no atomic
multi-row transitions and awkward locking. A second directory of JSON files —
reimplements a database badly.

```sql
CREATE TABLE sessions (            -- materialized cache of lifecycle.jsonl
  session_id            TEXT PRIMARY KEY,
  package_path          TEXT NOT NULL,
  allocated_at_utc_ns   INTEGER NOT NULL,
  protocol_id           TEXT,
  protocol_version      TEXT,
  participant_pseudonym TEXT,
  lifecycle_state       TEXT NOT NULL,
  closure_condition     TEXT,
  recording_outcome     TEXT,
  outcome_reason        TEXT,
  manifest_sha256       TEXT,
  scanned_at_utc_ns     INTEGER NOT NULL   -- provenance of this derived row
);

CREATE TABLE session_transitions ( -- mirror of lifecycle.jsonl + annotations
  session_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  record_kind TEXT NOT NULL,       -- 'lifecycle' | 'annotation'
  state       TEXT,
  outcome     TEXT,
  actor       TEXT,
  reason      TEXT,
  utc_ns      INTEGER NOT NULL,
  PRIMARY KEY (session_id, record_kind, seq)
);

CREATE TABLE streams (
  session_id TEXT NOT NULL, stream_id TEXT NOT NULL,
  device_kind TEXT, modality TEXT, required INTEGER NOT NULL,
  close_status TEXT, descriptor_sha256 TEXT,
  PRIMARY KEY (session_id, stream_id)
);

CREATE TABLE registry_meta (       -- how this index was built
  rebuilt_at_utc_ns INTEGER NOT NULL,
  scanned_root      TEXT NOT NULL,
  package_count     INTEGER NOT NULL
);
```

The registry answers: which sessions were allocated, which completed, which were
aborted, which failed technically, when they started, what protocol, where the
package is, whether finalization succeeded, and why a session was aborted — all
by cross-session query, all rebuildable.

**Atomicity and crash behaviour.** Because the registry is derived, a crash
during a registry write is harmless: `rebuild` drops and rescans. The ordering
in §12 guarantees no state exists where an allocation lives only in the
registry.

---

## 7. Metadata contract

**PROPOSED.** Q4 answer. Facts are split by **when they become known**, which is
what makes "immutable after allocation" achievable without placeholders.

### 7.1 `allocation.json` — immutable after allocation

```json
{
  "schema_name": "session_package",
  "schema_version": "1.0",
  "session_id": "9f2c1e40-6b3a-4d51-8e77-0a1b2c3d4e5f",
  "allocated_at": {
    "utc_ns": 1787923530123456789,
    "monotonic_ns": 884413221000,
    "host_clock_id": "CLOCK_REALTIME",
    "monotonic_clock_id": "CLOCK_MONOTONIC",
    "utc_quality": "unknown"
  },
  "origin": { "kind": "recording" },
  "protocol": { "id": "phase0", "version": "UNSPECIFIED", "sha256": null },
  "participant_pseudonym": "P001",
  "host": { "hostname_alias": "workstation-01", "os": "Linux 6.6.87.2", "arch": "x86_64" },
  "software": { "repo_commit": "d809caf...", "dirty": false },
  "environment": { "uv_lock_sha256": "…", "python_version": "3.11.15" }
}
```

`protocol.version` is `"UNSPECIFIED"` and `protocol.sha256` is `null` because no
Phase 0 protocol exists (`docs/PHASE0_PROTOCOL.md`). That is recorded honestly
rather than filled with a plausible value. — OPEN — HUMAN DECISION REQUIRED

### 7.2 `run.json` — sealed at `RECORDING_START`

Holds what is only knowable once hardware is present: devices actually found,
adapter and SDK versions, host BLE adapter identity, the declared
`required_streams` set, and writer configuration.

```json
{
  "sealed_at": { "utc_ns": …, "monotonic_ns": … },
  "required_streams": ["muse.eeg", "polar.ecg"],
  "optional_streams": ["muse.imu", "qtpy.marker"],
  "devices": [
    { "device_alias": "muse-01", "device_kind": "muse_s_athena",
      "firmware": { "value": null, "status": "assumed", "source": "not read", "observed_at": null },
      "library": { "name": "brainflow", "version": "5.x.y", "status": "verified", "source": "importlib.metadata", "observed_at": "…" } }
  ],
  "writer_config": {
    "chunk_max_seconds": 30, "chunk_max_rows": 100000,
    "clock_snapshot_interval_seconds": 60,
    "note": "Writer configuration only. NOT analysis epoching and NOT a scientific parameter."
  }
}
```

### 7.3 Authority table — no field has two homes

| Question | Authoritative file |
|---|---|
| What is this session? | `allocation.json` |
| What hardware and software ran it? | `run.json` + `raw/<stream>/descriptor.json` |
| What happened, and what is the outcome? | `lifecycle.jsonl` (sealed prefix) + `annotations.jsonl` |
| What did the system and operator observe? | `events/events.jsonl` |
| Which files exist and what are their bytes? | `manifest.json` |
| Cross-session queries | `registry.sqlite` (derived, never truth) |

`manifest.json` carries **no outcome field at all**. That is deliberate: in the
first draft of this design both the manifest and the lifecycle log claimed the
outcome, which Codex correctly identified as a permanent conflicting truth
(finding F3).

---

## 8. Stream descriptor

**PROPOSED.** Sealed at stream open, hashed, and referenced by every chunk.

```json
{
  "stream_id": "muse.eeg",
  "descriptor_version": 1,
  "device_alias": "muse-01",
  "device_kind": "muse_s_athena",
  "modality": "eeg",
  "layout": "dense_fixed_list",
  "channels": [
    { "index": 0, "channel_id": "TP9",  "unit": "uV", "dtype": "float32", "physical_meaning": "scalp electrode, 10-20 TP9" },
    { "index": 1, "channel_id": "AF7",  "unit": "uV", "dtype": "float32", "physical_meaning": "scalp electrode, 10-20 AF7" }
  ],
  "nominal_sample_rate_hz": { "value": 256, "status": "assumed", "source": "vendor documentation", "observed_at": null },
  "actual_sample_rate_hz": null,
  "device_preset": { "value": null, "status": "assumed", "source": null, "observed_at": null },
  "timing_capabilities": {
    "provides_device_time":     { "value": null, "status": "assumed", "source": null, "observed_at": null },
    "provides_packet_counter":  { "value": null, "status": "assumed", "source": null, "observed_at": null },
    "provides_sample_counter":  { "value": null, "status": "assumed", "source": null, "observed_at": null },
    "packet_counter_width_bits":{ "value": null, "status": "assumed", "source": null, "observed_at": null },
    "samples_per_packet":       { "value": null, "status": "assumed", "source": null, "observed_at": null }
  },
  "channel_layout_provenance": {
    "status": "assumed",
    "source": "vendor documentation, not measured on the device",
    "observed_at": null
  },
  "hardware_verification": { "status": "unverified", "ref": "docs/HARDWARE.md" },
  "extensions": { "muse_s_athena": {} }
}
```

Two rules do the load-bearing work:

- **Channel identity lives here, never in a column name.** Column names cannot
  safely carry channel order, units, physical meaning, or a mid-session rename.
- **Every hardware-dependent claim is a `{value, status, source, observed_at}`
  quad.** Today every `status` reads `assumed` and most values are `null`, which
  is the honest state of `docs/HARDWARE.md`.
- **The channel layout is itself a hardware claim.** Channel order, channel
  identity, units and physical meaning are all things we believe about a device
  we have never connected, so the descriptor carries
  `channel_layout_provenance: {status, source, observed_at}` covering the whole
  `channels` array. An individual channel entry may override it with its own
  quad once that specific channel is verified. Without this, a
  vendor-documentation guess about which electrode is index 0 would read as
  established fact for the life of every recording.

`actual_sample_rate_hz` does not exist at acquisition. It is derived.

Device-specific quirks go in `extensions.<device_kind>` — namespaced, never
promoted into the generic schema, so an unverified assumption about one device
cannot contaminate the common contract.

**A mid-session configuration change opens a NEW stream**
(`polar.ecg#2`) with its own descriptor; the previous stream closes with
`close_status = RECONFIGURED`. Streams are never blended. — PROPOSED

---

## 9. Raw sample/packet model

**PROPOSED.** Q2 and the packet-vs-sample question.

### 9.1 Three artifacts per chunk, one commit

**`payloads/NNNNNN.bin` — canonical raw.** Length-prefixed records of the exact
bytes received from the device, each tagged with its `packet_seq`. **Always on.**

Exact framing, so that two implementations cannot disagree — **all integers
little-endian, unsigned**:

| Offset | Field | Width | Meaning |
|---|---|---|---|
| 0 | `magic` | `u32` | `0x444C5950` (ASCII `PYLD`, little-endian) — resynchronisation point after a torn record |
| 4 | `packet_seq` | `u64` | the `packets.packet_seq` this payload belongs to |
| 12 | `payload_len` | `u32` | length in bytes of the payload that follows |
| 16 | `payload` | `payload_len` | **the exact device bytes, unmodified** |
| 16 + `payload_len` | `crc32c` | `u32` | CRC-32C (Castagnoli) over the payload bytes only |

Total record size is `20 + payload_len` bytes. There is no file header and no
padding, so records are byte-adjacent and a file is a pure concatenation.

`packets.payload_ref` refers into this file as: `file` = the chunk-relative path,
`offset` = the **byte offset of the record's `magic` field** from the start of
the file, `length` = `payload_len` (the payload alone, not the framing). A
reader seeking to `offset` must find `magic`; if it does not, the reference is
broken and the reader fails closed rather than returning whatever bytes are
there.

This is the single most conservative decision in the design and it is
justified by the repository's own state: no decoder is written, and
`docs/HARDWARE.md` marks every device unverified. If our Athena parser
mis-decodes and we stored only decoded samples, the error is permanent. With
payloads it is fully recoverable by re-decoding. For BLE these packed bytes are
typically *smaller* than the decoded `float32`/`float64` samples, so the cost is
low. Turning payload capture off requires a named human decision recorded in
`docs/DECISIONS.md`.

**`packets/NNNNNN.arrow` — one row per received packet.**

| Column | Type | Null? | Meaning |
|---|---|---|---|
| `packet_seq` | int64 | no | **Ours.** Strictly increasing at arrival. Never from the device. |
| `host_arrival_monotonic_ns` | int64 | no | `time.monotonic_ns()` — the session spine |
| `host_arrival_utc_ns` | int64 | no | `time.time_ns()` — may step |
| `host_arrival_monotonic_clock_id` | string | no | which OS clock produced the monotonic value |
| `host_arrival_utc_clock_id` | string | no | which OS clock produced the UTC value |
| `n_samples` | int32 | no | samples carried in this packet |
| `payload_ref` | struct{file,offset,length} | no | exact bytes this row was decoded from |
| `decode_status` | string | no | `ok` \| `partial` \| `failed` |

Note there is **no device timestamp column and no counter column here** — see
§9.3, which is the correction Codex forced in Pass 2.

**`samples/NNNNNN.arrow` — layout declared by the descriptor.**

`layout = "dense_fixed_list"` (dense synchronous streams: EEG, ECG, ACC):

| Column | Type |
|---|---|
| `packet_seq` | int64 |
| `sample_index_in_packet` | int32 |
| `values` | `fixed_size_list<float32>[n_channels]`, ordered by the descriptor |

`layout = "sparse_long"` (markers, status, mixed-rate streams):
`packet_seq`, `sample_index_in_packet`, `channel_id`, `value`.

`(packet_seq, sample_index_in_packet)` is the sample primary key. **There are no
timestamps in `samples`.**

### 9.2 Why separate tables, with numbers

Denormalising packet metadata onto every sample row costs roughly **48–64 extra
bytes per sample**, which at 256 Hz is **~44–59 MB per stream-hour**, scaling
linearly per stream. That is the cheap objection. The expensive one is
analytical: a packet timestamp copied onto 24 sample rows *looks like* 24
per-sample measurements and will eventually be read as one. Separation makes the
derivation explicit and unavoidable.

Denormalised tables are perfectly fine as **derived** artifacts. They are not
acquisition truth.

### 9.3 `observations/NNNNNN.arrow` — device times and counters

**PROPOSED, and this is the single most important correction in this document.**

The first draft had fixed columns: one `device_timestamp`, one
`device_packet_counter`, one `device_sample_counter`. Codex classified that
BLOCKING (finding G2/G3): if a device turns out to expose *two* device times —
say a sample-acquisition time and a packet-assembly time, or a library-
synthesized timestamp next to a device one — a single column silently picks a
winner, forever, and the other is unrecoverable. We cannot know which case we
are in, because no device has ever been connected.

So device-provided timing is a **sparse observations table**:

| Column | Type | Null? | Meaning |
|---|---|---|---|
| `packet_seq` | int64 | no | which packet this observation came with |
| `sample_index_in_packet` | int32 | yes | null = applies to the whole packet |
| `kind` | string | no | `time` \| `counter` |
| `name` | string | no | device-namespaced, e.g. `brainflow.timestamp`, `polar.ecg_ts` |
| `value_type` | string | no | `int64` \| `uint64` \| `float64` \| `decimal_string` |
| `value_i64` | int64 | yes | set iff `value_type = "int64"` |
| `value_u64` | uint64 | yes | set iff `value_type = "uint64"` — counters are frequently unsigned |
| `value_f64` | float64 | yes | set iff `value_type = "float64"` — library timestamps are frequently floats |
| `value_str` | string | yes | set iff `value_type = "decimal_string"` — exact lexical form, for anything the other three would round |
| `unit` | string | yes | null when the device does not state one |
| `clock_id` | string | yes | which clock this value is expressed on |
| `applies_to` | string | no | `sample_acquisition` \| `packet_assembly` \| `transmission` \| `host_receipt` \| `unknown` |
| `provenance` | string | no | `device_provided` \| `library_provided` |
| `status` | string | no | `verified` \| `assumed` \| `unknown` |

**Exactly one of the four value columns is non-null**, selected by `value_type`.
A single `int64` column was rejected: BrainFlow surfaces a floating-point
timestamp and device counters are commonly unsigned, so coercing everything into
a signed 64-bit integer would silently round or wrap values **at acquisition
time, into immutable data**. `decimal_string` is the escape hatch for any value
the other three cannot hold exactly. The rule is the same as everywhere else in
this design: store what arrived, in the form it arrived.

Today every row would carry `applies_to = "unknown"` and `status = "assumed"`,
which is exactly right — `docs/TIMING.md` says a device timestamp's meaning is
device-specific and must be established per device, not assumed.

The table is sparse (a handful of rows per packet), so the cost is negligible
next to the sample data.

### 9.4 What acquisition must never do

No deduplication. No reordering. No repair. No interpolation. **And no gap
flags** — a flag computed by a buggy acquisition build would freeze a wrong
observation into immutable data. Duplicated and out-of-order packets are
recorded exactly as received; `packet_seq` preserves as-received order
regardless of what the device counters do.

---

## 10. Timing representation

**PROPOSED.** No threshold is proposed. `docs/TIMING.md` freezes none, and this
document freezes none either.

### 10.1 Where each quantity lives

| `docs/TIMING.md` quantity | Where it lives | Raw or derived |
|---|---|---|
| Host arrival time | `packets.host_arrival_monotonic_ns` and `host_arrival_utc_ns`, each with its own `*_clock_id` | **Raw** |
| Device-provided time | `observations` rows with `kind="time"` | **Raw** |
| Packet / sample counter | `observations` rows with `kind="counter"` | **Raw** |
| Sample position in packet | `samples.sample_index_in_packet` | **Raw** |
| Reconstructed sample time | `data/derived/…` only | **Derived** |
| Offset | derived, from paired host/device observations | **Derived** |
| Clock drift | derived, from offset over time | **Derived** |
| Packet-arrival jitter | derived, from `host_arrival_monotonic_ns` deltas | **Derived** |
| Sample-timing uncertainty | derived, carried with its inputs | **Derived** |

**Nothing derived is ever written under `raw/`.** The `raw/` tree contains no
reconstructed time at all — not as a convenience column, not as a cache.

### 10.2 One row is one sample; one packet row is one packet

`samples` is one row per sample because a sample is the unit of measurement and
the unit of future analysis. `packets` is one row per packet because arrival is
a packet-level event and pretending otherwise is the specific error §9.2
describes. Chunks are a *storage* unit and carry no analytical meaning
whatsoever; chunk boundaries must never be readable as epoch boundaries.

### 10.3 Host clock quality

`clock_snapshot.v1` is a **required** event schema, emitted at minimum at
`RECORDING_START` and `FINALIZE_START`, and periodically at
`writer_config.clock_snapshot_interval_seconds`:

```json
{ "event_name": "CLOCK_SNAPSHOT", "payload_schema": "clock_snapshot.v1",
  "payload": { "monotonic_ns": …, "utc_ns": …,
               "monotonic_clock_id": "CLOCK_MONOTONIC", "utc_clock_id": "CLOCK_REALTIME",
               "sync_source": "unknown", "sync_status": "unknown", "utc_quality": "unknown" } }
```

`unknown` is written explicitly rather than omitted, so a reader can tell "we did
not know" from "we did not look". A UTC step during a session then appears as a
visible discontinuity between the monotonic and UTC series instead of silent
corruption.

### 10.4 Minimum needed to reconstruct device-relative sample time later

Per packet: `packet_seq`, `host_arrival_monotonic_ns`, `n_samples`, and **at
least one** observation row (a device time, a packet counter, or a sample
counter). Per stream: `nominal_sample_rate_hz` and the counter wrap width. Plus
the clock snapshots.

If a device provides none of the three, reconstruction rests on host arrival plus
nominal rate, and the resulting uncertainty must be **representable, not
hidden**. Whether each device provides any of them is
**OPEN — HARDWARE VALIDATION REQUIRED**.

### 10.5 Detecting drops without trusting any reconstructed time

Compare successive `counter` observations against an expected increment,
applying wrap only when `packet_counter_width_bits` is known; and compare summed
`n_samples` against sample-counter deltas. `packet_seq` independently preserves
host-observed order. If no counter exists, loss detection is reported as
`not_assessable` — never estimated from timestamps.

---

## 11. Event model

**PROPOSED.** Q5 answer.

**The boundary:** `raw/` records what the **devices** sent. `events/` records
what the **system and the operator** did.

A QT Py button press produces **both**, and this is not duplication:

- the electrical transition is raw stream data in `raw/qtpy.marker/`, with its
  own device timing;
- a semantic marker event **references** it by
  `{stream_id, packet_seq, sample_index_in_packet}`.

The event carries *meaning* — which protocol block, which operator action —
never a copy of the measurement. Raw preserves the pulse; the event preserves the
intent. Codex classified raw-only as BLOCKING (finding F1) precisely because the
pulse without the intent loses actor, protocol meaning and mapping version.

**One shared `events/events.jsonl`.** A single append-only file with a single
writer gives total host-observed order for free; per-stream event logs would make
cross-stream ordering a reconstruction problem.

```json
{ "event_seq": 42,
  "event_name": "BLOCK_START",
  "payload_schema": "block_start.v1",
  "origin": "protocol",
  "host_arrival_monotonic_ns": 884413221000,
  "host_arrival_utc_ns": 1787923530123456789,
  "host_arrival_monotonic_clock_id": "CLOCK_MONOTONIC",
  "host_arrival_utc_clock_id": "CLOCK_REALTIME",
  "raw_ref": { "stream_id": "qtpy.marker", "packet_seq": 9912, "sample_index_in_packet": 3 },
  "payload": { "block_index": 2 } }
```

| Decision | Answer |
|---|---|
| One shared stream or several? | **One**, `events/events.jsonl` |
| Multiple timestamp sources per event? | Yes — host always; `raw_ref` links to device timing when it exists |
| Origin distinguished? | Yes — `system` \| `protocol` \| `operator` \| `device_link` |
| Payloads? | Yes, free-form dict, but always behind a named `payload_schema` |
| Names versioned? | Per-schema (`block_start.v1`), **not** one global vocabulary integer |

Illustrative names — `SESSION_ALLOCATED`, `RECORDING_START`, `RECORDING_STOP`,
`BLOCK_START`, `BLOCK_END`, `USER_MARKER`, `DEVICE_CONNECTED`,
`DEVICE_DISCONNECTED`, `TECHNICAL_ERROR`, `CLOCK_SNAPSHOT` — are examples, not a
frozen vocabulary. **No Focus-specific event semantics are introduced.**

**A changed meaning requires a new name.** Redefining an existing event name is
forbidden by `AGENTS.md` §3, and no schema mechanism can rescue a reused name.

**Every schema used in a session is snapshotted into `schemas/`** and inventoried
in the manifest, so a package is interpretable years later with no access to this
repository (Codex finding F12).

---

## 12. Crash-safe writing

**PROPOSED.** One workstation. No distributed durability.

### 12.1 Allocation order — why no orphan can exist

```text
1. os.mkdir(data/sessions/<uuid>)            # atomic uniqueness gate
2. write allocation.json.tmp -> fsync -> rename -> fsync(dir)
3. append ALLOCATED to lifecycle.jsonl -> fsync
   --- acquisition may not start before this point ---
4. registry INSERT                            # derived; failure is harmless
```

The package is created **before** the registry row. There is therefore no state
in which an allocation exists only in the registry, which is what makes the
registry fully derived (Codex finding F10). If step 4 fails, `rebuild` finds the
package. If step 1 or 2 fails, nothing exists and no acquisition can start.

### 12.2 Chunk commit

Per chunk, in this order:

```text
payloads/NNNNNN.bin.part      -> flush -> fsync(fd) -> close -> rename -> fsync(dir)
packets/NNNNNN.arrow.part     -> flush -> fsync(fd) -> close -> rename -> fsync(dir)
observations/NNNNNN.arrow.part-> flush -> fsync(fd) -> close -> rename -> fsync(dir)
samples/NNNNNN.arrow.part     -> flush -> fsync(fd) -> close -> rename -> fsync(dir)
NNNNNN.commit.json.tmp        -> fsync -> rename -> fsync(dir)      # sidecar
append the same record to chunks.jsonl -> fsync                     # hash chain
```

```json
{ "chunk_id": 123, "prev_record_sha256": "…",
  "payloads":     { "path": "payloads/000123.bin",      "sha256": "…", "bytes": 918273 },
  "packets":      { "path": "packets/000123.arrow",     "sha256": "…", "bytes": 40112 },
  "observations": { "path": "observations/000123.arrow","sha256": "…", "bytes": 8104 },
  "samples":      { "path": "samples/000123.arrow",     "sha256": "…", "bytes": 1508992 },
  "first_packet_seq": 30000, "last_packet_seq": 30749,
  "descriptor_sha256": "…", "record_sha256": "…" }
```

**`chunks.jsonl` is the authoritative commit log. A chunk is real if and only if
its record appears there.** The `NNNNNN.commit.json` sidecar is a convenience
copy for recovery tooling and for verifying a single chunk without reading the
whole index; **a sidecar on its own is not a commit.** A crash after the sidecar
is renamed but before the `chunks.jsonl` append lands leaves the chunk
**uncommitted and orphaned** — recovery reports it and does not adopt it, exactly
as it treats a leftover `.part`.
The first draft sealed packets and samples independently, which Codex flagged as
SERIOUS (finding F7) because it permits committed packet metadata whose sample
values are permanently gone. One commit record covering all four files makes the
chunk atomic: a crash leaves at most one uncommitted chunk with orphan files,
which recovery reports and never adopts.

`prev_record_sha256` chains the index, so a truncated or edited `chunks.jsonl` is
detectable rather than silently shorter (finding F15).

**Canonical serialization — required for every hashed JSON or JSONL record.**
Two implementations must produce the same bytes for the same record, or every
hash in this design is meaningless:

- UTF-8, no BOM.
- Object keys sorted by Unicode code point.
- No insignificant whitespace: separators are `,` and `:` exactly.
- Non-ASCII characters emitted literally, never `\uXXXX`-escaped.
- Integers emitted without a decimal point or exponent; floats emitted with
  `repr()`-equivalent shortest round-trip formatting.
- In JSONL files, exactly one canonical record per line, terminated by `\n`.
  The terminating newline is **not** part of the hashed bytes.

In Python this is
`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.

**`record_sha256` is computed over the canonical serialization of the record with
the `record_sha256` key absent**, then inserted. A verifier removes the key,
re-serializes canonically, and compares. `prev_record_sha256` is the previous
record's `record_sha256` value; the first record in a chain uses
`"0" * 64`.

Arrow IPC **stream** format is chosen over Parquet for raw specifically because a
truncated IPC stream still yields every complete record batch, while a truncated
Parquet file — whose footer never landed — is a total loss. Derived artifacts use
Parquet, because they are re-derivable and query speed matters more there.

Chunk size: `chunk_max_seconds: 30` / `chunk_max_rows: 100000`, whichever comes
first. **Engineering constants, not scientific ones** — they bound how much
in-flight data a crash can cost, and nothing else. Recorded in
`run.json.writer_config` and explicitly labelled as writer configuration.

### 12.3 Disk full

`ENOSPC` on any raw write must: abort the in-flight chunk, refuse to write a
commit record, append a `TECHNICAL_ERROR` event and a `FINALIZING` transition,
and close. It must never produce a truncated-but-committed chunk. — PROPOSED,
and an explicit CL-002B acceptance test (§24).

**A known fatal writer error closes as `TECHNICAL_FAILURE`, not
`UNCLASSIFIED`.** The distinction is whether the process was alive and knew why
it was dying. `ENOSPC`, an I/O error, or an unrecoverable device-stack fault are
all diagnosed causes: the writer can and must record
`closure_condition = CLEAN, recording_outcome = TECHNICAL_FAILURE` with the
error in `outcome_reason`. `UNCLASSIFIED` is reserved for the case where cause
and intent are genuinely unknown — a process that died without writing
anything, which is discovered later by recovery. Collapsing the two would throw
away a fact the system actually had.

---

## 13. Manifest contract

**PROPOSED.** Written exactly once, at finalization, via tmp + fsync + rename +
fsync(dir). Before finalization it **does not exist**, and its absence is the
primary signal that a session was not cleanly closed.

```json
{
  "schema_name": "session_package", "schema_version": "1.0",
  "session_id": "9f2c1e40-…",
  "sealed_at": { "utc_ns": …, "monotonic_ns": … },
  "lifecycle_seal": { "path": "lifecycle.jsonl", "sealed_len": 8421, "sealed_sha256": "…" },
  "events_seal":    { "path": "events/events.jsonl", "bytes": 20144, "sha256": "…" },
  "streams": [
    { "stream_id": "muse.eeg", "required": true, "close_status": "CLEAN",
      "descriptor_sha256": "…", "chunk_count": 118,
      "chunk_chain_head_sha256": "…", "first_packet_seq": 0, "last_packet_seq": 88412 }
  ],
  "inventory": [ { "path": "allocation.json", "bytes": 812, "sha256": "…" } ],
  "schemas": [ { "schema_id": "block_start.v1", "path": "schemas/block_start.v1.json", "sha256": "…" } ],
  "scope_note": "logs/ and data/derived/ are OUTSIDE this manifest by design."
}
```

**The manifest carries no `outcome` field.** Outcome is owned by the lifecycle
log; the manifest owns bytes. The manifest pair is therefore a **finalization
marker, not a completion marker** — a cleanly aborted session produces an
identical, fully valid pair. The manifest binds to the outcome indirectly and
tamper-evidently, through `lifecycle_seal.sealed_len` + `sealed_sha256`: the
sealed prefix cannot be edited without detection, while legitimate post-seal
annotations remain possible as appends beyond `sealed_len`.

Nothing is appended to a hashed log after sealing, which removes the ordering
contradiction Codex found in the first draft (finding F2), where
`FINALIZE_COMPLETE` was written into a file the manifest had already hashed.

---

## 14. Checksums and finalization

**PROPOSED.**

```text
1. seal every open chunk, or leave it .part and record the fact
2. append FINALIZING to lifecycle.jsonl                        -> fsync
3. append the terminal CLOSED record: closure_condition + recording_outcome
                                                               -> fsync
4. seal events/events.jsonl (no further writes)                -> fsync
5. snapshot every schema used into schemas/
6. sha256 every in-scope file; verify every chunk chain end to end
7. write manifest.json.tmp -> fsync -> rename -> fsync(dir)
8. write manifest.sha256.tmp -> fsync -> rename -> fsync(dir)   <-- completion marker
9. registry upsert (derived)
```

The **existence of a matching `manifest.json` + `manifest.sha256` pair** is the
completion marker. Step 8 is the last durable act; a crash anywhere before it
leaves a session that is, correctly, not complete.

### The completion predicate

> A reader may conclude `recording_outcome = COMPLETED` **only if all seven
> hold**:
>
> 1. `manifest.json` exists and `manifest.sha256` matches it;
> 2. every file in `inventory` exists and hashes to its recorded value;
> 3. the first `lifecycle_seal.sealed_len` bytes of `lifecycle.jsonl` hash to
>    `lifecycle_seal.sealed_sha256`;
> 4. within that sealed prefix the terminal state is `CLOSED` with
>    `closure_condition = CLEAN` and `recording_outcome = COMPLETED`;
> 5. **every stream declared `required` in `run.json` has
>    `close_status = CLEAN`** in the manifest;
> 6. no `.part`, `.tmp` or `.open` file exists anywhere in the package;
> 7. every chunk file present under `raw/` appears in its `chunks.jsonl` hash
>    chain, and every chain verifies end to end.
>
> **No post-seal annotation can satisfy condition 4.** Annotations may only
> downgrade.

Conditions 4, 5 and the downgrade-only rule each close a false-complete state
Codex actually constructed in Pass 2 — respectively: a post-seal
`CLASSIFIED: COMPLETED` promoting an aborted session; a required device
disconnecting while the package still satisfied every other condition; and an
operator abort being reclassified upward.

---

## 15. Raw versus derived contract

**PROPOSED.**

- `data/sessions/<id>/raw/**` — immutable. Written once, never reopened.
- `data/derived/<session_id>/<artifact_id>/` — **outside the sealed package**.

Derived artifacts live outside because a manifest written once at finalization
would otherwise go stale the first time an analysis ran (Codex finding F11).
This also makes the backup story crisp: the acquisition package is a sealed,
verifiable unit; derived output is regenerable.

```json
{
  "artifact_schema_version": 1,
  "artifact_id": "psd-2026-09-01-a",
  "input_session_id": "9f2c1e40-…",
  "inputs": [ { "path": "raw/muse.eeg/samples/000000.arrow", "sha256": "…" },
              { "path": "raw/muse.eeg/descriptor.json",      "sha256": "…" } ],
  "input_manifest_sha256": "…",
  "pipeline": { "name": "…", "version": "…" },
  "code": { "repo_commit": "…", "dirty": false },
  "environment": { "uv_lock_sha256": "…", "python_version": "3.11.15" },
  "config": { },
  "generated_at_utc_ns": …,
  "outputs": [ { "path": "psd.parquet", "sha256": "…" } ]
}
```

This answers *exactly which raw bytes produced this result?* — by content hash,
not by path. **Resolution is by `(session_id, sha256)`; the relative path is a
hint.** A reader must never bind to a path whose hash does not match; it fails
closed (finding G6).

---

## 16. Replay contract

**PROPOSED.**

Replay is exposed at the **storage/analysis boundary**, not by impersonating a
device adapter. Impersonation is what makes a replay indistinguishable from a
recording downstream, and downstream is exactly where the confusion would be
permanent (Codex finding F17).

Replay **must**: reproduce sample ordering by `(packet_seq,
sample_index_in_packet)`; reproduce every raw field verbatim, including nulls;
reproduce observations rows unchanged; reproduce events in `event_seq` order.
Wall-clock pacing from `host_arrival_monotonic_ns` deltas is optional.

Replay **must never**: synthesize a field that was null; present reconstructed
time as raw; renumber `packet_seq`; or drop an observation because its
`applies_to` is `unknown`.

A replay that writes a package **must** carry in `allocation.json`:

```json
"origin": { "kind": "replay", "source_session_id": "…",
            "source_manifest_sha256": "…", "replay_tool_version": "…" }
```

A synthetic stream is the same shape with `origin.kind = "synthetic"` and a
recorded generator seed, which is what makes a synthetic session deterministic
and re-creatable.

---

## 17. Schema versioning and evolution

**PROPOSED.**

- `schema_version: "MAJOR.MINOR"` in `allocation.json` — written at allocation,
  so even a crashed, unfinalized package declares its version — and repeated in
  `manifest.json`.
- **Minor bump:** a new optional field. Readers ignore unknown fields.
- **Major bump:** removing or renaming a field, changing a unit, changing a
  column's meaning, or changing what an event name means.
- **Old packages are never migrated in place.** Raw immutability forbids it. A
  migration produces a **derived** artifact under `data/derived/`.
- **A v2 reader opening a v1 package** dispatches to a v1 reader, or fails
  closed with an explicit unsupported-version error. It never guesses and never
  upgrades.
- Event payload schemas version **independently**, per schema id
  (`block_start.v1` → `block_start.v2`), because event vocabularies evolve on
  their own clock. There is no global vocabulary integer.
- Migration code, when it is ever needed, belongs in an `analysis`-layer module
  reading v1 and writing derived v2 — never in `acquisition` or `storage`.

---

## 18. Participant pseudonym strategy

**PROPOSED.** Q7 answer.

- `participant_pseudonym` is **generated**, and validated against
  `^P[0-9]{3,6}$` at allocation. A free-form string is refused. An opaque-string
  field alone does not prevent an operator typing a real name into it (Codex
  finding F14); a regex does.
- The mapping pseudonym → person lives **outside the repository and outside
  `data/`**, under operator control. It is not designed here and no participant
  management system is built.
- **Device serials are never written.** Each device gets a study-local alias
  (`muse-01`) recorded in `run.json`. A keyed HMAC of a serial is the fallback if
  cross-session device identity is ever genuinely needed, with the key outside
  the package; the alias is preferred because it needs no key management and is
  not linkable at all. Unkeyed hashing of a low-entropy serial is **rejected** —
  it is reversible by enumeration.
- N-of-1 today, but `participant_pseudonym` is an ordinary field, so additional
  pseudonymous participants cost nothing. Nothing in the schema assumes one.
- Consent, retention and withdrawal remain **OPEN — HUMAN DECISION REQUIRED**
  (`docs/SAFETY.md`). The tension between withdrawal-on-request and raw
  immutability is unresolved and is not resolved here.

---

## 19. Recovery scenarios

**PROPOSED.** Every row was simulated by an independent reviewer in Pass 2.

| # | Scenario | On disk | Trustworthy | Never infer | Recovery |
|---|---|---|---|---|---|
| 1 | Process killed mid-write | `.part` files, no commit record for the active chunk | committed chunks; fsynced lifecycle prefix | that the active write is valid | report orphans; close `RECOVERED_UNCLEAN` / `UNCLASSIFIED` |
| 2 | Power loss | as (1), possibly plus a lost directory entry | hashed, committed files only | the intended terminal state | rescan; unclean close |
| 3 | Disk full | `.part`, no commit; `TECHNICAL_ERROR` event | prior committed chunks | that no data was lost after the last commit | ENOSPC path aborts the chunk and closes `CLEAN` / **`TECHNICAL_FAILURE`** — the cause is known, so it is recorded, not left `UNCLASSIFIED` |
| 4 | Active chunk partly written | orphan `.part` + no commit record | prior committed chunks | partial bytes as samples | report orphan; never adopt silently |
| 5 | Registry ok, package write failed | impossible by ordering (§12.1) | package | the registry | — |
| 6 | Package ok, registry update failed | valid package, stale/missing row | package | that the session does not exist | `registry rebuild` |
| 7 | Muse drops, Polar continues | Muse stream ends; `DEVICE_DISCONNECTED` event; `close_status != CLEAN` | everything recorded | Muse continuity | **completion predicate condition 5 rejects it** |
| 8 | Polar reconnects, different config | new `stream_id` + new descriptor; old closes `RECONFIGURED` | both, separately | that the two are one continuous stream | never blend |
| 9 | Device counter wraps | raw counter values preserved as received | raw counters + width status | wrap width while it is `assumed` | derive only with a known width — **OPEN — HARDWARE VALIDATION REQUIRED** |
| 10 | Packets out of order | recorded as received; `packet_seq` = arrival order | arrival order and raw counters | that acquisition reordered anything | analysis detects |
| 11 | Duplicate packets | both rows retained, both payloads retained | the exact duplicate arrivals | a deduplicated truth | analysis decides |
| 12 | No packet counters at all | no `counter` observation rows | host arrival + payload bytes | loss or order from timestamps | loss detection = `not_assessable` |
| 13 | Device clock resets | discontinuity visible in `time` observations | the raw device-time sequence | a continuous device clock | analysis detects; no repair |
| 14 | Host clock changes | UTC jumps, monotonic continuous | `host_arrival_monotonic_ns` | UTC continuity | use monotonic + snapshots |
| 15 | NTP step mid-session | `CLOCK_SNAPSHOT` discontinuity | monotonic; snapshot pairs | UTC as a stable timeline | quality assessed later from snapshots |
| 16 | App restarted after crash | package with no manifest pair | sealed committed files | the intended outcome | unclean close; `UNCLASSIFIED` |
| 17 | Explicit user abort | `CLOSED` / `CLEAN` / `ABORTED` in the sealed prefix | the abort record | that it completed | preserved; **cannot be upgraded** |
| 18 | Operator kills it, "doesn't feel right" | operator abort event + reason | the kill fact | a scientific reason | human classifies; downgrade-only |
| 19 | Manifest finalization crashes halfway | no valid manifest pair (tmp+rename is atomic) | pre-manifest bytes | that it closed completed | re-run finalization, or recover unclean |
| 20 | Checksum generation interrupted | no manifest pair | existing files | a complete inventory | recompute from immutable files |
| 21 | Derived inputs move | artifact paths break; hashes still identify bytes | recorded input hashes | that the same path means the same bytes | resolve by hash; fail closed |
| 22 | v2 reader opens v1 | `schema_version: "1.x"` | embedded schemas | that upgrade-in-place is safe | dispatch v1 or fail closed |
| 23 | Registry deleted | packages untouched | packages | that anything was lost | `registry rebuild` |
| 24 | Directory copied to another machine | self-contained package | the manifest-verified package | registry-only facts | scan the copy |
| 25 | Restored without the database | package present, no DB | package | sessions that lived elsewhere | rebuild the restored subset |

**Can the registry be fully reconstructed from packages?** **Yes** — by
construction, because the package is created before the registry row (§12.1).
The first draft could not make this claim; allocations that failed between the
registry insert and the directory creation would have existed only in SQLite.
Reordering the two removed that loss mode entirely.

**Can a package stand alone without the registry?** **Yes**, for all technical
and scientific interpretation. **No**, for identifying the human participant —
which is the intended design, not a gap (`docs/SAFETY.md`).

---

## 20. Decisions proposed for Q1–Q8

| Q | Question (`docs/SESSION_FORMAT.md`) | Proposal | Label |
|---|---|---|---|
| Q1 | Container format | Directory per session | **PROPOSED** |
| Q2 | Serialisation | Arrow IPC stream (raw chunks), length-prefixed binary (payloads), JSONL (events, lifecycle, chunk index), JSON (allocation/run/descriptor/manifest), SQLite (registry), Parquet (derived only) | **PROPOSED** |
| Q3 | Session identifier | UUIDv4, opaque, no embedded time; uniqueness via `os.mkdir` | **PROPOSED** |
| Q4 | Metadata set | Split by when known: `allocation.json` / `run.json` / `descriptor.json`; one authority per question (§7.3) | **PROPOSED** |
| Q5 | Event and marker representation | One `events/events.jsonl`; devices in `raw/`, meaning in events, joined by `raw_ref`; per-schema versioning; schemas snapshotted into the package | **PROPOSED** |
| Q6 | Partial and failed sessions | Same structural shape plus `closure_condition` + `recording_outcome`; absence of a manifest pair is the signal; recovery never guesses | **PROPOSED** |
| Q7 | Participant linkage | Generated `^P[0-9]{3,6}$` pseudonym; mapping outside `data/`; device aliases, never serials | **PROPOSED** |
| Q8 | Storage and retention | Canonical unit = the `sessions/<id>/` directory; backup moves packages (registry is rebuildable); derived is regenerable. **Retention period, offsite location and backup cadence remain undecided.** | **PROPOSED** / **OPEN — HUMAN DECISION REQUIRED** |

---

## 21. Alternatives rejected

| Rejected | Why |
|---|---|
| Single container per session (HDF5 / zip / SQLite blob) | Puts a central directory in the crash path; one bad write can lose a whole session |
| Parquet for raw chunks | Valid only once the footer lands; a truncated chunk is a total loss. Kept for **derived**, which is re-derivable |
| One ever-growing raw file per stream | Crash-tail handling becomes format-specific surgery |
| Denormalising packet metadata onto every sample row | ~44–59 MB per stream-hour at 256 Hz, **and** it falsely implies per-sample host timing precision |
| Long format for dense synchronous streams | 3–5× row inflation with no analytical gain; kept for sparse/mixed-rate streams via `layout: sparse_long` |
| One typed column per channel | Column names cannot carry channel order, units, physical meaning, or a mid-session rename |
| UUIDv7 / ULID / `timestamp+random` IDs | All embed a chronology that outlives any promise not to parse it; a wrong allocation-time clock would contaminate identity forever |
| Monotonic integer session IDs | Requires a durable allocator before any package exists |
| DuckDB as the operational registry | Analytical engine, not an operational state store |
| JSONL as the registry | No atomic multi-row transitions; awkward locking |
| A single `timestamp` column | Destroys timing provenance — the exact failure `docs/TIMING.md` exists to prevent |
| Fixed `device_timestamp` / `device_packet_counter` columns | Silently discards a second device-provided quantity if one exists. **This was in the first draft and was the sharpest finding of the review** |
| Gap / duplicate / out-of-order flags computed at acquisition | A buggy acquisition build would freeze a wrong observation into immutable data |
| Optional payload capture, off after "verification" | Assumes a decoder will never regress across firmware or library versions |
| `INDETERMINATE` as a fourth terminal outcome | Blurs operational closure with scientific meaning; replaced by `closure_condition` + `UNCLASSIFIED` |
| Manifest as outcome authority | Creates a permanent second truth alongside the lifecycle log |
| Derived artifacts inside the sealed package | A once-written manifest would go stale on the first analysis run |
| Replay by impersonating a device adapter | Makes a replay indistinguishable from a recording downstream |
| MNE-native session layout | Couples the on-disk contract to one library's evolution |
| Automatic quality scoring, plugin architecture, live alignment service, generic ontology, participant management system, global object store | Out of scope; several would also violate the no-interpretation-in-acquisition boundary |

---

## 22. Open questions that genuinely require human decision

### OPEN — HUMAN DECISION REQUIRED

1. **Retention, backup cadence and offsite location** for session packages
   (`docs/SESSION_FORMAT.md` Q8, `docs/OPERATIONS.md`).
2. **Withdrawal versus raw immutability.** If a participant withdraws, what
   happens to already-recorded raw data? Deletion-on-request and append-only
   immutability are in direct conflict and this design does not resolve it
   (`docs/SAFETY.md`).
3. **The Phase 0 protocol itself.** `protocol.version` is written as
   `"UNSPECIFIED"` because none exists. Every field that depends on protocol
   structure — blocks, markers, conditions — is unusable until it is supplied.
4. **Which streams are `required`.** The completion predicate depends on this
   set. A researcher must decide whether, say, a session that lost IMU but kept
   EEG counts as complete.
5. **Whether `UNCLASSIFIED` sessions block analysis** or are simply excluded
   with a recorded reason.
6. **Whether payload capture may ever be disabled** for a verified device, and
   under what evidence.

### OPEN — HARDWARE VALIDATION REQUIRED

7. **Which timing quantities each device actually exposes** — one device time or
   several, per packet or per sample, and what each refers to.
8. **Counter widths and wrap behaviour** for Muse and Polar.
9. **What `applies_to` is true** for each device time (`sample_acquisition` vs
   `packet_assembly` vs `transmission`). Every observation currently records
   `unknown`.
10. **Whether BrainFlow's timestamp is device-provided or host-synthesized.**
    This determines whether it is `device_provided` or `library_provided`, and
    the schema records both possibilities rather than assuming.
11. **Actual sustained sample rates** under BLE with two peripherals connected.
12. **QT Py serial round-trip latency and jitter**, which bounds how precisely a
    marker can be placed on a common timeline.

None of these has been converted into an assumed schema fact. The schema
preserves the raw information needed to answer all of them later.

### DEFERRED SAFELY

13. Compression codec for Arrow chunks (LZ4 vs ZSTD vs none) — a per-chunk
    property, changeable without a schema change.
14. Registry indexes and query surface — derived, rebuildable at will.
15. Any CLI ergonomics.

---

## 23. Proposed CL-002B implementation scope

Implementation only of what this document specifies, once approved.

**In scope**

1. Pydantic models for `allocation.json`, `run.json`, `descriptor.json`,
   `manifest.json`, the lifecycle/annotation record types, and the event
   envelope. Arrow schemas for `packets`, `samples`, `observations`.
2. JSON Schema files for the event payloads actually used, plus the snapshot
   mechanism that copies them into `schemas/`.
3. `SessionAllocator` — the §12.1 ordering, including the `os.mkdir` uniqueness
   gate.
4. `ChunkWriter` — the §12.2 atomic pattern, the hash chain, and the ENOSPC path.
5. `LifecycleLog` and `AnnotationLog`, including the downgrade-only rule.
6. `Finalizer` — the §14 sequence and the completion predicate as an executable
   function.
7. `RecoveryScanner` — reports orphans; adopts nothing.
8. `Registry` — SQLite schema, `rebuild` by scanning packages, and nothing that
   treats the registry as truth.
9. `SyntheticStreamSource` — deterministic, seeded, so the whole pipeline is
   testable with no hardware.
10. A read path sufficient to verify a package end to end.

**Explicitly out of scope for CL-002B**

Device adapters of any kind; BLE or serial transport; any analysis; any
reconstructed timing; any derived-artifact pipeline; any CLI beyond what the
tests need; any migration code.

---

## 24. Proposed acceptance tests for CL-002B

Each maps to a specific claim above. Property-based tests use `hypothesis`.

**Crash and atomicity**

1. Kill the writer at N random points during a synthetic session; for every N,
   the package either verifies fully or is detectably incomplete — never
   silently wrong.
2. Truncate a committed chunk file by one byte → verification fails.
3. Truncate `chunks.jsonl` mid-record → the hash chain reports the break.
4. Leave an orphan `.part` → recovery reports it and does not adopt it.
5. Simulate `ENOSPC` mid-chunk → no commit record is written, the session closes
   unclean, and no truncated chunk is ever committed.
6. Kill between `packets` seal and `samples` seal → the chunk is uncommitted and
   the packet metadata is not visible as real.

**False-complete (each of these is a regression test for a constructed attack)**

7. Append `CLASSIFIED: COMPLETED` to `annotations.jsonl` on an aborted session →
   the completion predicate still returns false.
8. Disconnect a `required` stream mid-session, then finalize → predicate false.
9. Edit one byte inside the sealed `lifecycle.jsonl` prefix → predicate false.
10. Write `manifest.json` but not `manifest.sha256` → predicate false.
11. A cleanly aborted session → predicate false, and the record is preserved.
11a. A crashed session closed `UNCLASSIFIED`, then annotated `COMPLETED` → the
    annotation is **rejected as invalid** and the predicate stays false. (RC1)
11b. A cleanly aborted session has a fully valid `manifest.json` +
    `manifest.sha256` pair → the pair verifies, and the predicate is still
    false. Finalization is not completion. (RC2)

**Timing provenance**

12. A synthetic device exposing **two** device times produces two `observations`
    rows; both survive a write/read round trip with `name`, `unit`, `clock_id`,
    `applies_to` and `provenance` intact.
13. A device exposing **no** counters produces zero counter rows, and no null,
    zero or host-derived value is substituted anywhere.
14. Property test: for any generated packet stream, no `raw/` file ever contains
    a reconstructed time.
15. Round-trip property: replay of a package reproduces `packets`, `samples` and
    `observations` byte-identically, nulls included.

**Authority and recovery**

16. Delete `registry.sqlite`, rebuild by scanning → the rebuilt registry equals
    the original for every session that has a package.
17. Copy one session directory to an empty root → it verifies standalone.
18. Registry and package disagree → the package wins and the disagreement is
    reported.

**Immutability and schema**

19. Any attempt to reopen a committed raw file for write raises.
19a. Canonical serialization is byte-stable: re-serializing any hashed record
    reproduces the exact bytes, and `record_sha256` verifies after removing the
    key and re-serializing. Key order, whitespace and non-ASCII escaping are
    each perturbed and each must fail verification. (RC6)
19b. Payload framing round-trips: `magic`, `packet_seq`, `payload_len`, payload
    bytes and `crc32c` survive a write/read cycle; a `payload_ref.offset` that
    does not land on `magic` fails closed instead of returning bytes. (RC7)
19c. A chunk with a valid `NNNNNN.commit.json` sidecar but no `chunks.jsonl`
    record is reported as orphaned and is **not** treated as committed. (RC5)
19d. An `ENOSPC` close records `TECHNICAL_FAILURE` with a reason, never
    `UNCLASSIFIED`. (RC9)
20. A v1 package read by a reader declaring only v2 support fails closed with an
    explicit unsupported-version error.
21. An unknown optional field in a v1.1 package is ignored by a v1.0 reader.
22. `participant_pseudonym: "Erandi"` is rejected at allocation.

---

## 25. Independent Review Trace

Reviewer: OpenAI Codex CLI 0.133.0, `codex exec`, read-only sandbox, reasoning
effort `high`. Codex modified no repository file. Claude retains responsibility
for the final design. Raw transcripts are deliberately not committed; this
section is the record.

| Pass | Reviewer mandate | Findings | Blocking | Serious | Accepted | Partially accepted | Rejected | Open (human) | Open (hardware) |
|---|---|---|---|---|---|---|---|---|---|
| 0 | Claude independent design, no reviewer input | — | — | — | — | — | — | — | — |
| 1a | Codex answers 20 architecture questions **without seeing the proposal** | 20 answers + 10 opinions | — | — | 13 converged | 5 diverged | 2 superseded by 1b | — | — |
| 1b | Codex data-contract / architecture red team | 20 (F1–F20) | 4 | 10 | 16 | 4 | 0 | — | — |
| 2 | Codex failure / recovery / timing review | 6 (G1–G6) + 25 scenarios + 3 false-complete constructions | 2 | 3 | 6 | 0 | 0 | — | 3 |
| 3 | Codex final go / no-go | 9 required changes (RC1–RC9) | 1 (RC1) | 8 | 9 | 0 | 0 | 0 | 0 |

### 25.1 Pass 1a — independent convergence

Codex was given the repository and the problem but **not** the proposal, to avoid
anchoring. It independently reached the same conclusion on: directory per
session; Arrow IPC immutable chunks (rejecting Parquet for raw on the same
truncation argument); SQLite for the registry; separate packet and sample tables;
package-as-truth with registry-as-cache; separate operational state and
scientific outcome; an atomic finalization marker as the only completion
evidence; typed null plus provenance for missing timing; counters rather than
timestamps for loss detection; namespaced device quirks; and no in-place
migration.

That convergence is the reason those points are not re-argued above.

### 25.2 Pass 1b — findings that changed the design

| # | Sev | Finding | Disposition | Resulting change |
|---|---|---|---|---|
| F1 | BLOCKING | QT Py press as raw-only loses intent, actor and protocol meaning | **ACCEPTED** | Press is raw **and** a semantic event that references it by `raw_ref` (§11) |
| F2 | BLOCKING | `FINALIZE_COMPLETE` appended to a log the manifest had already hashed | **ACCEPTED** | Nothing is appended to a hashed log after sealing; the manifest pair *is* the marker (§13, §14) |
| F3 | BLOCKING | Manifest and lifecycle both claimed the outcome | **ACCEPTED** | Manifest carries no outcome; it binds to the sealed lifecycle prefix by length + hash (§13) |
| F4 | BLOCKING | `INDETERMINATE` blurs operational closure with scientific meaning | **PARTIALLY ACCEPTED** | Kept "never guess"; adopted Codex's cleaner factoring into `closure_condition` + `UNCLASSIFIED` (§5) |
| F5 | SERIOUS | Timestamp-bearing ID creates a second chronology | **ACCEPTED** | UUIDv4. Note this also defeats Codex's own Pass-1a preference for UUIDv7, which embeds the same defect (§4) |
| F6 | SERIOUS | Wide per-channel columns freeze channel assumptions | **PARTIALLY ACCEPTED** | Adopted `fixed_size_list` keyed by an ordered descriptor + `descriptor_sha256` per chunk; **rejected** a blanket move to long format for dense streams (§9.1) |
| F7 | SERIOUS | Packets and samples not committed atomically | **ACCEPTED** | One commit record covering all four chunk files (§12.2) |
| F8 | SERIOUS | Payload capture optional after "verification" | **ACCEPTED** | Payloads are canonical raw, always on; disabling needs a named human decision (§9.1) |
| F9 | SERIOUS | `session.json` immutable-after-allocation cannot hold later facts | **ACCEPTED** | Split into `allocation.json` / `run.json` / `descriptor.json` by when facts become known (§7) |
| F10 | SERIOUS | Split authority; allocation-only registry rows | **ACCEPTED** | Package is created **before** the registry row; registry is now fully derived (§12.1) |
| F11 | SERIOUS | Derived inside the package makes a sealed manifest stale | **ACCEPTED** | `data/derived/` moved outside the package (§15) |
| F12 | SERIOUS | Named payload schemas are not interpretable without the repo | **ACCEPTED** | Every schema used is snapshotted into `schemas/` and inventoried (§11) |
| F13 | SERIOUS | Device metadata too thin | **ACCEPTED** | Every hardware claim is a `{value, status, source, observed_at}` quad (§8) |
| F14 | SERIOUS | "Opaque string" does not enforce pseudonymity | **ACCEPTED** | Generated, regex-validated pseudonym; device aliases instead of hashed serials (§18) |
| F15 | MODERATE | `chunks.jsonl` had no independent integrity | **ACCEPTED** | Hash chain + per-chunk sidecar (§12.2) |
| F16 | MODERATE | Logs would break or hollow the manifest | **ACCEPTED** | `logs/` explicitly out of manifest scope (§3) |
| F17 | MODERATE | Replay-as-device-adapter risks false provenance | **ACCEPTED** | Replay lives at the storage boundary; `origin` recorded (§16) |
| F18 | MODERATE | `host_utc_ns` without clock quality invites overconfidence | **ACCEPTED** | `clock_snapshot.v1` records source, sync status and `utc_quality` (§10.3) |
| F19 | MINOR | Global `event_vocab_version` will not scale | **ACCEPTED** | Per-schema versioning only (§11) |
| F20 | MINOR | Chunk size must be recorded as writer config | **ACCEPTED** | `run.json.writer_config`, explicitly not analysis epoching (§7.2) |

### 25.3 Pass 2 — three constructed false-complete states, and two information losses

Codex was asked to construct a false-complete session. **It succeeded three
times.** Each construction now has a closing rule and a regression test.

| # | Sev | Finding | Disposition | Resulting change |
|---|---|---|---|---|
| G1 | BLOCKING | A post-seal `CLASSIFIED: COMPLETED` could promote an aborted session without invalidating any hash | **ACCEPTED** | Annotations are **downgrade-only**; `COMPLETED` is establishable only inside the sealed prefix (§5, §14) |
| G2 | BLOCKING | A single `device_time_raw` column silently discards a second device-provided time, if one exists — and no device has been measured | **ACCEPTED** | Device times become rows in an `observations` table with `name`, `unit`, `clock_id`, `applies_to`, `provenance`, `status` (§9.3) |
| G3 | SERIOUS | Singular counter columns collapse per-channel or library-vs-device counters the same way | **ACCEPTED** | Counters are `observations` rows with `kind="counter"` (§9.3) |
| G4 | SERIOUS | A required stream could disconnect and the session still satisfy every completion condition | **ACCEPTED** | `run.json.required_streams` + per-stream `close_status`; condition 5 of the predicate (§14) |
| G5 | SERIOUS | `CLOCK_SNAPSHOT` cadence and sync metadata unspecified | **ACCEPTED** | Required schema with mandatory fields; cadence is recorded writer config, not a threshold (§10.3) |
| G6 | MODERATE | Derived artifacts resolving by relative path can bind to the wrong bytes | **ACCEPTED** | Resolve by `(session_id, sha256)`; path is a hint; fail closed on mismatch (§15) |

**Timing loss question — "is any timing information available at acquisition but
impossible to recover later?"** Answered **YES** in Pass 2 for the first draft
(G2, G3), which made it BLOCKING. After the `observations` table replaced the
fixed columns, all eleven quantities in `docs/TIMING.md` are either preserved raw
or derivable from preserved raw, with the device-dependent ones labelled
**OPEN — HARDWARE VALIDATION REQUIRED** rather than assumed.

**Challenged but survived:** directory per session; Arrow IPC for raw; JSONL for
small append-only logs; no acquisition-time dedup, reorder or repair; no
reconstructed time in `raw/`; and separate packet and sample tables — the last
one re-confirmed with storage arithmetic (~44–59 MB per stream-hour of pure
duplication at 256 Hz).

### 25.4 Pass 3 — final adversarial review

Codex was given only the repository contracts and the finished proposal, and
asked for one verdict: is there any remaining reason CL-002B should not begin?

**Verdict: `GO WITH REQUIRED CHANGES`** — nine required changes, RC1–RC9. **All
nine were accepted and applied before this document was completed.** None was
rejected or partially accepted.

RC1 is the important one: it is a **fourth false-complete path**, and one this
document had opened while closing the other three. §5 said a post-seal
annotation could move `UNCLASSIFIED` to "any of the three" — which includes
`COMPLETED`. Since every crashed session closes `UNCLASSIFIED`, appending one
line to `annotations.jsonl` would have promoted any crash to a completed
session, bypassing the sealed-prefix rule entirely. The downgrade-only rule was
written specifically to prevent that and was defeated by its own wording.

| RC | Category | Required change | Applied in |
|---|---|---|---|
| RC1 | **Blocking — false-complete** | `UNCLASSIFIED` may be annotated only to `ABORTED` or `TECHNICAL_FAILURE`; no annotation may ever produce `COMPLETED`. Predicate gains condition 8 | §5, §14 |
| RC2 | Source-of-truth | The manifest pair is a **finalization** marker, never on its own evidence of `COMPLETED` — a clean abort produces an identical pair | §13, §14 |
| RC3 | Timing provenance | Split the single `host_clock_id` into `host_arrival_monotonic_clock_id` and `host_arrival_utc_clock_id`; one field cannot name two clocks | §9.1, §10.1, §11 |
| RC4 | Information loss | `raw_value int64` coerced floating-point library timestamps and unsigned counters. Replaced with `value_type` + four typed columns, exactly one non-null | §9.3 |
| RC5 | Crash consistency | `chunks.jsonl` is the authoritative commit log; a sidecar alone is not a commit. Crash-after-sidecar-before-index = orphaned | §12.2 |
| RC6 | Crash consistency / ambiguity | Canonical JSON serialization defined exactly, and `record_sha256` defined as the hash of the record with that key absent | §12.2 |
| RC7 | Information loss / ambiguity | Exact binary framing for `payloads/*.bin`: magic, widths, byte order, CRC, and what `payload_ref.offset` counts | §9.1 |
| RC8 | Information loss | The channel layout is itself an unverified hardware claim; it now carries `channel_layout_provenance` | §8 |
| RC9 | Source-of-truth / crash | A **known** fatal writer error (ENOSPC, I/O fault) closes `TECHNICAL_FAILURE` with a reason; `UNCLASSIFIED` is reserved for genuinely unknown cause | §12.3, §19 |

Codex reported **no** unnecessary complexity.

Each of RC1, RC2, RC5, RC6, RC7 and RC9 also gained a regression test in §24
(11a, 11b, 19a–19d), because a rule with no test is a rule that decays.

**CL-002B is therefore unblocked**, subject to human approval of this proposal
and to the §22 decisions that only a person can make.

---

## Appendix A — Special review questions, answered explicitly

| # | Question | Answer | § |
|---|---|---|---|
| 1 | One Parquet file per whole stream, or immutable chunk files? | **Immutable chunk files, Arrow IPC stream — not Parquet.** A truncated IPC stream yields every complete batch; a truncated Parquet file has no footer and is a total loss | §12.2 |
| 2 | Events: Parquet, JSONL or other? | **JSONL.** Sporadic, tiny, heterogeneous payloads; fsync-per-event is affordable; a torn last line is trivially droppable | §11 |
| 3 | Should the operational registry use SQLite? | **Yes** — stdlib, so zero new dependencies; ACID; single file; WAL. And it is **fully derived**, so its crash behaviour barely matters | §6 |
| 4 | UUIDv7, ULID or another scheme? | **UUIDv4.** Every time-bearing scheme, v7 included, embeds a chronology that outlives any promise not to parse it | §4 |
| 5 | Packet metadata and sample rows in separate tables? | **Yes.** ~44–59 MB per stream-hour of pure duplication at 256 Hz, and denormalising falsely implies per-sample host timing precision | §9.2 |
| 6 | How to represent streams with device timestamps per packet rather than per sample? | An `observations` row with `sample_index_in_packet = null` means "applies to the whole packet"; a non-null value means "applies to this sample" | §9.3 |
| 7 | How can a reader detect dropped packets without trusting reconstructed timestamps? | Counter observations plus `n_samples` sums, with wrap applied only when the width is known; `packet_seq` independently preserves arrival order. No counter → `not_assessable` | §10.5 |
| 8 | Minimum information to reconstruct device-relative sample time later? | Per packet: `packet_seq`, `host_arrival_monotonic_ns`, `n_samples`, and ≥1 observation. Per stream: nominal rate + counter width. Plus clock snapshots | §10.4 |
| 9 | What does an interrupted session look like on disk? | No `manifest.json` / `manifest.sha256` pair; possibly orphan `.part` files and an uncommitted chunk; `lifecycle.jsonl` ends without a `CLOSED` record | §19 |
| 10 | What must be written before the first sensor connects? | The session directory, `allocation.json`, and an `ALLOCATED` lifecycle record — all fsynced. Acquisition may not start before that | §12.1 |
| 11 | What must be immutable after allocation? | `allocation.json` in full: session id, schema version, allocation time, protocol identity, participant pseudonym, host, commit, environment | §7.1 |
| 12 | What becomes immutable only at finalization? | The file inventory and its hashes, the per-stream close statuses, the sealed `lifecycle.jsonl` prefix, and `events/events.jsonl` | §13, §14 |
| 13 | What may remain mutable operationally? | Only three things: appends to `annotations.jsonl` (downgrade-only), `logs/`, and the entire derived registry | §5, §6 |
| 14 | How is the registry prevented from becoming a second conflicting source of truth? | It is fully derived, every row carries `scanned_at_utc_ns`, `rebuild` drops and rescans, and the package is created before the row so no registry-only state can exist | §6, §12.1 |
| 15 | Can a package be understood with the registry completely lost? | **Yes** for technical and scientific interpretation. **No** for identifying the human participant — by design | §19 |
| 16 | Can the registry be rebuilt by scanning packages? | **Yes, completely** — a property bought by the allocation ordering, not assumed | §12.1, §19 |
| 17 | How are missing optional timing fields represented without inventing values? | The observation row simply does not exist. Where a column is unavoidable, `null` plus an explicit provenance field. Never zero, never host time standing in for device time | §9.3 |
| 18 | How are device quirks kept out of the generic model? | `extensions.<device_kind>` in the descriptor, `name` namespacing in observations, and canonical payload bytes for anything not yet modelled | §8, §9.3 |
| 19 | Which parts of the schema would make future Athena/H10 timing analysis impossible if designed wrong today? | Collapsing host and device time into one column; a **single** device-time or counter column when a device exposes several; dropping the packet→sample grouping; storing only reconstructed time; treating QT Py markers as abstract events with no raw serial timing; discarding transport payloads | §9.3, §10, §11 |
| 20 | What is the smallest implementation that fully preserves future analytical options? | §23: models, allocator, chunk writer, lifecycle + annotations, finalizer, recovery scanner, derived registry, synthetic source, and a verifying read path. No adapters, no transport, no analysis | §23 |

## Appendix B — Things deliberately NOT designed

- Device adapters, BLE transport, serial transport — CL-002B is explicitly hardware-free.
- Any reconstructed timing, alignment or drift model. The schema preserves the inputs; the model is a later ticket and a scientific decision.
- Any signal quality metric, artefact criterion, band definition, epoch length or Focus definition. Forbidden by `AGENTS.md` §6.
- Any analysis pipeline, feature extraction, or ML of any kind.
- Neurofeedback or any live feedback path. Forbidden by `docs/SAFETY.md` S1.
- iOS, mobile, web dashboards, cloud storage, multi-laboratory or distributed operation.
- A participant management system, a consent system, or a retention system.
- Backup automation. §20 defines what must move together; the cadence is a human decision.
- Migration code. Nothing exists to migrate from, and the versioning rules already say what a migration must do when one is needed.
- Compression tuning, registry query surface, and CLI ergonomics — all safely deferrable.
