# QT Py marker channel

A switch on a USB serial line. **Every statement about how it behaves lives in
[`../../docs/HARDWARE.md`](../../docs/HARDWARE.md)**, which is the document built
to mark what is verified, what is assumed and what is Unknown. Nothing here
describes behaviour, because nothing here can: no board has been connected.

This file is the wiring, the flashing steps and the wire protocol. That is all.

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

- Switch between the GPIO named in `BUTTON_PIN` and **GND**. No resistor: the
  firmware enables the internal pull-up.
- Which pins your board exposes is a board fact this repository has not verified.
  Read the pinout for the model in your hand. `A0` is only what `code.py` ships
  with; change that one line.

**Nothing connects to a participant, to the Muse, or to the H10, and nothing is
driven out.** There is no conductor between this device and a person, which is
how `../../docs/SAFETY.md` S6 is obeyed — by having no electrical interface,
rather than by choosing a limit. A version that injects into an EEG channel is a
different device and a different decision (`../../docs/DECISIONS.md` D42).

## Flashing

1. Plug the board in. If `CIRCUITPY` does not appear, install CircuitPython for
   your exact board from `circuitpython.org/downloads` (press reset twice
   quickly, then drag the `.uf2` onto the `*BOOT` drive). The firmware needs a
   CircuitPython with `time.monotonic_ns` and f-strings; check what your board
   has rather than assuming.
2. Copy **both** `boot.py` and `code.py` to the root of `CIRCUITPY`.
3. **Power-cycle the board.** `boot.py` runs only at reset.

If `boot.py` did not run, the firmware **acquires nothing** and says so on the
console. Marks are never written to the console: a record sharing a stream with
tracebacks still looks like data.

## Wire protocol

One ASCII line per record, `\n` terminated.

| line | when |
|---|---|
| `B <fw> <board> time_unit=ns resolution=unmeasured` | once at startup |
| `M <seq> <device_ns>` | first observed high→low transition |
| `R <token> <device_ns>` | answering `P <token>` |
| `H <seq> <device_ns>` | periodically |

Host to device: `P <token>\n`.

`time_unit=ns` describes the representation of the numbers. `device_ns` is the
board's own monotonic clock; the host separately records when a line arrived.
The two are different quantities and both are kept
(`../../docs/TIMING.md` rule 2).

A switch already held at startup is not a transition, so it produces no `M`.

## What is known about any of this

Nothing. See [`../../docs/HARDWARE.md`](../../docs/HARDWARE.md).
