# TIMING

This document defines the timing vocabulary for the project. Use these terms
exactly; do not coin synonyms, and do not merge two quantities into one field.

**No timing threshold is frozen.** No tolerance, no acceptable drift, no
maximum jitter, no uncertainty budget is specified here. Those are empirical
and must be measured on real hardware first (`HARDWARE.md`) and then decided by
a named human (`DECISIONS.md`). Any number that appears in code before that has
been invented, and `AGENTS.md` §6 forbids it.

---

## Why the distinctions matter

A sample has more than one time. The moment the host noticed it is not the
moment the device produced it, and neither is the moment the underlying
physiological event occurred. Collapsing these into a single "timestamp" is
irreversible: once written, no later analysis can recover which quantity it
was. Every distinction below exists because it cannot be reconstructed after
the fact.

## The quantities

### Host arrival time
The time, on the host's clock, at which the host received the data — the
moment the packet surfaced to our process. Includes every delay in the path
(radio, OS scheduling, driver buffering, our own event loop). It is always
available, and it is always later than the sample it describes by an unknown
amount.

### Device-provided time
A time value supplied by the device itself, expressed on the device's clock.
Available only when the device provides one, and its meaning is
device-specific: it may refer to sample acquisition, packet assembly or packet
transmission. Which one it refers to must be established per device and
recorded in `HARDWARE.md`, not assumed.

### Packet / sample counter
A monotonically increasing count supplied by the device — of packets, of
samples, or of both. It is a sequence quantity, not a time. Its value is the
authoritative signal for **loss** and **ordering**: a gap in the counter is a
gap in the data, whatever the timestamps look like. Counters may wrap; the wrap
width is a device fact.

### Reconstructed sample time
A time assigned to an individual sample by the host, computed from some
combination of the quantities above and the device's nominal sample rate. It is
**derived**, never measured. It is stored **in addition to**, never instead of,
its inputs, and the procedure that produced it is recorded with it. Changing
the reconstruction procedure must not silently change the meaning of previously
recorded data (`AGENTS.md` §3).

### Offset
The instantaneous difference between the device clock and the host clock at a
point in time. An estimate, with its own error. Two devices each having an
offset to the host does not by itself make them mutually aligned.

### Clock drift
The rate at which the offset changes over time, because independent
oscillators do not run at identical rates. Drift is why an offset measured at
session start is not valid at session end, and why a single alignment
correction applied to a whole session is a modelling choice that must be
recorded as one.

### Packet-arrival jitter
The variability in host arrival time between successive packets. Property of
the transport and the host, not of the physiology. High jitter degrades what
can be inferred from arrival times; it does not by itself mean samples were
lost — the counter answers that.

### Sample-timing uncertainty
The residual uncertainty in when a given sample was actually acquired, after
all reconstruction. It aggregates unknown transport latency, quantisation of
device timestamps, offset estimation error, drift within the session and
counter ambiguity. It is a property to be **quantified and carried alongside
the data**, not a problem to be declared solved.

## Rules

1. Record every quantity the device actually provides. Do not discard one
   because another looks better.
2. Never overwrite a captured quantity with a derived one.
3. Label which clock a value is expressed on. A bare number with no clock is
   not usable.
4. Cross-device alignment is an analysis-time operation with its own recorded
   procedure and its own uncertainty. Acquisition does not perform it.
5. Counters govern loss and ordering. Timestamps govern time. Do not use one
   to answer the other's question.
6. When a threshold is eventually needed, it arrives from `DECISIONS.md` with
   the measurement that justified it — not from this document.

## Open

- Which quantities each planned device actually exposes. Unverified for all
  three; see `HARDWARE.md`.
- Whether a host-side reference clock discipline (e.g. NTP/PTP state) is
  captured per session, and how its quality is represented.
- How the QT Py marker channel is placed on a common timeline with the BLE
  device streams. This is the hardest timing question in the study and it is
  open.
- Storage representation of all of the above: `SESSION_FORMAT.md` Q4/Q5.
