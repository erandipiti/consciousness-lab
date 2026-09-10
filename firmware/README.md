# firmware/

Microcontroller firmware for the QT Py marker device.

**One firmware exists: [`qtpy_marker/`](qtpy_marker/), added at CL-007-A.** It has
**never been flashed to a board**, so nothing about its behaviour has been
observed. The marker *mechanism* — how a mark is placed on a common timeline with
the BLE streams — remains undesigned and gated (`../docs/DECISIONS.md` D42).

Code existing is not a device behaving; see
[`../docs/HARDWARE.md`](../docs/HARDWARE.md), where the QT Py row still reads
*Pending verification*.

Firmware is built and flashed independently of the Python package. The open
questions that block work here are the marker semantics
(`../docs/SESSION_FORMAT.md` Q5), how a marker is placed on a common timeline
with the BLE device streams (`../docs/TIMING.md`), and whether the hardware
makes any electrical contact with the participant (`../docs/SAFETY.md`).
