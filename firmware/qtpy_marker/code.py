"""QT Py marker channel — CircuitPython firmware.

Drop this on the CIRCUITPY drive as ``code.py``. It needs no libraries beyond
what CircuitPython ships with. Written for CircuitPython 8 or newer (it uses
f-strings and ``time.monotonic_ns``); current QT Py boards ship 9.x.

WHAT THIS DEVICE IS
    A switch that says, over USB serial, exactly when it was pressed, on its own
    clock. That is all. It is precise about itself.

WHAT IT IS NOT
    A solution to placing that instant on the EEG or ECG timeline. Those arrive
    over BLE with tens to hundreds of milliseconds of variable, unmeasured
    latency, and `docs/TIMING.md` calls aligning them "the hardest timing
    question in the study", still open. This firmware produces one input to that
    problem; it does not answer it, and nothing here should be read as if it did.

WHY THE TIMESTAMP IS TAKEN WHERE IT IS
    ``t = monotonic_ns()`` runs on the FIRST edge, before debouncing and before
    anything is written to serial. Debounce after the timestamp and the mark
    stays true to first contact; debounce before it and you have silently added
    an unmeasured delay to a device whose entire purpose is knowing when.

THE PROTOCOL, one ASCII line per event, ``\\n`` terminated:

    B <fw> <board> <ns_per_tick>     once at boot: what the host is talking to
    M <seq> <device_ns>              a press, at first contact
    R <token> <device_ns>            reply to a host ping; for round-trip latency
    H <seq> <device_ns>              periodic heartbeat; makes drift measurable

    Host -> device:  P <token>       ping

    Device time and host arrival time are DIFFERENT QUANTITIES and both are
    kept. `TIMING.md` rule 2: never overwrite a captured quantity with a derived
    one. The host records when a line arrived; this device records when the
    thing happened by its own clock; nobody collapses them.

NO ELECTRICAL CONTACT WITH A PARTICIPANT
    A switch, a GPIO and ground. Nothing here connects to a person, to the Muse
    or to the H10, and nothing drives a signal into anything. That isolation is
    a property of the wiring, not a promise in a comment — see README.md.
"""

import time

import board
import digitalio
import usb_cdc

# --- things you may need to change for your board -----------------------------

#: The GPIO the switch is wired to, active-low against the internal pull-up.
#: A0 exists on every QT Py variant (SAMD21, RP2040, ESP32-S2/S3). Any free
#: GPIO works; change this one line.
BUTTON_PIN = board.A0

#: Ignore further edges for this long after a press. A mechanical switch bounces
#: for a few milliseconds; this is a debounce window, NOT a timing parameter —
#: the mark was already timestamped before this applies. Raise it if one press
#: reports twice; that is the only reason to touch it.
DEBOUNCE_MS = 25

#: Seconds between heartbeats, or 0 to disable. Heartbeats exist so that the
#: offset between this device's clock and the host's can be watched over a
#: session and its DRIFT measured rather than assumed constant (`TIMING.md`).
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
        # Fall back so a misconfigured board still says something rather than
        # appearing dead. The README explains how to enable the data channel.
        serial = usb_cdc.console

    button = digitalio.DigitalInOut(BUTTON_PIN)
    button.direction = digitalio.Direction.INPUT
    button.pull = digitalio.Pull.UP  # pressed reads False

    def emit(line):
        serial.write(line.encode("ascii"))

    # monotonic_ns() is the only clock here with usable resolution; the float
    # monotonic() loses precision as uptime grows, which is exactly wrong for a
    # device that exists to report a moment.
    emit(f"B {FIRMWARE} {_board_name()} 1\n")

    press_seq = 0
    beat_seq = 0
    last_press_ns = 0
    last_beat_ns = time.monotonic_ns()
    was_down = False
    inbox = ""

    while True:
        # READ THE PIN FIRST. The timestamp is taken inside the transition, after
        # the observation that produced it, so it cannot precede the thing it
        # claims to time. Sampling the clock before the pin biases every mark
        # EARLY by the pin-read interval — small, but systematic, and systematic
        # error in a device whose only job is knowing when is the worst kind.
        is_down = not button.value

        if is_down and not was_down:
            edge_ns = time.monotonic_ns()
            # Debounce decided AFTER the timestamp exists, so the mark stays
            # true to first contact either way.
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
        #
        # HOW OFTEN THAT IS, IS UNMEASURED. `probe serial` does NOT establish it:
        # that probe sends a ping and times the reply, which exercises the USB
        # path and this loop's SERVICE latency, and never touches the button or
        # the GPIO at all. Treating serial round-trip as evidence about button
        # detection would be claiming a measured quantity that nobody measured —
        # see README.md for what would actually establish it.


main()
