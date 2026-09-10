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

## The tapping idea, and its one flaw

If the plan is to strike the headband so the impact appears in the Muse's
accelerometer *and* as a mark on this channel, then **the impact and the button
press must be the same physical event**. Press the button and then tap, and the
two are separated by human coordination error — tens of milliseconds, variable,
which is the same order as the BLE uncertainty you were trying to escape.

Mount the switch on the face that strikes, so the impact presses it. Then the
mark and the mechanical shock are one event, within the switch's travel.

A tap on the forehead is well coupled to a device on the forehead. It is **not**
well coupled to a chest strap: the body damps it heavily. Marking both devices
with one strike on the head is an assumption, and it is measurable — do not
build a session protocol on it before `probe concurrent` has shown the transient
in both accelerometer streams.
