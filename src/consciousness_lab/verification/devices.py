"""Bench captures. Each one connects, watches for a bounded time, and describes.

Every function here is written to be *disappointing*: it names no quantity,
decides no semantics, and returns nothing a caller could mistake for a device
fact. What it produces is a pile of described columns and a list of failure
modes. Turning that into "the Athena exposes a device clock" is a human reading
the report, and `HARDWARE.md` is where that reading gets written down.

These are not adapters. They implement no protocol the recorder consumes, they
produce no `SourcePacket`, and no adapter should be written against them — the
adapter comes *after* these answer what the device provides.

Imports of the device libraries are deliberately local to each function, so a
host with no Bluetooth stack can still run `probe env` and get a report saying
exactly that.
"""

import time
from typing import Any

from consciousness_lab.verification.record import VerificationRun, describe_series

#: How long a bench capture runs when the operator does not say. An engineering
#: constant: long enough to see a counter wrap into a pattern, short enough that
#: nobody walks away. It carries no scientific claim and bounds nothing but
#: this diagnostic (AGENTS.md §6).
DEFAULT_SECONDS = 30.0


def _record_arrival_shape(run: VerificationRun, arrivals: list[float], how: str) -> None:
    """Describe when things showed up on the host, and say only that.

    Host arrival time is the one quantity we always have and the one that says
    least about the device (`TIMING.md`): it includes the radio, the OS, the
    driver and this loop. Recorded because a gap here is real, and labelled so
    nobody later mistakes it for when a sample was acquired.
    """
    run.observe(
        "host arrival times of received blocks (monotonic seconds)",
        describe_series(arrivals),
        how,
    )
    if len(arrivals) > 1:
        span = arrivals[-1] - arrivals[0]
        run.observe(
            "blocks received per second of wall time on the host",
            (len(arrivals) - 1) / span if span > 0 else None,
            "count of received blocks divided by elapsed host monotonic time; a host "
            "rate, NOT the device's sampling rate",
        )


def capture_muse(
    run: VerificationRun, seconds: float = DEFAULT_SECONDS, *, serial_number: str = ""
) -> None:
    """Watch a Muse S Athena through BrainFlow and describe every column it gives.

    Deliberately does not name the channels. `HARDWARE.md` records as ASSUMED
    that the scalp montage is TP9/AF7/AF8/TP10 with an FPz reference and that
    auxiliary channels exist — this run is one of the things that could
    contradict it, so it must not start by agreeing with it.

    Nor does it decide that BrainFlow's timestamp column is device time.
    `HARDWARE.md` says in as many words that a timestamp column does not
    establish where the timestamp came from.
    """
    try:
        from brainflow.board_shim import BoardIds, BoardShim, BrainFlowInputParams
    except Exception as exc:
        run.fail(f"brainflow could not be imported: {type(exc).__name__}: {exc}")
        return

    board_id = int(BoardIds.MUSE_S_BOARD.value)
    for candidate in ("MUSE_S_ATHENA_BOARD", "MUSE_S_BOARD"):
        if hasattr(BoardIds, candidate):
            board_id = int(getattr(BoardIds, candidate).value)
            run.observe(
                "BrainFlow board id used",
                {"name": candidate, "value": board_id},
                "first of MUSE_S_ATHENA_BOARD / MUSE_S_BOARD present in this BrainFlow build",
            )
            break

    params = BrainFlowInputParams()
    if serial_number:
        params.serial_number = serial_number
    board = BoardShim(board_id, params)

    # What the LIBRARY claims about the board, recorded as the library's claim
    # and not as a device fact. It is metadata compiled into BrainFlow; the
    # device is not consulted.
    for label, getter in (
        ("sampling rate", BoardShim.get_sampling_rate),
        ("eeg channel indices", BoardShim.get_eeg_channels),
        ("timestamp channel index", BoardShim.get_timestamp_channel),
        ("package/count channel index", BoardShim.get_package_num_channel),
        ("total channel count", BoardShim.get_num_rows),
    ):
        try:
            run.observe(
                f"BrainFlow's declared {label}",
                getter(board_id),
                "read from BrainFlow's compiled board description; this is the LIBRARY's "
                "claim about the board, not something the device was asked",
            )
        except Exception as exc:
            run.fail(f"BrainFlow could not report {label}: {type(exc).__name__}: {exc}")

    arrivals: list[float] = []
    try:
        board.prepare_session()
    except Exception as exc:
        run.fail(f"prepare_session failed: {type(exc).__name__}: {exc}")
        return
    try:
        board.start_stream()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.25)
            if board.get_board_data_count() > 0:
                arrivals.append(time.monotonic())
        data = board.get_board_data()
    except Exception as exc:
        run.fail(f"streaming failed: {type(exc).__name__}: {exc}")
        return
    finally:
        for step, action in (
            ("stop_stream", board.stop_stream),
            ("release_session", board.release_session),
        ):
            try:
                action()
            except Exception as exc:
                run.fail(f"{step} failed: {type(exc).__name__}: {exc}")

    _record_arrival_shape(run, arrivals, "polled get_board_data_count() every 250 ms")
    run.observe(
        "shape of the array BrainFlow returned",
        {"rows": int(data.shape[0]), "columns": int(data.shape[1])} if data.size else {},
        f"single get_board_data() after {seconds:g}s of streaming",
    )
    # Every row described, none named. Which row is a clock, a counter, a
    # channel or padding is what the report is FOR.
    for index in range(int(data.shape[0]) if data.size else 0):
        run.observe(
            f"row {index} of the BrainFlow array",
            describe_series([float(v) for v in data[index][:20000]]),
            "described as a numeric series; this run does not decide what the row is",
        )


def capture_polar(
    run: VerificationRun, seconds: float = DEFAULT_SECONDS, *, address: str = ""
) -> None:
    """Watch a Polar H10 through polar-python and describe what arrives.

    `HARDWARE.md` records that polar-python is a third-party community library
    whose maintenance status and coverage of the device's streams are
    unverified. A failure here is therefore a finding about the library at
    least as much as about the device, and the two are kept apart in the record.
    """
    try:
        import asyncio

        import polar_python  # noqa: F401  (presence is the observation)
    except Exception as exc:
        run.fail(f"polar_python could not be imported: {type(exc).__name__}: {exc}")
        return

    if not address:
        run.fail(
            "no BLE address given for the Polar H10; discovery is a separate step so that "
            "'nothing was found' and 'the wrong thing was connected' stay distinguishable"
        )
        return

    async def _watch() -> dict[str, Any]:
        from bleak import BleakClient

        arrivals: list[float] = []
        notifications: list[int] = []
        async with BleakClient(address) as client:
            services = [
                {"uuid": str(s.uuid), "characteristics": [str(c.uuid) for c in s.characteristics]}
                for s in client.services
            ]
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                await asyncio.sleep(0.25)
                arrivals.append(time.monotonic())
        return {"services": services, "arrivals": arrivals, "notifications": notifications}

    try:
        result = asyncio.run(_watch())
    except Exception as exc:
        run.fail(f"BLE connection or streaming failed: {type(exc).__name__}: {exc}")
        return

    run.observe(
        "GATT services and characteristics the device advertised",
        result["services"],
        "enumerated from the connected BleakClient; recorded as advertised, not interpreted",
    )
    _record_arrival_shape(run, result["arrivals"], "polled every 250 ms while connected")


def scan_ble(run: VerificationRun, seconds: float = 10.0) -> None:
    """List what the host can see. Answers 'is the stack alive' before anything else.

    Separated from every device capture on purpose. A host with no Bluetooth
    stack and a device that is switched off look identical from inside a library
    that simply times out, and confusing them costs a bench session.
    """
    try:
        import asyncio

        from bleak import BleakScanner
    except Exception as exc:
        run.fail(f"bleak could not be imported: {type(exc).__name__}: {exc}")
        return
    try:
        found = asyncio.run(BleakScanner.discover(timeout=seconds))
    except Exception as exc:
        run.fail(
            f"BLE scan failed: {type(exc).__name__}: {exc} — this is a HOST finding, not a "
            "statement about any device"
        )
        return
    run.observe(
        "devices visible to the host",
        [{"address": d.address, "name": d.name} for d in found],
        f"bleak BleakScanner.discover({seconds:g}s); names are what each device advertised",
    )
