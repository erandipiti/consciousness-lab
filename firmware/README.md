# firmware/

Microcontroller firmware for the QT Py marker device.

**Empty at CL-001.** No firmware has been written and the marker mechanism is
undesigned — see [`../docs/HARDWARE.md`](../docs/HARDWARE.md).

Firmware is built and flashed independently of the Python package. The open
questions that block work here are the marker semantics
(`../docs/SESSION_FORMAT.md` Q5), how a marker is placed on a common timeline
with the BLE device streams (`../docs/TIMING.md`), and whether the hardware
makes any electrical contact with the participant (`../docs/SAFETY.md`).
