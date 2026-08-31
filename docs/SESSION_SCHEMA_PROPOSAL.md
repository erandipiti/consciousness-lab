# SESSION_SCHEMA_PROPOSAL — Session Package v1 and Session Registry

> ## Status: SUPERSEDED BY SESSION PACKAGE v2 — historical record
>
> **Superseded by** [`SESSION_SCHEMA_V2_PROPOSAL.md`](SESSION_SCHEMA_V2_PROPOSAL.md)
> per `DECISIONS.md` **D27** (CL-002A-R3-APPROVAL, 2026-08-31). Session Package
> v2 is the acquisition data contract; a v2 reader fails closed on a v1 package.
> No Study 001 recording was made under v1.
>
> **Nothing below is deleted.** This document remains accurate about v1 and is
> kept as the historical design record — including the five adversarial review
> rounds whose findings are the argument for v2. Its approval history stands as
> recorded.
>
> **Approved baseline (v1):** `c5e6a9e712cd4a214162bec57d99ea89c33b01e4`
> **Human approval recorded in:** CL-002A-APPROVAL, 2026-08-28.
> **Decision index:** [`DECISIONS.md`](DECISIONS.md) D8–D26.
>
> This document is the **authoritative specification** for Session Package v1.
> **A future implementation discrepancy is a bug, unless a later decision record
> in `DECISIONS.md` explicitly supersedes this specification.**
>
> The filename still says "proposal" and the historical review trace in §25 is
> deliberately preserved — it records how five adversarial passes reached this
> design, including four constructed false-complete states and two
> information-loss findings that reshaped it. That history is evidence, not
> clutter, and is not to be erased.

**Ticket:** CL-002A, corrected by CL-002A-R1 and CL-002A-R2, approved by
CL-002A-APPROVAL.

Every major design point carries one of these labels.

| Label | Meaning |
|---|---|
| **APPROVED** | Approved as written on 2026-08-28; binding on CL-002B. See `DECISIONS.md` D8–D26 |
| **ALREADY DECIDED** | Settled in `docs/DECISIONS.md` before CL-002A; restated, not re-decided |
| **OPEN — HUMAN DECISION REQUIRED** | Still open. A person must choose; no coding agent may |
| **OPEN — HARDWARE VALIDATION REQUIRED** | Still open. Depends on device behaviour never physically measured |
| **DEFERRED SAFELY** | Not decided, and deferring still costs nothing |

The design goal this document is written against:

> A session recorded two years from now should still be interpretable by someone
> who did not run it, without needing undocumented assumptions from the original
> machine or chat history.

---

## 1. Executive recommendation

**APPROVED.** A session is a **directory** on disk. It is the only authority.
Everything else — including the registry — is a derived index that can be
deleted and rebuilt.

Inside the package, raw data is written as **immutable, hash-chained chunks**.
Canonical raw is the lowest-level representation actually observed at our
acquisition boundary: where the device's transport bytes reach our code they are
preserved alongside the decoded view, and where an upstream library decoded them
first that is recorded as such rather than fabricated. Timing is never collapsed: host arrival lives in the
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
3. Canonical raw is the lowest level our acquisition boundary actually saw.
   Where transport bytes are exposed, preserving them is mandatory. Where an
   upstream library decoded them first, the stream is honestly labelled
   `library_decoded` and no bytes are fabricated.
4. `packets` and `samples` are separate tables. Denormalising packet metadata
   onto every sample row costs ~44–59 MB per stream-hour at 256 Hz **and**
   falsely implies per-sample host timing precision.
5. Device times and counters are **observations** (`name`, `unit`, `clock_id`,
   `applies_to`, `provenance`, `status`), not fixed columns. A device that turns
   out to expose two timestamps must not force us to silently pick one.
6. `lifecycle_state`, `closure_condition` and `recording_outcome` are three
   separate fields. A crash produces `RECOVERED_UNCLEAN` + `UNCLASSIFIED`; it
   never guesses between an operator abort and a power failure.
7. `COMPLETED` is creatable **only inside the sealed prefix**, and readers
   evaluate the **effective** outcome — the sealed outcome after applying every
   valid post-seal downgrade. A corrupt annotation log fails closed rather than
   falling back to the sealed value.
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

7. **One authority per question.** APPROVED. If two files can answer the same
   question, one of them is wrong eventually.
8. **Null means "not provided".** APPROVED. Never zero, never a default, never
   an interpolation, never the host clock standing in for a device clock.
9. **Acquisition is dumb on purpose.** APPROVED. No dedup, no reordering, no
   repair, no gap flags. A flag computed by a buggy acquisition build would
   freeze a wrong observation into immutable data.
10. **Recovery reports; it does not repair.** APPROVED. Orphan files are
    surfaced, never silently adopted.

---

## 3. Session package directory structure

**APPROVED.** Q1 answer: directory per session.

```text
data/
  sessions/<session_id>/                  # THE sealed acquisition package
    allocation.json                       # immutable after allocation
    run.json                              # sealed at RECORDING_START
    lifecycle.jsonl                       # append-only; SEALED at finalization
    annotations.jsonl                     # post-seal, hash-chained, downgrade-only
    annotations.head.json                 # expected length/count/head hash of the above;
                                          #   mutable after sealing (see 14.1)
    events/
      events.jsonl                        # sealed before the manifest
    schemas/
      <schema_id>.json                    # snapshot of every schema used
    raw/<stream_id>/
      descriptor.json                     # sealed at stream open
      payloads/000000.bin                 # transport bytes; ONLY when
                                          #   raw_capture_level = transport_payload
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

**APPROVED.** Q3 answer: **UUIDv4**, canonical lowercase, as the directory name.

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

**APPROVED.** Three orthogonal fields. Collapsing them is how false-complete
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

> **A post-seal annotation may only DOWNGRADE an outcome.** The complete set of
> permitted transitions is exactly four:
>
> | From | To | Permitted |
> |---|---|---|
> | `COMPLETED` | `ABORTED` | yes |
> | `COMPLETED` | `TECHNICAL_FAILURE` | yes |
> | `UNCLASSIFIED` | `ABORTED` | yes |
> | `UNCLASSIFIED` | `TECHNICAL_FAILURE` | yes |
> | *anything* | `COMPLETED` | **never** |
>
> `COMPLETED` may be **created** only inside the sealed lifecycle prefix, during
> a clean finalization. No annotation may create it and no annotation may
> restore it: once an outcome has been downgraded away from `COMPLETED`, the
> session can never be `COMPLETED` again.

Two things make this rule load-bearing rather than decorative.

First, without the `never` row, appending one line to a text file would turn an
aborted session into a completed one without invalidating any hash. Codex
constructed exactly that attack in Pass 2 (finding G1).

Second, the `UNCLASSIFIED` restriction matters as much as the `COMPLETED` one.
Every crashed session closes `UNCLASSIFIED`; if an annotation could raise that
to `COMPLETED`, the sealed-prefix rule would be bypassed by every crash. An
earlier draft of this document said an annotation could move `UNCLASSIFIED` to
"any of the three", which reopened precisely that hole.

**Lateral reclassification — `ABORTED` <-> `TECHNICAL_FAILURE` — is not
permitted by this rule set.** A human who classifies a session as an operator
abort and later determines it was a device fault has no path to correct the
record. That is a deliberate consequence of keeping the transition set minimal.
— **DECIDED, CL-002A-APPROVAL (`DECISIONS.md` D19):** forbidden in v1.
Minimising mutable scientific and operational state is preferred for Study 001,
and the flexibility is not required to begin. Revisit in a future schema
revision if experience shows it is genuinely necessary.

### 5.1 Sealed outcome versus effective outcome

These are two different quantities and conflating them is how a session reads
`COMPLETED` after a human has already downgraded it.

- **Sealed recording outcome** — the `recording_outcome` written during
  finalization, inside the sealed prefix of `lifecycle.jsonl`. It is immutable
  and hash-protected by `manifest.lifecycle_seal`.
- **Effective recording outcome** — the sealed outcome after applying, in file
  order, every **valid** post-seal downgrade annotation from
  `annotations.jsonl`.

**Every reader, tool, report and query must use the effective outcome.** The
sealed outcome is an input to that computation and is never the answer on its
own.

Computing the effective outcome is deterministic:

**`annotations.head.json` — the anti-deletion pointer.** A hash chain proves
that the records present are intact; it cannot prove that no record was
*removed*. Deleting `annotations.jsonl`, or truncating it on a record boundary,
would leave a perfectly valid shorter chain — and under a naive
`absent -> sealed` rule that silently restores a sealed `COMPLETED`, undoing a
human's downgrade.

So finalization writes `annotations.head.json` as part of sealing:

```json
{ "bytes": "0", "record_count": "0", "head_record_sha256": null }
```

and every annotation append updates it atomically (tmp -> fsync -> rename ->
fsync(dir)) **after** the append lands. It is **one of the three objects §14.1
permits to change after sealing**, and the only one replaced atomically as a
whole file rather than appended to. It exists solely so that the *expected*
length and head of the chain are recorded outside the chain itself.

**Finalization always writes it**, initialised to zero records, whether or not
an annotation is ever added — see step 6 of §14. A finalized package that lacks
it has been tampered with or damaged.

```text
0. if annotations.head.json is absent:
       package annotation status = INDETERMINATE; is_completed := false
       (finalization always writes it, so its absence means tampering or loss)
1. if head.record_count == 0:
       annotations.jsonl must be absent or zero-length -> effective := sealed
       anything else -> INDETERMINATE; is_completed := false
2. if head.record_count > 0:
       annotations.jsonl must exist, be exactly head.bytes long, and its last
       record's record_sha256 must equal head.head_record_sha256
       any mismatch -> INDETERMINATE; is_completed := false
       otherwise:
         verify the hash chain end to end
         2a. chain fails  -> INDETERMINATE; effective is UNDEFINED
                             is_completed := false
                             (NEVER fall back to the sealed outcome)
         2b. chain passes -> apply records in file order, maintaining a running
                             effective outcome that starts at `sealed`:
               - a record whose `from` does not equal the CURRENT running
                 effective outcome is REJECTED
               - a record whose target is COMPLETED is REJECTED
               - a record whose (from -> to) is not one of the four permitted
                 transitions is REJECTED
               - a rejected record is never applied, and is reported
             if any record was rejected -> is_completed := false
             effective := the running outcome after every accepted record
```

Every annotation therefore carries **both** `from` and `to`, and `from` must
match the outcome in force at that point in the file. Without that check two
conforming implementations could legitimately disagree about a log containing
two downgrades — one applying both, one applying the first and treating the
second's premise as stale — and "conforming implementations disagree" is exactly
what a canonical format exists to prevent.

Step 2a is the fail-closed rule: a corrupt or truncated annotation log must
never silently restore a sealed `COMPLETED`, because "the downgrade record is
unreadable" and "there was no downgrade" are indistinguishable from the bytes
and only one of them is safe to assume.

**Residual limitation, stated plainly.** The head pointer defeats accidental
loss, truncation, and tampering with either file alone. It does **not** defeat
an actor with write access who removes or rewrites *both* files consistently.
Defending against that needs append-only media or a signature, neither of which
this single-workstation design has. — **OPEN — HUMAN DECISION REQUIRED**, and
recorded here rather than left as an implied guarantee.

The `is_completed := false` on any rejected record is deliberately
conservative. A package containing a rejected upgrade attempt is not one a
reader should silently bless, even if the surviving records happen to leave the
outcome at `COMPLETED`.

---

## 6. Registry design

**APPROVED.** SQLite (`sqlite3`, Python standard library — **no new
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

**APPROVED.** Q4 answer. Facts are split by **when they become known**, which is
what makes "immutable after allocation" achievable without placeholders.

### 7.1 `allocation.json` — immutable after allocation

```json
{
  "schema_name": "session_package",
  "schema_version": "1.0",
  "session_id": "9f2c1e40-6b3a-4d51-8e77-0a1b2c3d4e5f",
  "allocated_at": {
    "utc_ns": "1787923530123456789",
    "monotonic_ns": "884413221000",
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
  "sealed_at": { "utc_ns": "…", "monotonic_ns": "…" },
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
    "note": "Writer configuration only. NOT analysis epoching and NOT a scientific parameter. These stay JSON Numbers: declared domain is bounded int32 (see 12.2.1)."
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

**APPROVED.** Sealed at stream open, hashed, and referenced by every chunk.

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
  "acquisition": {
    "backend": { "name": "brainflow", "version": "5.x.y", "adapter_version": "0.1.0" },
    "raw_capture_level": "library_decoded",
    "transport_payload_preserved": false,
    "decode_boundary": "BrainFlow decoded the BLE notification payloads before our recorder received anything.",
    "provenance": { "status": "assumed", "source": "not yet measured on hardware", "observed_at": null }
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
`close_status = RECONFIGURED`. Streams are never blended. — APPROVED

---

## 9. Raw sample/packet model

**APPROVED.** Q2 and the packet-vs-sample question.

### 9.1 Three artifacts per chunk, one commit

### 9.0 What "canonical raw" means

**APPROVED.** Canonical raw is:

> the **lowest-level representation actually observed at our acquisition
> boundary**, preserved without scientific transformation by our code.

That definition is deliberately relative to our boundary, because where the
boundary sits is not the same for every device or every backend, and pretending
otherwise would make the schema lie.

Every raw stream therefore declares a **`raw_capture_level`**:

| Level | Meaning | Canonical raw is |
|---|---|---|
| `transport_payload` | We received the device/transport bytes before any scientific decoding | The transport bytes — and in v1 they **must** be preserved (§9.1) |
| `library_decoded` | An external acquisition library decoded or transformed the transport payload before our recorder saw anything | The library's decoded representation, **with reduced provenance** |
| `synthetic` | We generated the stream; there is no device and no transport | The generated values, plus the generator seed |

```text
transport_payload:   BLE notification bytes -> [our boundary] -> payload log -> our decoder -> packets/samples
library_decoded:     device -> BrainFlow decoder -> numeric board data -> [our boundary] -> packets/samples
```

**`library_decoded` is not equivalent to transport raw and this document never
implies that it is.** In that mode a decoding step happened upstream, outside
our control and outside our version pinning, and the bytes that entered it are
gone. That is a real loss of provenance; it is recorded as one rather than
papered over.

This matters concretely today: Muse S Athena may initially be reached through
BrainFlow, which may expose only decoded board data. Forcing the schema to claim
transport bytes were captured in that case would be a fabrication, and
fabricating them would be worse.

### 9.0.1 Acquisition provenance, recorded per stream

Every `raw/<stream_id>/descriptor.json` carries:

```json
"acquisition": {
  "backend": { "name": "brainflow", "version": "5.x.y", "adapter_version": "0.1.0" },
  "raw_capture_level": "library_decoded",
  "transport_payload_preserved": false,
  "decode_boundary": "BrainFlow decoded the BLE notification payloads before our recorder received anything; the notification bytes were never visible to us.",
  "provenance": { "status": "assumed", "source": "not yet measured on hardware", "observed_at": null }
}
```

This answers, for any session read years later: which backend produced the
stream, at what version, what capture level was available, whether exact
transport bytes were preserved, and — if not — exactly where the transformation
boundary sat.

The same four fields describe direct BLE (`transport_payload`, `true`), a Polar
library adapter (either, depending on the library), QT Py serial
(`transport_payload`, `true`), and synthetic sources (`synthetic`, `false`, with
a seed). **The generic session format does not change when the backend
changes** — only these values do.

### 9.1 Three or four artifacts per chunk, one commit

**`payloads/NNNNNN.bin` — the transport payload log**, present **only when
`raw_capture_level = "transport_payload"`**. Length-prefixed records of the exact
bytes received from the device, each tagged with its `packet_seq`.

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

**The capture rule for Session Package v1, stated as an invariant:**

> **If transport payload bytes cross our acquisition boundary, they MUST be
> preserved.** There is no human-disable exception in v1.
>
> **If the upstream library never exposes them, we record
> `transport_payload_preserved: false` and `raw_capture_level:
> "library_decoded"`, and we never fabricate or reconstruct fake transport
> bytes.**

#### The v1 invariant table

| `raw_capture_level` | `transport_payload_preserved` | `payloads/` artifact | `packets.payload_ref` |
|---|---|---|---|
| `transport_payload` | **must be `true`** | **must exist** for every committed chunk | **non-null** for every packet decoded from transport bytes |
| `library_decoded` | **must be `false`** | must **not** exist | **must be null** |
| `synthetic` | **must be `false`** | must **not** exist | **must be null** |

Formally, for schema version 1:

```text
raw_capture_level == "transport_payload"  IFF  transport_payload_preserved == true
raw_capture_level == "library_decoded"     =>  transport_payload_preserved == false
raw_capture_level == "synthetic"           =>  transport_payload_preserved == false
```

**`transport_payload` with `transport_payload_preserved: false` is not a valid
state and must fail model validation.** An earlier draft permitted it via a
"unless a human disables it" escape, which left two reasonable CL-002B
implementers free to disagree about whether a payload artifact must exist for
that stream — one treating `raw_capture_level` as the contract, the other
treating `transport_payload_preserved`. The descriptor must determine the chunk
shape unambiguously, so the two fields are locked together and the exception is
removed.

`transport_payload_preserved` is consequently **redundant with**
`raw_capture_level` in v1. It is kept anyway, as an explicit, greppable
assertion that a reader can check without knowing the enum's semantics — and
because a future schema version that reintroduces the distinction would need the
field to exist. Any disagreement between the two is a validation failure, not a
degree of freedom.

If the invariant later proves operationally unacceptable, relaxing it requires
an explicit future schema decision and a **major** version bump. That
hypothetical is not solved today.

An earlier draft went the other way and said payload capture was
*unconditionally* mandatory at all capture levels. That was not implementable:
it would force a BrainFlow-backed adapter either to fail, or to synthesize bytes
it never saw — and a synthesized payload log is strictly worse than an honest
absence, because it looks like ground truth. The invariant above keeps the
mandate exactly where it is implementable: at the boundary the bytes actually
cross.

Where payloads *are* available, preserving them remains the most conservative
decision in the design, and the repository's own state justifies it: no decoder
is written, and `docs/HARDWARE.md` marks every device unverified. If our Athena
parser mis-decodes and we stored only decoded samples, the error is permanent.
With payloads it is fully recoverable by re-decoding. For BLE these packed bytes
are typically *smaller* than the decoded `float32`/`float64` samples, so the
cost is low.

**`packets/NNNNNN.arrow` — one row per received packet.**

| Column | Type | Null? | Meaning |
|---|---|---|---|
| `packet_seq` | int64 | no | **Ours.** Strictly increasing at arrival. Never from the device. |
| `host_arrival_monotonic_ns` | int64 | no | `time.monotonic_ns()` — the session spine |
| `host_arrival_utc_ns` | int64 | no | `time.time_ns()` — may step |
| `host_arrival_monotonic_clock_id` | string | no | which OS clock produced the monotonic value |
| `host_arrival_utc_clock_id` | string | no | which OS clock produced the UTC value |
| `n_samples` | int32 | no | samples carried in this packet |
| `payload_ref` | struct{file,offset,length} | **yes** | exact bytes this row was decoded from. **Non-null iff `raw_capture_level = "transport_payload"`** (equivalently, iff `transport_payload_preserved = true` — §9.1 locks the two together); null otherwise. A fabricated pointer is never written. `offset` and `length` are `uint64` in Arrow, and `uint64_decimal` wherever this struct appears in JSON |
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

**APPROVED, and this is the single most important correction in this document.**

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

**APPROVED.** No threshold is proposed. `docs/TIMING.md` freezes none, and this
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
  "payload": { "monotonic_ns": "…", "utc_ns": "…",
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

**APPROVED.** Q5 answer.

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
{ "event_seq": "42",
  "event_name": "BLOCK_START",
  "payload_schema": "block_start.v1",
  "origin": "protocol",
  "host_arrival_monotonic_ns": "884413221000",
  "host_arrival_utc_ns": "1787923530123456789",
  "host_arrival_monotonic_clock_id": "CLOCK_MONOTONIC",
  "host_arrival_utc_clock_id": "CLOCK_REALTIME",
  "raw_ref": { "stream_id": "qtpy.marker", "packet_seq": "9912", "sample_index_in_packet": 3 },
  "payload": { "block_index": 2 } }

`packet_seq` is `int64_decimal`; `sample_index_in_packet` and `block_index` are
bounded Numbers (§12.2.1).
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

**APPROVED.** One workstation. No distributed durability.

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
# ONLY when raw_capture_level == "transport_payload":
payloads/NNNNNN.bin.part      -> flush -> fsync(fd) -> close -> rename -> fsync(dir)

# always:
packets/NNNNNN.arrow.part     -> flush -> fsync(fd) -> close -> rename -> fsync(dir)
observations/NNNNNN.arrow.part-> flush -> fsync(fd) -> close -> rename -> fsync(dir)
samples/NNNNNN.arrow.part     -> flush -> fsync(fd) -> close -> rename -> fsync(dir)
NNNNNN.commit.json.tmp        -> fsync -> rename -> fsync(dir)      # sidecar
append the same record to chunks.jsonl -> fsync                     # hash chain
```

The payload step is **conditional on the capture level and on nothing else**. At
`library_decoded` and `synthetic` no `payloads/` file is created at all — not an
empty one, not a placeholder — and the commit record omits the `payloads` entry
(§9.1). A writer that emits a payload artifact at those levels has violated the
§9.1 invariant; test P4 exists to catch it.

```json
{ "chunk_id": "123", "prev_record_sha256": "…",
  "payloads":     { "path": "payloads/000123.bin",      "sha256": "…", "bytes": "918273" },
  "packets":      { "path": "packets/000123.arrow",     "sha256": "…", "bytes": "40112" },
  "observations": { "path": "observations/000123.arrow","sha256": "…", "bytes": "8104" },
  "samples":      { "path": "samples/000123.arrow",     "sha256": "…", "bytes": "1508992" },
  "first_packet_seq": "30000", "last_packet_seq": "30749",
  "descriptor_sha256": "…", "record_sha256": "…" }
```

**`chunks.jsonl` is the authoritative commit log. A chunk is real if and only if
its record appears there, and its record must name every file the stream
actually produces.** The descriptor's `raw_capture_level` determines the
required shape, and **only one shape is valid for each level**:

| `raw_capture_level` | Required commit-record entries | `payloads` entry |
|---|---|---|
| `transport_payload` | `payloads`, `packets`, `observations`, `samples` | **required** |
| `library_decoded` | `packets`, `observations`, `samples` | **must be absent** |
| `synthetic` | `packets`, `observations`, `samples` | **must be absent** |

The `payloads` entry is **omitted entirely** at the latter two levels — never
written as `null`, never as an empty path. Because
`descriptor.acquisition.raw_capture_level` fixes the expected shape before a
reader looks at the chunk, an absent `payloads` entry can never be mistaken for
a lost file, and a *present* one at `library_decoded` is a validation failure
rather than a curiosity. The `NNNNNN.commit.json` sidecar is a convenience
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

**Canonical serialization — RFC 8785 (JSON Canonicalization Scheme, JCS).**

Two implementations must produce identical bytes for the same logical record, or
every hash in this design is meaningless. Rather than invent a project-local
format, this design **adopts RFC 8785** for any JSON object whose canonical
bytes are hashed. Conformance to the RFC is the requirement; a prose
approximation of it is not.

An earlier draft specified
`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` and
asserted that independent implementations would necessarily agree. **That claim
was too strong, and it is withdrawn.** Python's ordinary `json.dumps()` is not a
conforming JCS implementation, on at least two counts that matter:

- **Key ordering.** JCS sorts object keys by their **UTF-16 code units**;
  `sort_keys=True` sorts by Unicode code point. The two orders differ once any
  key contains a character above the BMP.
- **Number formatting.** JCS mandates the ECMAScript `Number::toString`
  algorithm. Python's float repr is shortest-round-trip but is not
  byte-identical to it in every case, and Python has no notion of JCS's integer
  range rules.

CL-002B implements or vendors a small, tested JCS helper. **No new dependency is
mandated**; whether to take one is an implementation-review call, not a schema
decision.

**Numeric rules** — constraints on what may be written, not on canonicalization:

- `NaN`, `Infinity` and `-Infinity` are **invalid** in any JSON record and must
  be rejected at write time. Python's `json` emits them as bare
  `NaN`/`Infinity`, which is not valid JSON at all, so writers must set
  `allow_nan=False` and treat the resulting error as fatal.
- JCS serializes `-0` as `0`. Negative zero therefore **cannot carry meaning**
  and must not be used to.
- Floating point appears in JSON **nowhere in this design**. Every timing
  quantity is an integer; the only floats in the whole package are Arrow
  columns (`observations.value_f64`, sample values), which are never JSON.

### 12.2.1 Integer representation in JSON — `int64_decimal` and `uint64_decimal`

**APPROVED, and it is a correctness requirement, not a style preference.**

RFC 8785 constrains JSON Numbers to values exactly representable as IEEE-754
doubles. Integers are therefore interoperably exact only within

```text
-9007199254740991  ..  9007199254740991      (±2^53 − 1)
```

Our UTC nanosecond timestamps sit around `1.8e18` — roughly **200 times** beyond
that range. Written as a JSON Number, `1787923530123456789` is not
representable; a conforming implementation rounds it to `1787923530123456768`,
and the value is silently wrong by 21 nanoseconds. These numbers participate in
provenance and in hashes, so losing a single bit is unacceptable.

> **Rule.** Any field whose *declared semantic domain* is `int64` or `uint64`
> MUST be represented in JSON as a **canonical decimal string**, never as a JSON
> Number.

```json
{
  "utc_ns":        "1787923530123456789",
  "monotonic_ns":  "884413221000",
  "byte_offset":   "918273",
  "record_count":  "42"
}
```

**The rule is driven by the declared domain, not by the current value.** A
counter that happens to read `42` today is still written `"42"` if its domain is
`uint64`, so that a field's representation never changes merely because a value
later crosses `2^53`. A representation that shifts under load is a
representation that breaks readers under load.

**Scope: every JSON and JSONL document in the package**, not only the
hash-chained ones. The rounding happens in any conforming parser, hashed or not
— a browser-based or JavaScript reader inspecting `manifest.json` would silently
corrupt `sealed_len` just as readily.

#### Logical types

The schema declares two logical types, so that an implementation knows from the
schema alone that a JSON string carries integer semantics:

| Logical type | JSON | Domain |
|---|---|---|
| `int64_decimal` | string | −2^63 .. 2^63 − 1 |
| `uint64_decimal` | string | 0 .. 2^64 − 1 |

**`uint64_decimal` grammar** — ASCII digits only; no leading `+`; no leading
zeros except the single value `0`; no decimal point; no exponent; no whitespace;
negative values forbidden.

```text
valid:    0    1    42    18446744073709551615
invalid:  01   +1   -1    1.0    1e3    " 1"    "1 "    ""
```

**`int64_decimal` grammar** — as above, plus an optional leading `-`. `-0` is
forbidden (it is not a distinct value and would give one integer two spellings,
which would give one record two hashes).

```text
valid:    0    1    -1    9223372036854775807    -9223372036854775808
invalid:  01   -01  +1    -0     1.0    1e3    " 1"    "1 "    ""
```

Every string is parsed as an exact arbitrary-precision integer and then
range-checked against its declared domain. **A conforming parser must never
route an `int64_decimal` or `uint64_decimal` value through a floating-point
type**, not even transiently.

#### What stays a JSON Number

Fields whose declared domain is deliberately small and bounded stay Numbers,
because they cannot approach `2^53` by construction:

| Field | Declared domain |
|---|---|
| `schema_version` major/minor | already a string (`"1.0"`) |
| `descriptor_version`, `artifact_schema_version` | bounded `uint16` |
| `channels[].index` | bounded `int32`, ≤ channel count |
| `sample_index_in_packet` (in `raw_ref`) | bounded `int32`, ≤ samples per packet |
| `packet_counter_width_bits`, `samples_per_packet` | bounded `uint16` |
| `nominal_sample_rate_hz.value` | bounded numeric |
| `writer_config.*` (`chunk_max_seconds`, `chunk_max_rows`, `clock_snapshot_interval_seconds`) | bounded `int32` |
| protocol payload fields such as `block_index` | bounded by the protocol |

The declared bound is part of the schema. A field cannot be quietly widened to
`int64` later while keeping its JSON Number representation; widening the domain
changes the representation and is therefore a **major** schema version bump
(§17).

#### Arrow is unaffected

**This rule governs JSON serialization only.** Arrow has exact native 64-bit
integer types, so raw tables keep them:

```text
Arrow  :  host_arrival_monotonic_ns   int64          (native, exact)
JSON   :  "host_arrival_monotonic_ns": "884413221000" (int64_decimal)
```

Same semantic value, different serialization. `observations.value_i64`,
`value_u64` and `value_f64` are Arrow columns and stay native — R2 does not
touch them, and turning them into strings would be a real loss for no gain.

**JSONL files** — `lifecycle.jsonl`, `annotations.jsonl`, `chunks.jsonl`,
`events/events.jsonl` — carry exactly one canonical record per line, terminated
by `\n`. **The terminating newline is not part of the hashed bytes.**

**`record_sha256` procedure**, in this order, so that a verifier can run it
inverted and deterministically:

```text
1. construct the logical record
2. omit the record_sha256 key entirely   (omit, not null, not empty string)
3. canonicalize per RFC 8785
4. SHA-256 over those canonical bytes
5. insert record_sha256 with the resulting lowercase hex digest
6. write the final record, canonicalized per RFC 8785
```

Verification: parse the line, remove `record_sha256`, canonicalize, hash,
compare. Because verification re-canonicalizes from the parsed object rather
than trusting the bytes on disk, **whitespace or key-order tampering in an
external representation cannot pass** — it either reproduces the same canonical
bytes, in which case nothing changed, or it does not, in which case the hash
fails.

`prev_record_sha256` is the previous record's `record_sha256`; the first record
in a chain uses `"0" * 64`.

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
and close. It must never produce a truncated-but-committed chunk. — APPROVED,
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

**APPROVED.** Written exactly once, at finalization, via tmp + fsync + rename +
fsync(dir). Before finalization it **does not exist**, and its absence is the
primary signal that a session was not cleanly closed.

```json
{
  "schema_name": "session_package", "schema_version": "1.0",
  "session_id": "9f2c1e40-…",
  "sealed_at": { "utc_ns": "…", "monotonic_ns": "…" },
  "lifecycle_seal": { "path": "lifecycle.jsonl", "sealed_len": "8421", "sealed_sha256": "…" },
  "events_seal":    { "path": "events/events.jsonl", "bytes": "20144", "sha256": "…" },
  "streams": [
    { "stream_id": "muse.eeg", "required": true, "close_status": "CLEAN",
      "descriptor_sha256": "…", "chunk_count": "118",
      "chunk_chain_head_sha256": "…", "first_packet_seq": "0", "last_packet_seq": "88412" }
  ],
  "inventory": [ { "path": "allocation.json", "bytes": "812", "sha256": "…" } ],
  "schemas": [ { "schema_id": "block_start.v1", "path": "schemas/block_start.v1.json", "sha256": "…" } ],
  "scope_note": "logs/, annotations.jsonl, annotations.head.json and data/derived/ are OUTSIDE this manifest by design."
}
```

**`annotations.jsonl` and `annotations.head.json` are excluded from
`manifest.inventory` and from every manifest byte hash.** They must be: both are
written *after* the manifest is sealed, so including them would recreate exactly
the ordering contradiction that finding F2 closed. Their integrity is therefore
**not** established by the manifest — it is established only by the §5.1
verification (hash chain plus head pointer), and they are authoritative only
through that path. A reader that verifies the manifest has verified the sealed
package; it has **not** verified the annotations, and must run §5.1 separately
before reporting any outcome.

**The manifest carries no `outcome` field.** Outcome is owned by the lifecycle
log — the sealed prefix plus `annotations.jsonl`, resolved as in §5.1 — while
the manifest owns bytes. The manifest pair is therefore a **finalization
marker**, never a completion marker: a cleanly aborted session produces an
identical, fully valid pair. The manifest binds to the outcome indirectly and
tamper-evidently, through `lifecycle_seal.sealed_len` + `sealed_sha256`: the
sealed prefix cannot be edited without detection, while legitimate post-seal
annotations remain possible as appends beyond `sealed_len`.

Nothing is appended to a hashed log after sealing, which removes the ordering
contradiction Codex found in the first draft (finding F2), where
`FINALIZE_COMPLETE` was written into a file the manifest had already hashed.

---

## 14. Checksums and finalization

**APPROVED.**

```text
1. seal every open chunk, or leave it .part and record the fact
2. append FINALIZING to lifecycle.jsonl                        -> fsync
3. append the terminal CLOSED record: closure_condition + recording_outcome
                                                               -> fsync
4. seal events/events.jsonl (no further writes)                -> fsync
5. snapshot every schema used into schemas/
6. write annotations.head.json.tmp -> fsync -> rename -> fsync(dir)
      { "bytes": "0", "record_count": "0", "head_record_sha256": null }
   NOTE: written here, but deliberately NOT in manifest.inventory and NOT
   hashed into the manifest (13) - it is mutable by design.
7. sha256 every in-scope file; verify every chunk chain end to end
8. write manifest.json.tmp -> fsync -> rename -> fsync(dir)
9. write manifest.sha256.tmp -> fsync -> rename -> fsync(dir)   <-- FINALIZATION marker
10. registry upsert (derived)
```

The **existence of a matching `manifest.json` + `manifest.sha256` pair** is the
**finalization marker** — also called the sealed-package marker. It proves one
thing and one thing only:

> the package was cleanly finalized and sealed.

It does **not** prove that the recording outcome was `COMPLETED`. A cleanly
finalized `ABORTED` or `TECHNICAL_FAILURE` session produces an identical, fully
valid pair. **This document never calls the manifest pair a completion marker.**

Step 9 is the last durable act; a crash anywhere before it leaves a session that
is, correctly, not finalized.

**Step 6 exists because §5.1 treats a missing `annotations.head.json` as
tampering.** Every cleanly finalized session must therefore ship one, initialised
to zero records, whether or not an annotation is ever written; without it every
clean session would evaluate `INDETERMINATE` and the completion predicate would
be false for all of them. It is written *before* the manifest is built so a
finalized package is never missing it, and it is excluded from the inventory
because it is one of the three objects §14.1 permits to change afterwards —
hashing it would invalidate `manifest.sha256` on the first annotation.

### The completion predicate

There is exactly one predicate, with **eight conditions**. It evaluates the
**effective** recording outcome (§5.1), not the sealed one.

> `is_completed(package)` is true **if and only if all eight hold**:
>
> 1. `manifest.json` exists and `manifest.sha256` matches it;
> 2. every file in `inventory` exists and hashes to its recorded value;
> 3. the first `lifecycle_seal.sealed_len` bytes of `lifecycle.jsonl` hash to
>    `lifecycle_seal.sealed_sha256`;
> 4. within that sealed prefix the terminal state is `CLOSED`, with
>    `closure_condition = CLEAN` and **sealed** `recording_outcome = COMPLETED`;
> 5. every stream declared `required` in `run.json` has `close_status = CLEAN`
>    in the manifest;
> 6. no `.part`, `.tmp` or `.open` file exists anywhere in the package;
> 7. every chunk file present under `raw/` appears in its `chunks.jsonl` hash
>    chain, and every chain verifies end to end;
> 8. the **effective** recording outcome computed by §5.1 is `COMPLETED` —
>    which requires that `annotations.jsonl` is either absent, or present with a
>    verifying hash chain, no rejected record, and no accepted downgrade.

Condition 4 tests the sealed outcome; condition 8 tests the effective one.
Both are required, and neither implies the other: condition 4 alone would miss a
valid downgrade, and condition 8 alone would accept an outcome that was never
sealed. Any package where §5.1 returns `INDETERMINATE` fails condition 8.

Each of conditions 4, 5, 8 and the four-transition rule closes a false-complete
state that was actually constructed against an earlier draft: a post-seal
`CLASSIFIED: COMPLETED` promoting an aborted session; a required device
disconnecting while every other condition still held; an operator abort being
reclassified upward; and a valid human downgrade being invisible to a predicate
that read only the sealed prefix.

### 14.1 What may change after sealing — the complete list

**APPROVED.** This is the authoritative list. Any other statement in this
document that appears to permit post-seal mutation is wrong and defers to this
one.

**Inside the sealed acquisition package**, exactly three objects may change
after `manifest.sha256` lands:

| Object | How it changes | Constraint |
|---|---|---|
| `annotations.jsonl` | **append only** | Hash-chained; downgrade-only; never rewritten, never truncated |
| `annotations.head.json` | **atomic whole-file replace** | tmp → fsync → rename → fsync(dir), *after* the append it describes lands |
| `logs/` | freely | Operational only; **never authoritative**; outside manifest scope |

Neither annotations file is in `manifest.inventory` or any manifest hash (§13),
which is exactly why appending an annotation does not invalidate
`manifest.sha256`. `annotations.head.json` is the one file in the package
written by whole-file replacement rather than append; that is deliberate, and
§5.1 is what verifies it.

**Immutable after sealing — no exceptions:**

`allocation.json`, `run.json`, every `raw/<stream>/descriptor.json`, the sealed
prefix of `lifecycle.jsonl`, `events/events.jsonl`, everything under `raw/`
(payload logs, packet / sample / observation chunks, `chunks.jsonl`, every
`*.commit.json`), every file under `schemas/`, `manifest.json` and
`manifest.sha256`.

`lifecycle.jsonl` deserves an explicit note: **the sealed prefix is immutable
and hash-protected**, and after sealing nothing appends to it at all —
post-seal classification goes to `annotations.jsonl` instead. That is what
removed the F2 ordering contradiction, and it is why `lifecycle.jsonl` does not
appear in the mutable table above.

**Outside the package**, and therefore not package content at all:

| Object | Status |
|---|---|
| `data/registry.sqlite` | Fully derived, freely mutable, rebuildable by scanning packages |
| `data/derived/<session_id>/…` | Regenerable artifacts, added and removed at will |

The distinction matters because "mutable" means two different things on the two
sides of that line. Inside the package, mutability is a narrowly-scoped,
verifiable exception carved out of an otherwise sealed unit. Outside it,
mutability is the normal state of a cache.

---

## 15. Raw versus derived contract

**APPROVED.**

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
  "generated_at_utc_ns": "…",
  "outputs": [ { "path": "psd.parquet", "sha256": "…" } ]
}
```

This answers *exactly which raw bytes produced this result?* — by content hash,
not by path. **Resolution is by `(session_id, sha256)`; the relative path is a
hint.** A reader must never bind to a path whose hash does not match; it fails
closed (finding G6).

---

## 16. Replay contract

**APPROVED.**

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

**APPROVED.**

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

**APPROVED.** Q7 answer.

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

**APPROVED.** Every row was simulated by an independent reviewer in Pass 2.

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
| Q1 | Container format | Directory per session | **APPROVED** |
| Q2 | Serialisation | Arrow IPC stream (raw chunks), length-prefixed binary (transport payloads, when available), JSONL (events, lifecycle, annotations, chunk index), JSON canonicalized per RFC 8785 where hashed (allocation/run/descriptor/manifest), SQLite (registry), Parquet (derived only) | **APPROVED** |
| Q3 | Session identifier | UUIDv4, opaque, no embedded time; uniqueness via `os.mkdir` | **APPROVED** |
| Q4 | Metadata set | Split by when known: `allocation.json` / `run.json` / `descriptor.json`; one authority per question (§7.3) | **APPROVED** |
| Q5 | Event and marker representation | One `events/events.jsonl`; devices in `raw/`, meaning in events, joined by `raw_ref`; per-schema versioning; schemas snapshotted into the package | **APPROVED** |
| Q6 | Partial and failed sessions | Same structural shape plus `closure_condition` + `recording_outcome`; absence of a manifest pair is the signal; recovery never guesses | **APPROVED** |
| Q7 | Participant linkage | Generated `^P[0-9]{3,6}$` pseudonym; mapping outside `data/`; device aliases, never serials | **APPROVED** |
| Q8 | Storage and retention | Canonical unit = the `sessions/<id>/` directory; backup moves packages (registry is rebuildable); derived is regenerable. **Retention period, offsite location and backup cadence remain undecided.** | **APPROVED** / **OPEN — HUMAN DECISION REQUIRED** |

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
| Optional payload capture of *available* transport bytes, off after "verification" | Assumes a decoder will never regress across firmware or library versions. Distinct from `library_decoded`, where the bytes never reached us at all |
| Unconditionally mandatory payload capture | Not implementable against a backend that never exposes transport bytes; it would force an adapter to fabricate a payload log, which is worse than an honest absence |
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
6. **The annotation tampering threat model.** `annotations.head.json` defeats
   accidental loss, truncation and single-file tampering, but not an actor who
   rewrites both files consistently. Whether that matters here — and whether it
   justifies append-only media or signatures — is a study-operations call.

> **Closed in CL-002A-R2:** *"whether transport payload capture may be disabled
> when the bytes are available."* For Session Package v1 the answer is **no**,
> fixed by the §9.1 invariant.
>
> **Closed in CL-002A-APPROVAL (`DECISIONS.md` D19):** *"whether lateral
> reclassification `ABORTED` <-> `TECHNICAL_FAILURE` should be permitted."* For
> Session Package v1 the answer is **no** — the annotation model stays
> downgrade-only with exactly the four transitions in §5. A mis-classified
> session therefore cannot be corrected sideways, and that cost is accepted
> knowingly in favour of minimising mutable scientific state. A future schema
> revision may revisit it.
>
> Both are recorded here rather than deleted, so a reader can see that the
> question was asked and answered rather than overlooked.

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
13. **The Muse S Athena acquisition backend and its available
    `raw_capture_level`.** Whether BrainFlow exposes the BLE notification
    payloads or only decoded board data; whether a direct-BLE or
    OpenMuse-style route is viable; packet stability; and how much timing is
    visible at each level. **This task deliberately does not select a backend**,
    and introduces no new dependency. The schema supports
    `transport_payload` and `library_decoded` equally, so the choice can be made
    from measurements instead of from guesses.

None of these has been converted into an assumed schema fact. The schema
preserves the raw information needed to answer all of them later.

### DEFERRED SAFELY

14. Compression codec for Arrow chunks (LZ4 vs ZSTD vs none) — a per-chunk
    property, changeable without a schema change.
15. Registry indexes and query surface — derived, rebuildable at will.
16. Any CLI ergonomics.

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
5. `LifecycleLog` and `AnnotationLog`, including the four-transition rule, the
   `from`-must-match check, and the `annotations.head.json` pointer with its
   atomic update.
6. `Finalizer` — the §14 sequence and the eight-condition completion predicate
   as an executable function, built on an `effective_outcome()` function
   implementing §5.1 including its fail-closed path.
6a. An RFC 8785 (JCS) canonicalization helper, implemented or vendored, tested
   against the RFC's published vectors, plus the `record_sha256` procedure of
   §12.2. Every hashed record in the system depends on it, so it lands before
   anything that writes a hash.
6b. `int64_decimal` / `uint64_decimal` logical types (§12.2.1): a strict parser
   and serializer with the exact grammars, domain range checks, and a guarantee
   that no value transits a floating-point type. Every model field depends on
   them, so they land before 6a.
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
5. Simulate `ENOSPC` mid-chunk → no commit record is written, no truncated chunk
   is ever committed, and the session closes
   `closure_condition = CLEAN, recording_outcome = TECHNICAL_FAILURE` with the
   error in `outcome_reason` — **not** unclean and **not** `UNCLASSIFIED`,
   because the cause is known (§12.3, §19).
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

**Effective outcome (§5.1) — one test per row of the truth table**

| Test | Sealed outcome | Annotations | Expected `effective_outcome` | Expected `is_completed` |
|---|---|---|---|---|
| **A** | `COMPLETED` | none | `COMPLETED` | **true** (given conditions 1–7 pass) |
| **B** | `COMPLETED` | `ABORTED` | `ABORTED` | false |
| **C** | `COMPLETED` | `TECHNICAL_FAILURE` | `TECHNICAL_FAILURE` | false |
| **D** | `ABORTED` | attempts `COMPLETED` | `ABORTED`; annotation **rejected** and reported | false |
| **E** | `COMPLETED` | log corrupt (broken hash chain) | **UNDEFINED / INDETERMINATE** | false — and it must **never** silently report `COMPLETED` |

Test E is the one that matters most: it asserts the fail-closed path. A test
that merely checks "corrupt log → not completed" would pass even on an
implementation that fell back to the sealed outcome and happened to be aborted;
it must assert that the package integrity status is reported as indeterminate.

Additional:

11c. `UNCLASSIFIED` → `ABORTED` and `UNCLASSIFIED` → `TECHNICAL_FAILURE` are
    both accepted; `UNCLASSIFIED` → `COMPLETED` is rejected.
11d. After `COMPLETED` → `ABORTED`, a second annotation attempting to restore
    `COMPLETED` is rejected; the effective outcome stays `ABORTED`.
11e. A lateral `ABORTED` → `TECHNICAL_FAILURE` annotation is rejected under the
    current four-transition rule, and the rejection is reported rather than
    silently ignored (see the open question in §5).
11f. **Deleted downgrade.** Sealed `COMPLETED`, one valid `ABORTED` annotation,
    then `annotations.jsonl` is deleted while `annotations.head.json` still
    reports one record → **INDETERMINATE**, `is_completed = false`. Never
    `COMPLETED`.
11g. **Boundary truncation.** Two valid annotations, then the file is truncated
    on the record boundary after the first → head mismatch → INDETERMINATE.
11h. `annotations.head.json` missing entirely → INDETERMINATE (finalization
    always writes it).
11i. `head.record_count = 0` with a non-empty `annotations.jsonl` →
    INDETERMINATE.
11j. **Stale `from`.** Two records, the second declaring `from: COMPLETED` after
    the first already downgraded to `ABORTED` → the second is rejected, the
    effective outcome stays `ABORTED`, and `is_completed = false`.
11k. `annotations.jsonl` and `annotations.head.json` appear in **neither**
    `manifest.inventory` nor any manifest hash, and adding an annotation does
    **not** invalidate `manifest.sha256`.

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
19a. **RFC 8785 canonicalization** (all of these, each its own assertion):
    (i) two dicts with different insertion order produce identical canonical
    bytes and identical `record_sha256`;
    (ii) non-ASCII and astral-plane strings survive a canonicalize → hash →
    parse → re-canonicalize round trip byte-identically, including keys that
    sort differently under UTF-16 code units than under code points;
    (iii) `NaN`, `Infinity` and `-Infinity` are **rejected at write time**, not
    serialized;
    (iv) `-0.0` in a hashed field is rejected;
    (v) changing any single value changes `record_sha256`;
    (vi) reformatting a record's whitespace or key order on disk does **not**
    defeat verification, because the verifier re-canonicalizes from the parsed
    object;
    (vii) canonical re-serialization of a verified record reproduces the
    original bytes exactly;
    (viii) the helper matches the RFC 8785 published test vectors.
19b. Payload framing round-trips: `magic`, `packet_seq`, `payload_len`, payload
    bytes and `crc32c` survive a write/read cycle; a `payload_ref.offset` that
    does not land on `magic` fails closed instead of returning bytes. (RC7)
19c. A chunk with a valid `NNNNNN.commit.json` sidecar but no `chunks.jsonl`
    record is reported as orphaned and is **not** treated as committed. (RC5)
19d. An `ENOSPC` close records `TECHNICAL_FAILURE` with a reason, never
    `UNCLASSIFIED`. (RC9)

**Raw capture level (§9.0)**

23. A `transport_payload` stream writes a payload log, sets
    `transport_payload_preserved: true`, and every `packets.payload_ref` is
    non-null and resolves to a record whose `magic` matches.
24. A `library_decoded` stream writes **no** payload log, sets
    `transport_payload_preserved: false`, and every `packets.payload_ref` is
    **null** — never a fabricated pointer, never a zero-length record.
25. A `library_decoded` chunk commit record **omits** the `payloads` entry
    entirely, and a reader driven by `raw_capture_level` does not report the
    absent file as a lost file.
26. A `synthetic` stream records `raw_capture_level: "synthetic"` and its
    generator seed, and replaying it reproduces identical values.
27. `acquisition.backend.name`, `.version` and `.adapter_version` survive a
    write/read round trip and appear in the manifest inventory hash.
28. Round trip on a session containing **both** a `transport_payload` stream and
    a `library_decoded` stream — the generic format handles both without
    branching outside the descriptor.

**Integer exactness in JSON (§12.2.1)**

- **J1 — UTC nanoseconds.** `1787923530123456789` survives
  `int64 -> int64_decimal -> JCS -> parse -> int64` with **exact** equality. The
  test asserts equality against the integer, not against a float, and fails if
  the value round-trips as `1787923530123456768`.
- **J2 — int64 limits.** `-9223372036854775808` and `9223372036854775807`
  round-trip exactly.
- **J3 — uint64 maximum.** `18446744073709551615` round-trips exactly.
- **J4 — invalid representations rejected** where an integer-decimal logical
  type is expected: `"01"`, `"+1"`, `"-0"`, `"1.0"`, `"1e3"`, `" 1"`, `"1 "`,
  `""`. Also `"-01"`, a value outside the declared domain, and a bare JSON
  Number where a decimal string is required.
- **J5 — hash stability.** Two independent logical constructions of the same
  int64-valued record (different key insertion order, different code paths)
  produce **identical JCS bytes and identical SHA-256**.
- **J6 — no numeric coercion.** Parsing a canonical document never routes an
  `int64_decimal` / `uint64_decimal` field through IEEE-754. Asserted with a
  value whose float round trip is detectably lossy, so a hidden float hop
  cannot pass.
- **J7 — bounded Numbers stay Numbers.** `channels[].index`,
  `descriptor_version` and `writer_config.*` remain JSON Numbers, and a decimal
  string in those positions is rejected. The rule is symmetric: a field's
  representation is fixed by its declared domain in both directions.

**Raw capture invariant (§9.1)**

- **P1** — `raw_capture_level: "transport_payload"` with
  `transport_payload_preserved: false` **fails model validation**.
- **P2** — `transport_payload` + `true` validates.
- **P3** — a committed `transport_payload` chunk whose payload artifact is
  missing fails verification.
- **P4** — a `library_decoded` chunk carrying a payload artifact is invalid: a
  fabricated transport payload must be caught, not tolerated.
- **P5** — a `library_decoded` packet with a non-null `payload_ref` is invalid.
- **P6** — a mixed session validates end to end with
  `muse.eeg = library_decoded`, `polar.ecg = transport_payload` and
  `synthetic.x = synthetic`, each stream independently satisfying its own
  invariant.
- **P7** — `synthetic` with `transport_payload_preserved: true` is invalid.

**Post-seal mutability (§14.1)**

- **M1** — appending an annotation and updating `annotations.head.json` leaves
  `manifest.sha256` valid and every inventory hash unchanged.
- **M2** — modifying any file in the §14.1 immutable list is detected by
  manifest verification. Parameterised over the whole list, so a file added to
  the package later cannot quietly escape the check.
- **M3** — a cleanly finalized session with **zero** annotations still ships
  `annotations.head.json` with `record_count: "0"`, and `is_completed` is
  **true**. This is the regression test for RC-R2-3: without step 6 of §14,
  every clean session would evaluate `INDETERMINATE`.
- **M4** — `annotations.head.json` is absent from `manifest.inventory`, yet
  present on disk in every finalized package.
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
scientific outcome; an atomic finalization marker as the only evidence a package
was cleanly sealed; typed null plus provenance for missing timing; counters rather than
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

**Verdict: `GO WITH REQUIRED CHANGES`** — nine required changes, RC1–RC9. All
nine were accepted; none was rejected or partially accepted.

**Correction, recorded 2026-08-28 during CL-002A-R1:** six of the nine
(RC3–RC9) landed in commit `eb89ed0`. **RC1 and RC2 did not.** An editing script
aborted on a failed anchor match before writing, silently discarding those edits
while the trace below still reported them as applied. §5 and §14 therefore
shipped with the pre-RC1 wording — including the `UNCLASSIFIED` → `COMPLETED`
path RC1 existed to close. Both were re-applied, and extended, in CL-002A-R1
(§25.5). The lesson is recorded rather than quietly fixed: **a review trace that
claims a change was applied is worth nothing unless the claim is verified
against the file.**

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

### 25.5 CL-002A-R1 — final correction pass

**Reviewer:** OpenAI Codex CLI 0.133.0, `codex exec`, read-only sandbox, single
pass. **Scope: the CL-002A-R1 corrections only** — no redesign, no reopening of
settled architecture.

Three issues were corrected before review:

| Issue | Problem | Resolution | § |
|---|---|---|---|
| 1 | The manifest pair was called a "completion marker" in one place and a "finalization marker" in another; and the predicate read only the sealed outcome, so a valid post-seal downgrade could be invisible to it | Terminology unified on **finalization / sealed-package marker**. **Sealed** and **effective** recording outcome defined separately, with a deterministic fail-closed algorithm. One predicate, **eight conditions**, evaluating the effective outcome | §5, §5.1, §13, §14 |
| 2 | "Transport bytes are always canonical raw" is not implementable against a backend that never exposes them — BrainFlow may hand us only decoded board data | `raw_capture_level` (`transport_payload` / `library_decoded` / `synthetic`), `transport_payload_preserved`, `acquisition.backend`, `decode_boundary`. Payload capture is mandatory **where payloads are exposed**; where they are not, that is recorded, never fabricated. `payload_ref` becomes nullable | §9.0, §9.0.1, §9.1, §12.2 |
| 3 | Canonical JSON was specified as `json.dumps(sort_keys=True, …)` with a claim that independent implementations must agree — too strong | **RFC 8785 (JCS) adopted.** The `json.dumps` claim is explicitly withdrawn, with the two concrete divergences named (UTF-16 key ordering, ECMAScript number formatting). NaN / Infinity / `-0.0` rejected at write time | §12.2 |

Also corrected: RC1 and RC2 from Pass 3, which the trace had claimed were
applied and which were not (see §25.4).

Acceptance tests added: **A–E** for the effective-outcome truth table plus 11c–e;
19a rewritten as eight distinct canonicalization assertions including the RFC
8785 test vectors; and 23–28 for raw capture level.

**Unresolved after this pass:** one new open question — whether lateral
reclassification `ABORTED` <-> `TECHNICAL_FAILURE` should be permitted (§22,
human decision) — and one new hardware-validation item, the Athena acquisition
backend and its available capture level (§22). Neither blocks CL-002B: the
schema supports both outcomes of each.

**Verdict: `GO WITH REQUIRED CHANGES`** — five required changes, RC-R1-1 to
RC-R1-5. All five accepted and applied; none rejected.

| RC | Finding | Resolution | § |
|---|---|---|---|
| RC-R1-1 | The executive summary and the directory listing still asserted unconditional transport-byte capture, contradicting the new §9.0 | Both rewritten to the acquisition-boundary definition; `payloads/` annotated as existing only at `raw_capture_level = transport_payload` | §1, §3 |
| RC-R1-2 | `annotations.jsonl` had no declared manifest scope | Explicitly **excluded** from `manifest.inventory` and every manifest hash — they are written after sealing, so including them would recreate the F2 ordering contradiction. Authoritative only through §5.1 | §13 |
| RC-R1-3 | **A hash chain proves the records present are intact; it cannot prove none was removed.** Deleting or boundary-truncating `annotations.jsonl` left a valid shorter chain, and `absent -> sealed` then restored a sealed `COMPLETED` | Added `annotations.head.json`, written at finalization and updated atomically after each append, recording expected `bytes`, `record_count` and `head_record_sha256`. Any mismatch is INDETERMINATE. Residual limitation (an actor rewriting both files) stated rather than implied | §5.1 |
| RC-R1-4 | §5.1 never required an annotation's `from` to match the outcome in force, so two conforming implementations could disagree on a two-downgrade log | Every annotation carries `from` and `to`; `from` must equal the running effective outcome or the record is rejected | §5.1 |
| RC-R1-5 | Acceptance test 5 said an `ENOSPC` session "closes unclean", contradicting §12.3 and §19 | Test corrected to `CLEAN` / `TECHNICAL_FAILURE` with `outcome_reason` | §24 |

RC-R1-3 is the substantive one: it is a **fifth false-complete path**, and it
was invisible to the three earlier passes because they all reasoned about
corruption rather than deletion. Tests 11f–11k were added for it and for RC-R1-4.

**No new source-of-truth conflict and no weakened crash consistency** were
found in the corrections.

### 25.6 CL-002A-R2 — numeric exactness and final invariants

**Reviewer:** OpenAI Codex CLI 0.133.0, `codex exec`, read-only, single pass.
**Scope: the CL-002A-R2 changes only** — integer representation, the raw-capture
invariant, and the post-seal mutability list. No architecture reopened.

Three corrections were applied before review:

| # | Problem | Resolution | § |
|---|---|---|---|
| R2-1 | JSON examples carried `"utc_ns": 1787923530123456789` as a JSON Number. Under RFC 8785 that is not representable — a conforming parser rounds it to `…456768`, silently wrong by 21 ns, in values that feed provenance and hashes | `int64_decimal` / `uint64_decimal` logical types: declared-int64/uint64 domains are canonical decimal **strings** in JSON, with exact grammars and a ban on transiting a float. Bounded fields stay Numbers. Arrow untouched. 13 examples converted | §12.2.1 |
| R2-2 | `transport_payload` + `transport_payload_preserved: false` was a reachable state, leaving two implementers free to disagree about whether a payload artifact must exist | v1 invariant: the two fields are locked (`IFF`), the human-disable escape is removed, and chunk shape is a per-level table with exactly one valid shape each | §9.1, §12.2 |
| R2-3 | The post-seal mutability list predated `annotations.head.json` and was inconsistent with §5.1 | §14.1 is now the single authoritative list: three mutable objects inside the package, everything else immutable and enumerated, with `registry.sqlite` and `data/derived/` explicitly *outside* | §14.1 |

**Verdict: `GO WITH REQUIRED CHANGES`** — three required changes, all applied,
none rejected.

| RC | Finding | Resolution |
|---|---|---|
| RC-R2-1 | §5.1 still called `annotations.head.json` "the one file in the package" mutable after sealing, contradicting §14.1's three | Reworded to "one of the three objects §14.1 permits to change", noting it is the only one replaced atomically rather than appended |
| RC-R2-2 | The §12.2 chunk write sequence wrote `payloads/*.part` unconditionally, contradicting the R2-2 invariant | Payload step made explicitly conditional on `raw_capture_level = "transport_payload"`, with the no-placeholder rule stated |
| RC-R2-3 | **§5.1 makes a missing `annotations.head.json` INDETERMINATE, but §14 never wrote one.** Every cleanly finalized session would therefore have failed the completion predicate | Added step 6 to §14: write the zero-record head file before building the manifest, explicitly outside inventory scope. Regression tests M3 and M4 |

RC-R2-3 is the substantive one. It is not a wording problem: as written, the
R1 design would have made `is_completed` false for **every** session, and the
defect was introduced by R1's own fix. It is the second time in this review
sequence that a correction created the bug its own next pass had to find.

**Unresolved after this pass:** none new. The §22 open list lost one item
(payload capture disable, now answered `no` for v1) and gained none.

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
| 13 | What may remain mutable operationally? | **Inside** the sealed package, exactly three: `annotations.jsonl` (append-only, downgrade-only), `annotations.head.json` (atomic replace) and `logs/`. **Outside** it: `registry.sqlite` and everything under `data/derived/`, both fully derived and freely rebuildable | §5.1, §6, §14.1 |
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
