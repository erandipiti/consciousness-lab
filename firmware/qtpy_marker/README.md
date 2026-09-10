# QT Py marker channel

A switch that reports, over USB serial, exactly when it was pressed — on its own
clock. That is the whole device.

**It does not solve alignment.** Placing that instant on the EEG or ECG timeline
is a different, open problem: those streams arrive over BLE with tens to
hundreds of milliseconds of variable latency, and `docs/TIMING.md` calls that
"the hardest timing question in the study". This firmware is one input to it.

---

## Wiring

One momentary switch. Nothing else.

```
   QT Py                       tactile switch
  ┌────────┐
  │    A0  ├───────────────────┤ ├───────────┐
  │        │                                 │
  │   GND  ├─────────────────────────────────┘
  └────────┘
```

- One leg of the switch to **A0**, the other to **GND**.
- No resistor. The firmware enables the internal pull-up, so the pin idles high
  and reads low when pressed.
- Any free GPIO works; `A0` exists on every QT Py variant. Change `BUTTON_PIN`
  in `code.py` if you use another.

### What this wiring deliberately does not touch

Nothing here connects to a participant, to the Muse, or to the H10, and nothing
drives a signal into anything. The isolation is a property of the circuit, not a
promise: there is no path from this device to a person because there is no
conductor between them. `docs/SAFETY.md` S6 forbids inventing electrical limits,
and the way to obey that is to build something with no electrical interface to a
person at all.

**A version that injects a signal into an EEG channel is a different device and a
different decision.** It needs the exact QT Py model and its ADC voltage limits
verified first, and a named human to own the safety question. Do not extend this
one into that without both.

## Flashing

1. Plug the QT Py in. If `CIRCUITPY` does not appear, install CircuitPython 8 or
   newer for your exact board from `circuitpython.org/downloads` (double-tap
   reset, drag the `.uf2` onto the `*BOOT` drive).
2. Copy **both** files to the root of `CIRCUITPY`:
   - `boot.py` — gives the marker stream its own serial channel
   - `code.py` — the firmware
3. **Power-cycle the board.** `boot.py` runs only at reset; a soft reload will
   not enable the data channel, and marks would then share the console with
   tracebacks.

## The protocol

One ASCII line per event, `\n` terminated.

| line | when | meaning |
|---|---|---|
| `B <fw> <board> <ns_per_tick>` | once at boot | what the host is talking to |
| `M <seq> <device_ns>` | button, first contact | a mark |
| `R <token> <device_ns>` | answering `P <token>` | round-trip latency probe |
| `H <seq> <device_ns>` | every 10 s | heartbeat, so drift is measurable |

Host to device: `P <token>\n`.

`device_ns` is this board's own monotonic clock. The host separately records
when the line arrived. **They are different quantities and both are kept** —
`TIMING.md` rule 2: never overwrite a captured quantity with a derived one.

Heartbeats exist so the offset between the two clocks can be watched across a
session and its *drift* measured, rather than an offset taken once at the start
being assumed to hold at the end.

## Where the timestamp is taken, and why it matters

`code.py` timestamps the **first edge**, before debouncing and before writing
anything to serial. Debounce after the timestamp and the mark stays true to
first contact. Debounce before it and you have silently added an unmeasured
delay to a device whose entire purpose is knowing when something happened.

## What is still unknown about this device

Nothing below is a defect. They are the things that have not been measured, and
a number written here before measurement would be invented.

- **Detection jitter of the polling loop.** CircuitPython gives no user
  interrupts, so the switch is polled. How tight that loop actually is on your
  board is a measured number: run `consciousness-lab probe serial`.
- **USB serial round-trip latency and its variability.** Same probe. This is the
  gate `docs/HANDOFF.md` puts on CL-007.
- **Whether the board's clock drifts against the host's, and how fast.** The
  heartbeats produce the data; nobody has read it yet.

## Striking with the button, and what it does and does not give you

The intended use is that **the button is the striking face**: the QT Py is tapped
against the forehead, and the impact is what presses the switch. That matters,
and it is the whole reason the mark is worth anything.

The alternative — press, then tap — separates the two by human coordination
error: tens of milliseconds, variable, the same order as the BLE uncertainty the
marker exists to escape. Mount the switch so the impact presses it and the mark
and the mechanical shock are one event, within the switch's travel.

### The alignment chain, and where it is weakest

| link | how | sharpness |
|---|---|---|
| marker ↔ Muse | strike the headband with the button | an impulse; sharp |
| marker ↔ H10 | strike the strap body with the button | same mechanism; sharp |
| Muse ↔ H10 | a cough | a shared physiological event, but not an impulse |

A **cough** is genuinely sensed by both: it jolts the head and it moves the chest
wall, so it should appear in both accelerometers, and it leaves EMG in the EEG
and motion artefact in the ECG. As a *cross-check* that both devices really did
observe one physical event, it is exactly the right instrument.

As a *ruler*, it is softer than a tap, and it is worth knowing why before
building on it. A cough is not an impulse — it is a few hundred milliseconds
with a build-up, so "the instant of the cough" is ambiguous, and the head jolt
and the chest wall motion are mechanically different events that need not peak
together. Whatever feature you align on carries that ambiguity as error.

Which is why the table has two marker-referenced legs. Striking **both** devices
gives two sharp, independently marker-timestamped links, and the cough then
serves as the independent confirmation that the alignment they imply is real.

None of the above is established. Whether a strike shows cleanly in the Muse's
accelerometer, whether a cough shows in both, and how sharp either transient is,
are things `probe concurrent` records and a human reads. Do not build a session
protocol on any of it first.
