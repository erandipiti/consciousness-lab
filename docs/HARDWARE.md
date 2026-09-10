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

## Producing the evidence (CL-004)

The harness in `consciousness_lab.verification` records what a device actually
does. It **observes and does not conclude**: it describes the columns and
timings that arrived, and never says which of them is a clock, a counter or a
channel. That naming is what you do when you read the report and write an entry
below.

```bash
uv run consciousness-lab probe env   --purpose "..."            # host only, no device
uv run consciousness-lab probe scan  --purpose "..."            # what the host can see
uv run consciousness-lab probe muse  --purpose "..." --firmware "..." --method "..."
uv run consciousness-lab probe polar --purpose "..." --firmware "..." --method "..." --address "..."

# what a reconnect does: two windows around a real disconnection, side by side
uv run consciousness-lab probe reconnect  --device muse  --purpose "..." --firmware "..." --method "..."

# two peripherals on one adapter, which HARDWARE.md calls unmeasured
uv run consciousness-lab probe concurrent --polar-address "..." --purpose "..." --firmware "..." --method "..."

# the marker channel's round trip AND ITS SPREAD — the gate CL-007 sits behind.
# Bench only: no BLE, no participant.
uv run consciousness-lab probe serial --port /dev/cu.usbmodem101 --purpose "..." --firmware "qtpy-marker/1" --method "..."
```

`reconnect` records cycle 0 and cycle 1 under prefixed names and **computes no
difference between them**. Whether a counter reset or a timebase restarted is
what you conclude from seeing both series; a wrong conclusion baked into the
probe would be invisible.

`concurrent` holds both devices at once and records what each one did. It does
**not** compare against a solo run: run each device alone first, then both, and
read the three reports. Deciding that something "degraded" is a judgement, and
the probe does not make it.

The Polar capture runs two paths, because they fail for different reasons and
the difference is itself evidence: raw GATT notifications from every notifiable
characteristic (nothing decoded, so nothing can be misdecoded), and the PMD
streams that need a control-point handshake, through polar-python. Stream
parameters there come from the device's own `request_stream_settings` response —
never from a number chosen in our code, which would be inventing a device
parameter.

`--purpose`, `--method` and (for a device) `--firmware` have no defaults and the
run refuses to be written without them, because they are part of what a
verification *is* under `AGENTS.md` §7.

Reports land in `data/verification/`, never in `data/sessions/`. **A verification
run is not a recording session**: putting a device on to see whether it streams
produces no session package, and its output must not become study data.

Run `probe scan` before blaming a device. A host with no Bluetooth stack and a
device that is switched off look identical from inside a library that simply
times out, and confusing the two costs a bench session.

### Setting up the recording host (macOS)

The Mac is the recording host (`DECISIONS.md` D41). Three lines, then a script
that does the rest and checks its own work:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/erandipiti/consciousness-lab.git
cd consciousness-lab && ./scripts/bootstrap-mac.sh
```

`bootstrap-mac.sh` is idempotent, installs nothing silently, and finishes by
running `probe env` — so the first thing that happens on the machine is evidence
about it rather than an assumption. The lock is already resolved for macOS
(`bleak` pulls `pyobjc-framework-corebluetooth` under a `sys_platform ==
'darwin'` marker), so `uv sync --locked` needs no special handling.

**The one thing no script can do, and it looks exactly like broken hardware.**
CoreBluetooth grants Bluetooth access to the *application*, and a terminal does
not have it by default. Without it every scan returns nothing and every device
looks switched off.

> System Settings → Privacy & Security → Bluetooth → enable your terminal,
> then **quit and reopen** the terminal — a new tab is not enough.

That is why `probe scan` exists and why it comes before every device probe: a
host with no permission and a device with a flat battery are indistinguishable
from inside a library that simply times out.

Serial ports are `/dev/cu.usbmodem*` on macOS, not `/dev/ttyACM*`.

### A bench session, in order

The order is not arbitrary. Each step either produces evidence the next one
needs, or separates a host problem from a device problem before you can waste an
hour confusing them.

| # | command | what it settles |
|---|---|---|
| 1 | `probe env` | is this host capable at all — before any device is blamed |
| 2 | `probe scan` | does the host *see* anything; gets you the H10's address |
| 3 | `probe serial --port /dev/cu.usbmodem*` | the marker's round trip and its spread. No BLE, no participant — do it while the others charge |
| 4 | `probe muse` | what the Athena actually delivers, alone |
| 5 | `probe polar --address <from step 2>` | what the H10 actually delivers, alone |
| 6 | `probe reconnect --device muse` | what a reconnect does to counters and timebase |
| 7 | `probe concurrent --polar-address <…>` | whether one adapter sustains both |

Steps 4 and 5 must run **alone** before step 7, or step 7 has nothing to be
compared against — and the comparison is the operator's, not the probe's.

Every command needs `--purpose` and `--method` in your own words, and a device
command needs `--firmware`. They have no defaults and the run refuses to be
written without them: they are part of what a verification *is* (`AGENTS.md`
§7), and a report missing its provenance is worse than no report because it
still looks like evidence.

**When something fails, read the failure before re-running.** A failure is a
recorded finding, not an error to retry past. "No notification arrived" and "the
scan saw nothing" mean different things and point at different halves of the
system.

### Getting the evidence back to mimisbrunnr

```bash
rsync -av data/verification/ mimisbrunnr:~/projects/consciousness-lab/data/verification/
rsync -av data/sessions/     mimisbrunnr:~/projects/consciousness-lab/data/sessions/
```

Copying whole directories is not a workaround. `SESSION_FORMAT.md` Q8 makes the
entire `sessions/<id>/` directory the canonical, independently interpretable
unit, with the registry rebuildable by scanning packages — moving a session
between hosts is what the format was built for. Rebuild the index after:

```bash
uv run python -c "from consciousness_lab.session import registry; \
from consciousness_lab.storage.paths import DataRoot; \
from pathlib import Path; registry.rebuild(DataRoot(Path('data')))"
```

### Host readiness — observed, not a device verification

**2026-09-08, mimisbrunnr (Linux 7.0.0-29-generic).** `probe env` reported: a
Bluetooth radio is present and bound to the kernel (Intel `8087:0026`, driver
`btusb`, `hci0` visible), D-Bus is active, and **BlueZ is not installed** — no
`bluetoothd` on disk, no `bluetoothctl`, and no `bluetooth.service`. `brainflow`,
`bleak`, `polar_python` and `pyserial` all import in the locked environment.

Consequence: no BLE device can be reached from this host until BlueZ is
installed, regardless of what the device does. This is a **host** observation.
It verifies nothing about any device, and no row in the table above moves.

---

## Recording a verification

When a device is tested, add a dated subsection below, update its row in the
status table, and log any structural consequence in `DECISIONS.md`. Do not
delete superseded verification entries — a behaviour that changed with a
firmware update is itself a finding.

### Verification log

*(empty — no device has been tested)*
