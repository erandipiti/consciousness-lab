# QT Py marker channel

A switch that reports, over USB serial, the device-clock time at which its
polling loop **first observed** the input go low. That is the whole device, and
the wording is the claim.

The physical instant the switch closed is **earlier by an unknown amount**. The
loop samples the pin, and how often it does so has never been measured, so the
gap between the edge and the observation of it is unmeasured too. `TIMING.md`
keeps an underlying event and an observation of it as separate quantities; this
device reports the second and cannot report the first.

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
   newer for your exact board from `circuitpython.org/downloads` (press reset
   twice quickly, then drag the `.uf2` onto the `*BOOT` drive).
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
| `B <fw> <board> time_unit=ns resolution=unmeasured` | once at boot | what the host is talking to |
| `M <seq> <device_ns>` | first **observed** high→low transition | a mark |
| `R <token> <device_ns>` | answering `P <token>` | round-trip latency probe |
| `H <seq> <device_ns>` | every 10 s | heartbeat, so drift is measurable |

Host to device: `P <token>\n`.

`time_unit=ns` describes the **representation** of the numbers below — a fact
about the format, which is knowable from here. The board's actual clock
**resolution** is a fact about the board, nobody has measured it, and
`time.monotonic_ns()` returning integer nanoseconds does not establish it: a
coarse tick scaled into nanoseconds is indistinguishable from a fine one at this
end. So it is reported unmeasured rather than encoded as a number.

`device_ns` is this board's own monotonic clock. The host separately records
when the line arrived. **They are different quantities and both are kept** —
`TIMING.md` rule 2: never overwrite a captured quantity with a derived one.

Heartbeats exist so the offset between the two clocks can be watched across a
session and its *drift* measured, rather than an offset taken once at the start
being assumed to hold at the end.

## Where the timestamp is taken, and why it matters

The pin is **read first**, and the clock is sampled **inside the transition**,
after the observation that produced it:

```python
is_down = not button.value  # observe
if is_down and not was_down:
    edge_ns = time.monotonic_ns()  # then time it
    if edge_ns - last_press_ns > DEBOUNCE_MS * 1_000_000:
        ...
```

Two things are being avoided, and only one of them is obvious.

**Debounce after the timestamp, never before.** Debounce first and you have
silently added an unmeasured delay to a device whose entire purpose is knowing
when something happened.

**Sample the clock after the pin, never before.** Reading `monotonic_ns()` and
*then* the GPIO biases every mark EARLY by the pin-read interval. It is small,
but it is systematic rather than noise, and a systematic error is the kind that
survives averaging and never announces itself.

## What is still unknown about this device

Nothing below is a defect. They are the things that have not been measured, and
a number written here before measurement would be invented.

- **The gap between a switch closing and this firmware observing it.** That gap
  is what separates `M` from the physical edge, and it is **unmeasured**.
  CircuitPython gives no user interrupts, so the pin is polled, and how often it
  is actually sampled on your board has never been established.
- **The board's clock resolution.** Unmeasured, and reported as such in the boot
  banner rather than assumed from the fact that the values are printed in
  nanoseconds.

  `probe serial` does **not** establish it. That probe sends a ping and times
  the reply: it exercises the USB path and this loop's *service* latency, and it
  never touches the button or the GPIO. Reading serial round-trip as evidence
  about button detection would be claiming a measured quantity that nobody
  measured — the exact error this project exists to prevent.

  What *would* establish it: driving the input from a source whose own timing is
  known — a signal generator or a second microcontroller pulsing the line — and
  comparing its edge against the `M` line the board emits. **That bench setup
  does not exist**, and nothing here should be read as if the number were known.
- **USB serial round-trip latency and its variability.** This one `probe serial`
  does measure, and it is the gate `docs/HANDOFF.md` puts on CL-007.
- **Whether the board's clock drifts against the host's, and how fast.** The
  heartbeats produce the data; nobody has read it yet.

## What this device cannot tell you

A mark is related to nothing but itself. This firmware reports when its switch
closed, on its own clock, and that is the entire claim.

**How a mark gets related to an EEG or ECG stream is undesigned, and designing it
is gated** — `DECISIONS.md` D42: the marker mechanism, its electrical interface,
and how a mark is placed on a BLE stream's timeline stay gated on a real
measurement existing. None has been taken. The firmware has never been flashed
and `probe serial` has never seen a board.

So this README says nothing about how to use the device to align two streams, and
nothing about what any device would sense if you did. Proposals for that exist
and are recorded as **open questions** in `../../docs/HARDWARE.md` — as questions,
because that is what they are until something has been measured.
