"""`consciousness-lab probe` — run a bench capture and write its report.

Every subcommand requires ``--purpose`` and ``--method`` in the operator's own
words, and a device capture also requires ``--firmware``. They have no defaults
and the run refuses to be written without them, because `AGENTS.md` §7 makes
them part of what a verification *is*, and a report that quietly omitted them
would still look like evidence.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from consciousness_lab.verification import devices
from consciousness_lab.verification.record import (
    IncompleteVerificationError,
    VerificationRun,
    data_root_default,
    write_report,
)

app = typer.Typer(
    name="probe",
    help="Record what a device actually does. Produces evidence, never a verdict.",
    no_args_is_help=True,
    add_completion=False,
)


def _device_capture(
    device: str, address: str, seconds: float
) -> tuple[str, Callable[[VerificationRun], None]]:
    """Resolve a device name to its capture. Refuses an unknown one rather than guessing."""
    if device == "muse":
        return "muse-s-athena", lambda run: devices.capture_muse(run, seconds)
    if device == "polar":
        if not address:
            raise typer.BadParameter("--address is required for polar; get it from `probe scan`")
        return "polar-h10", lambda run: devices.capture_polar(run, seconds, address=address)
    raise typer.BadParameter(f"unknown device {device!r}; expected 'muse' or 'polar'")


def _finish(run: VerificationRun, root: Path) -> None:
    try:
        target = write_report(root, run)
    except IncompleteVerificationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"wrote {target}")
    typer.echo(f"  observations: {len(run.observations)}   failures: {len(run.failures)}")
    for failure in run.failures:
        typer.echo(f"  FAILURE: {failure}")
    typer.echo(
        "This is evidence, not a verified fact. To promote a row in docs/HARDWARE.md you "
        "must have connected the unit and observed it yourself (AGENTS.md §7)."
    )


@app.command()
def env(
    purpose: Annotated[str, typer.Option(help="What this run set out to establish.")],
    method: Annotated[
        str, typer.Option()
    ] = "captured the host environment only; no device attached",
    root: Annotated[Path | None, typer.Option(help="Where to write. Defaults to ./data.")] = None,
) -> None:
    """Capture the host itself. Needs no device, and answers 'is this host ready'."""
    run = VerificationRun(subject="host", purpose=purpose, method=method)
    ble = run.environment["ble"]
    run.observe(
        "Bluetooth support present on this host",
        ble,
        "read from /sys/class/bluetooth, the filesystem, and systemctl",
    )
    if not ble["kernel_adapters"]:
        run.fail("no Bluetooth adapter is visible to the kernel")
    if not ble["bluetoothd_present"]:
        run.fail(
            "BlueZ is not installed (no bluetoothd on disk): bleak and BrainFlow cannot "
            "reach a BLE device on this host regardless of what the device does"
        )
    if ble["service_state"] not in ("active",):
        run.fail(f"the bluetooth service is not active (systemctl reports {ble['service_state']})")
    _finish(run, root or data_root_default())


@app.command()
def scan(
    purpose: Annotated[str, typer.Option(help="What this run set out to establish.")],
    seconds: Annotated[float, typer.Option(help="How long to scan.")] = 10.0,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """List the BLE devices the host can see. Do this before blaming a device."""
    run = VerificationRun(
        subject="host",
        purpose=purpose,
        method=f"bleak BLE discovery for {seconds:g}s with no device-specific filter",
    )
    devices.scan_ble(run, seconds)
    _finish(run, root or data_root_default())


@app.command()
def muse(
    purpose: Annotated[str, typer.Option(help="What this run set out to establish.")],
    firmware: Annotated[
        str, typer.Option(help="Firmware or hardware revision, as printed/reported.")
    ],
    method: Annotated[
        str, typer.Option(help="How you ran it: worn, on the bench, electrodes wetted.")
    ],
    alias: Annotated[
        str, typer.Option(help="Study-local alias. Never a serial number.")
    ] = "athena-01",
    serial_number: Annotated[
        str, typer.Option(help="BrainFlow serial_number, if you need to pin one.")
    ] = "",
    seconds: Annotated[float, typer.Option()] = devices.DEFAULT_SECONDS,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Watch a Muse S Athena and describe every column BrainFlow returns."""
    run = VerificationRun(
        subject="muse-s-athena",
        purpose=purpose,
        method=method,
        device_firmware=firmware,
        device_alias=alias,
    )
    devices.capture_muse(run, seconds, serial_number=serial_number)
    _finish(run, root or data_root_default())


@app.command()
def serial(
    purpose: Annotated[str, typer.Option(help="What this run set out to establish.")],
    port: Annotated[str, typer.Option(help="Serial port: /dev/ttyACM0, /dev/cu.usbmodem*, COM3.")],
    firmware: Annotated[str, typer.Option(help="Marker firmware id, e.g. qtpy-marker/1.")],
    method: Annotated[str, typer.Option(help="How you ran it: on the bench, cable, hub.")],
    alias: Annotated[str, typer.Option(help="Study-local alias.")] = "marker-01",
    seconds: Annotated[float, typer.Option()] = devices.DEFAULT_SECONDS,
    pings: Annotated[int, typer.Option(help="How many round trips to attempt.")] = 200,
    baudrate: Annotated[int, typer.Option()] = 115200,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Measure the marker channel's round trip and its SPREAD, and record what it emits.

    This is the gate HANDOFF.md puts on CL-007: the marker mechanism cannot be
    designed until serial round-trip latency and its variability are measured.
    The spread is the number that matters — a mean says nothing about whether a
    given mark can be trusted.

    Needs no BLE and no participant. Bench only.
    """
    run = VerificationRun(
        subject="qtpy-marker",
        purpose=purpose,
        method=method,
        device_firmware=firmware,
        device_alias=alias,
    )
    devices.capture_serial(run, port, seconds=seconds, pings=pings, baudrate=baudrate)
    _finish(run, root or data_root_default())


@app.command()
def reconnect(
    purpose: Annotated[str, typer.Option(help="What this run set out to establish.")],
    firmware: Annotated[str, typer.Option(help="Firmware or hardware revision, as reported.")],
    method: Annotated[str, typer.Option(help="How you ran it.")],
    device: Annotated[str, typer.Option(help="muse or polar.")] = "muse",
    address: Annotated[str, typer.Option(help="BLE address, required for polar.")] = "",
    cycles: Annotated[int, typer.Option(help="How many connect/capture windows.")] = 2,
    gap_seconds: Annotated[float, typer.Option(help="How long the link stays down.")] = 5.0,
    seconds: Annotated[float, typer.Option()] = devices.DEFAULT_SECONDS,
    alias: Annotated[str, typer.Option(help="Study-local alias. Never a serial number.")] = "",
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Capture, drop the link, capture again. Answers what a reconnect does — to a reader.

    The report puts cycle 0 and cycle 1 side by side and computes no difference
    between them. Whether a counter reset or a timebase restarted is what you
    conclude from seeing both series, not what this decides for you.
    """
    subject, capture = _device_capture(device, address, seconds)
    run = VerificationRun(
        subject=subject,
        purpose=purpose,
        method=method,
        device_firmware=firmware,
        device_alias=alias or None,
    )
    devices.capture_reconnect(run, capture, cycles=cycles, gap_seconds=gap_seconds)
    _finish(run, root or data_root_default())


@app.command()
def concurrent(
    purpose: Annotated[str, typer.Option(help="What this run set out to establish.")],
    firmware: Annotated[str, typer.Option(help="Firmware revisions of the devices involved.")],
    method: Annotated[str, typer.Option(help="How you ran it.")],
    polar_address: Annotated[str, typer.Option(help="BLE address of the H10, from `probe scan`.")],
    seconds: Annotated[float, typer.Option()] = devices.DEFAULT_SECONDS,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Hold the Muse and the H10 at once on one adapter, and record what each did.

    HARDWARE.md says two concurrent BLE peripherals on one host adapter are
    unmeasured. Run each device alone first; this report is the third of the
    three you compare, and it does not do the comparing.
    """
    run = VerificationRun(
        subject="muse-s-athena+polar-h10", purpose=purpose, method=method, device_firmware=firmware
    )
    devices.capture_concurrent(
        run,
        {
            "muse-s-athena": lambda child: devices.capture_muse(child, seconds),
            "polar-h10": lambda child: devices.capture_polar(child, seconds, address=polar_address),
        },
    )
    _finish(run, root or data_root_default())


@app.command()
def polar(
    purpose: Annotated[str, typer.Option(help="What this run set out to establish.")],
    firmware: Annotated[str, typer.Option(help="Firmware or hardware revision, as reported.")],
    method: Annotated[str, typer.Option(help="How you ran it: worn, strap wetted, on the bench.")],
    address: Annotated[str, typer.Option(help="BLE address from `probe scan`.")],
    alias: Annotated[
        str, typer.Option(help="Study-local alias. Never a serial number.")
    ] = "h10-01",
    seconds: Annotated[float, typer.Option()] = devices.DEFAULT_SECONDS,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Watch a Polar H10 and describe what it advertises and delivers."""
    run = VerificationRun(
        subject="polar-h10",
        purpose=purpose,
        method=method,
        device_firmware=firmware,
        device_alias=alias,
    )
    devices.capture_polar(run, seconds, address=address)
    _finish(run, root or data_root_default())
