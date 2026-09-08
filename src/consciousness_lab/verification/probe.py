"""`consciousness-lab probe` — run a bench capture and write its report.

Every subcommand requires ``--purpose`` and ``--method`` in the operator's own
words, and a device capture also requires ``--firmware``. They have no defaults
and the run refuses to be written without them, because `AGENTS.md` §7 makes
them part of what a verification *is*, and a report that quietly omitted them
would still look like evidence.
"""

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
