# HARDWARE

Planned devices for Study 001, and the verification status of each.

> **Nothing in this repository has been tested against a physical device.**
> Every row in the status table below reads *pending verification*. No code
> path, comment or document may state or imply otherwise until a row here
> changes, with a date and a method.

---

## Verification status

| Device | Role | Transport | Planned library | Status |
|---|---|---|---|---|
| Muse S Athena | EEG (and whatever else the device exposes) | BLE | `brainflow` | **Pending verification** — never connected |
| Polar H10 | Cardiac | BLE | `polar-python` (over `bleak`) | **Pending verification** — never connected |
| Adafruit QT Py | Marker / synchronisation channel | USB serial | `pyserial` | **Pending verification** — no firmware written |

## What "verified" means here

A device is verified only when someone has connected the physical unit and
measured its behaviour. Recording a verification requires all of:

- date of the test
- device firmware or hardware revision
- host OS and the library version used
- what was measured and how
- what was observed, including failure modes

A device that connects successfully once is not verified. Verification is about
the behaviours the study depends on — which timing quantities the device
actually provides, whether counters wrap, what happens on disconnect and
reconnect, what a dropped packet looks like from our side, and what the device
does when the host is slow.

## Assumptions currently in force

These are unverified and are recorded so they can be checked, not relied on.

- **Muse S Athena via `brainflow`.** That BrainFlow supports this specific
  device revision, and which board identifier applies, is assumed from vendor
  documentation and not confirmed here. Channel layout, sample rate and the
  timing quantities BrainFlow surfaces (versus the ones it synthesises on the
  host) are all unconfirmed. BrainFlow producing a timestamp column does not
  establish that the timestamp came from the device — see `TIMING.md`.
- **Polar H10 via `polar-python`.** `polar-python` is a third-party
  community library (`github.com/zHElEARN/polar-python`), not a Polar product.
  Its maintenance status, its coverage of the device's data streams, and the
  fidelity with which it exposes device-side timing are all unverified. Whether
  the study needs streams this library does not expose is an open question.
- **QT Py marker channel.** No firmware exists in `firmware/`. The marker
  mechanism, its electrical interface, and how a marker is placed on a common
  timeline with the BLE streams are undesigned. Serial round-trip latency and
  its variability are unmeasured.
- **BLE generally.** Concurrent connections to two BLE peripherals from one
  host adapter, and the effect of that on packet-arrival jitter, are unmeasured.
  Assume nothing about co-existence until it is tested.

## Open questions

- Does each device expose a device-side clock, a counter, both, or neither?
- What is the actual, sustained sample rate under BLE, as opposed to nominal?
- What does a reconnect do to counters and to any device-side time base?
- Which host platforms are supported? BLE stacks differ substantially between
  Linux/BlueZ, macOS and Windows, and `bleak` abstracts them imperfectly.
- Does the study require a hardware synchronisation path between devices, or is
  post-hoc alignment sufficient? Undecided, and it depends on measurements that
  have not been taken.

## Recording a verification

When a device is tested, add a dated subsection below, update its row in the
status table, and log any structural consequence in `DECISIONS.md`. Do not
delete superseded verification entries — a behaviour that changed with a
firmware update is itself a finding.

### Verification log

*(empty — no device has been tested)*
