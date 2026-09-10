"""QT Py marker channel — CircuitPython firmware.

A switch on a USB serial line. Copy this and ``boot.py`` to CIRCUITPY and
power-cycle the board; see ``README.md`` for wiring and flashing.

**Every statement about how this behaves lives in ``docs/HARDWARE.md``**, which
is the document built to mark what is verified, what is assumed and what is
Unknown. Nothing is claimed here, because nothing here can be: no board has been
connected.

Wire protocol, one ASCII line per record::

    B <fw> <board> time_unit=ns resolution=unmeasured   at startup
    M <seq> <device_ns>                                 first observed high->low
    R <token> <device_ns>                               answering P <token>
    H <seq> <device_ns>                                 periodically

    host -> device:  P <token>

``time_unit=ns`` describes the representation of the numbers. ``device_ns`` is
this board's own monotonic clock; the host separately records when a line
arrived. Both are kept and neither is derived from the other
(``docs/TIMING.md`` rule 2).

The comments below explain why the code is shaped as it is. They are about this
code, not about any device.
"""

import time

import board
import digitalio
import usb_cdc

# --- things you may need to change for your board -----------------------------

#: The GPIO the switch is wired to, active-low against the internal pull-up.
#: Set it to a free GPIO ON YOUR BOARD. Which pins a given QT Py exposes is a
#: board fact, no board has been connected, and this repository has verified
#: none of them — read the pinout for the model in your hand.
BUTTON_PIN = board.A0

#: Debounce window. The mark is timestamped before this applies, so it changes
#: which edges are reported and never when one is timed. Raise it if a single
#: press reports twice; that is the only reason to touch it.
DEBOUNCE_MS = 25

#: Seconds between heartbeats, or 0 to disable. They exist so the two clocks can
#: be compared over a session; what that comparison shows is not decided here.
HEARTBEAT_SECONDS = 10

FIRMWARE = "qtpy-marker/1"


def _board_name():
    """Whatever the board calls itself, recorded rather than assumed."""
    try:
        import os

        return os.uname().machine.replace(" ", "_")
    except Exception:
        return "unknown"


def main():
    # data=True must be enabled in boot.py; see README.md. The console channel is
    # deliberately not used for marks: a stray traceback must never land in the
    # middle of the data stream.
    serial = usb_cdc.data
    if serial is None:
        # FAIL CLOSED. Emitting the protocol on the console would put records
        # in the same stream as tracebacks, and a record indistinguishable from
        # console noise still looks like data. So nothing is acquired: the
        # console gets one explanation and the board idles.
        if usb_cdc.console is not None:
            usb_cdc.console.write(
                b"FATAL: usb_cdc.data is unavailable, so there is no channel that "
                b"carries marks and nothing else.\r\n"
                b"Copy boot.py to CIRCUITPY and POWER-CYCLE the board; boot.py runs "
                b"only at reset.\r\n"
                b"No marks will be emitted until then.\r\n"
            )
        while True:
            time.sleep(1)

    button = digitalio.DigitalInOut(BUTTON_PIN)
    button.direction = digitalio.Direction.INPUT
    button.pull = digitalio.Pull.UP  # pressed reads False

    def emit(line):
        serial.write(line.encode("ascii"))

    # monotonic_ns() rather than the float monotonic(), which loses precision as
    # uptime grows.
    # `time_unit` describes the numbers this firmware prints. The board's clock
    # resolution is a board fact, reported unmeasured and never inferred from the
    # values being printed in nanoseconds (AGENTS.md §7).
    emit(f"B {FIRMWARE} {_board_name()} time_unit=ns resolution=unmeasured\n")

    press_seq = 0
    beat_seq = 0
    last_press_ns = 0
    last_beat_ns = time.monotonic_ns()
    # Armed from the pin's ACTUAL state, never from a literal. Starting at False
    # means a switch already held at startup satisfies `is_down and not was_down`
    # on the first sample and emits an M for a transition nobody observed.
    was_down = not button.value
    inbox = ""

    while True:
        # READ THE PIN FIRST, then take the clock inside the transition, so a
        # mark cannot precede the observation that produced it. Sampling the
        # clock first biases every mark early by the pin-read interval —
        # systematic, not noise.
        is_down = not button.value

        if is_down and not was_down:
            # The time the transition was OBSERVED, not the time it happened.
            edge_ns = time.monotonic_ns()
            # Debounce decided AFTER the timestamp exists, so the mark stays
            # tied to the first observed transition either way.
            if edge_ns - last_press_ns > DEBOUNCE_MS * 1_000_000:
                last_press_ns = edge_ns
                emit(f"M {press_seq} {edge_ns}\n")
                press_seq += 1
        was_down = is_down

        # Housekeeping clock, read after the edge path so nothing above waits on
        # it. This is NOT the mark's timestamp.
        now = time.monotonic_ns()

        # A host ping, answered immediately so the round trip measures the link
        # and not this loop's other work.
        if serial.in_waiting:
            try:
                inbox += serial.read(serial.in_waiting).decode("ascii")
            except Exception:
                inbox = ""
            while "\n" in inbox:
                line, inbox = inbox.split("\n", 1)
                line = line.strip()
                if line.startswith("P "):
                    emit(f"R {line[2:].strip()} {time.monotonic_ns()}\n")

        if HEARTBEAT_SECONDS and now - last_beat_ns >= HEARTBEAT_SECONDS * 1_000_000_000:
            last_beat_ns = now
            emit(f"H {beat_seq} {now}\n")
            beat_seq += 1

        # No sleep, so the pin is sampled as often as the interpreter allows.
        # How often that is, and everything else about behaviour, is in
        # docs/HARDWARE.md.


main()
