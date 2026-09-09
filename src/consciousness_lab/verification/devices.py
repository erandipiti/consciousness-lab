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

import contextlib
import threading
import time
from collections.abc import Callable
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
    """Watch a Polar H10 and record what it actually delivers, two ways.

    Two paths, because they fail for different reasons and the difference is
    itself evidence:

    * **GATT notifications**, generically. Subscribe to every characteristic the
      device advertises as notifiable and record raw bytes and host arrival
      times. Nothing is decoded, so nothing can be misdecoded. This needs no
      knowledge of Polar's protocol and works for whatever flows unprompted.
    * **PMD streams through polar-python**, for what needs a control-point
      handshake to start at all. The stream parameters come from the device's
      own ``request_stream_settings`` response, never from a number chosen here:
      picking a sample rate would be inventing a device parameter, which is the
      thing this package exists not to do.

    `HARDWARE.md` records polar-python as a third-party community library whose
    coverage and maintenance are unverified, so a failure on the second path is
    a finding about the library at least as much as about the device. The two
    are kept apart in the record for that reason.
    """
    if not address:
        run.fail(
            "no BLE address given for the Polar H10; discovery is a separate step so that "
            "'nothing was found' and 'the wrong thing was connected' stay distinguishable"
        )
        return
    _capture_polar_gatt(run, seconds, address)
    _capture_polar_pmd(run, seconds, address)


def _capture_polar_gatt(run: VerificationRun, seconds: float, address: str) -> None:
    """Raw notifications from every notifiable characteristic. Decodes nothing."""
    try:
        import asyncio

        from bleak import BleakClient
    except Exception as exc:
        run.fail(f"bleak could not be imported: {type(exc).__name__}: {exc}")
        return

    async def _watch() -> dict[str, Any]:
        services: list[dict[str, Any]] = []
        arrivals: dict[str, list[float]] = {}
        payloads: dict[str, list[str]] = {}
        lengths: dict[str, list[float]] = {}
        subscribed: list[str] = []
        subscribe_failures: list[str] = []

        async with BleakClient(address) as client:
            for service in client.services:
                services.append(
                    {
                        "uuid": str(service.uuid),
                        "characteristics": [
                            {"uuid": str(c.uuid), "properties": list(c.properties)}
                            for c in service.characteristics
                        ],
                    }
                )

            def on_data(sender: Any, data: bytearray) -> None:
                key = str(getattr(sender, "uuid", sender))
                arrivals.setdefault(key, []).append(time.monotonic())
                lengths.setdefault(key, []).append(float(len(data)))
                # A bounded sample of raw bytes, hex, undecoded. Enough for a
                # human to see the frame shape; not enough to be a data path.
                if len(payloads.setdefault(key, [])) < 8:
                    payloads[key].append(bytes(data).hex())

            for service in client.services:
                for characteristic in service.characteristics:
                    if "notify" not in characteristic.properties:
                        continue
                    try:
                        await client.start_notify(characteristic, on_data)
                        subscribed.append(str(characteristic.uuid))
                    except Exception as exc:
                        subscribe_failures.append(
                            f"{characteristic.uuid}: {type(exc).__name__}: {exc}"
                        )
            await asyncio.sleep(seconds)
            for uuid in subscribed:
                try:
                    await client.stop_notify(uuid)
                except Exception as exc:
                    subscribe_failures.append(f"stop_notify {uuid}: {type(exc).__name__}: {exc}")

        return {
            "services": services,
            "arrivals": arrivals,
            "payloads": payloads,
            "lengths": lengths,
            "subscribed": subscribed,
            "failures": subscribe_failures,
        }

    try:
        result = asyncio.run(_watch())
    except Exception as exc:
        run.fail(f"GATT connection or notification capture failed: {type(exc).__name__}: {exc}")
        return

    run.observe(
        "GATT services and characteristics the device advertised",
        result["services"],
        "enumerated from the connected BleakClient; recorded as advertised, not interpreted",
    )
    run.observe(
        "characteristics this run subscribed to",
        result["subscribed"],
        "every characteristic whose advertised properties included notify",
    )
    for failure in result["failures"]:
        run.fail(f"notification subscription: {failure}")
    if not result["arrivals"]:
        run.fail(
            f"no notification arrived on any subscribed characteristic in {seconds:g}s; "
            "some Polar streams do not start without a control-point handshake, which is "
            "what the polar-python path below attempts"
        )
    for uuid, arrivals in sorted(result["arrivals"].items()):
        run.observe(
            f"host arrival times of notifications on {uuid}",
            describe_series(arrivals),
            "monotonic host clock at the bleak callback; a HOST arrival series, which "
            "TIMING.md distinguishes from when a sample was acquired",
        )
        run.observe(
            f"notification payload lengths on {uuid}",
            describe_series(result["lengths"][uuid]),
            "byte length of each notification; the frame is not decoded",
        )
        run.observe(
            f"first raw notification payloads on {uuid} (hex)",
            result["payloads"][uuid],
            "undecoded bytes, so nothing can be misdecoded; a human reads the frame shape",
        )


def _capture_polar_pmd(run: VerificationRun, seconds: float, address: str) -> None:
    """The streams that need a control-point handshake, via polar-python."""
    try:
        import asyncio

        from polar_python import PolarDevice
    except Exception as exc:
        run.fail(f"polar_python could not be imported: {type(exc).__name__}: {exc}")
        return

    async def _stream() -> dict[str, Any]:
        received: list[dict[str, Any]] = []
        arrivals: list[float] = []
        features: list[str] = []
        settings_seen: list[dict[str, Any]] = []
        used: dict[str, Any] = {}

        device = PolarDevice(address)
        await device.connect()
        try:
            available = await device.get_available_features()
            features = [str(f) for f in available]
            ecg = next((f for f in available if "ECG" in str(f).upper()), None)
            if ecg is None:
                return {
                    "features": features,
                    "settings": settings_seen,
                    "received": received,
                    "arrivals": arrivals,
                    "used": used,
                }

            settings = await device.request_stream_settings(ecg)
            settings_seen.append(
                {
                    "measurement_type": str(settings.measurement_type),
                    "settings": [str(s) for s in (settings.settings or [])],
                    "error_code": str(settings.error_code),
                }
            )

            # Parameters come from the DEVICE's settings response. Choosing a
            # sample rate here would be inventing a device parameter.
            chosen: dict[str, int] = {}
            for setting in settings.settings or []:
                name = str(getattr(setting, "type", "")).upper()
                values = list(
                    getattr(setting, "array_length", []) or getattr(setting, "values", []) or []
                )
                if values:
                    if "SAMPLE" in name and "RATE" in name:
                        chosen["sample_rate"] = int(values[0])
                    elif "RESOLUTION" in name:
                        chosen["resolution"] = int(values[0])
            if "sample_rate" not in chosen or "resolution" not in chosen:
                raise RuntimeError(
                    "the device's stream-settings response did not carry both a sample rate "
                    f"and a resolution this run could read: {settings_seen[-1]}"
                )
            used = dict(chosen)

            def on_ecg(data: Any) -> None:
                arrivals.append(time.monotonic())
                payload = getattr(data, "data", None)
                received.append(
                    {
                        "type": type(data).__name__,
                        "device_timestamp": getattr(data, "timestamp", None),
                        "sample_count": len(payload) if payload is not None else None,
                    }
                )

            await device.start_ecg_stream(on_ecg, **chosen)
            await asyncio.sleep(seconds)
        finally:
            with contextlib.suppress(Exception):
                await device.disconnect()
        return {
            "features": features,
            "settings": settings_seen,
            "received": received,
            "arrivals": arrivals,
            "used": used,
        }

    try:
        result = asyncio.run(_stream())
    except Exception as exc:
        run.fail(
            f"polar-python PMD stream failed: {type(exc).__name__}: {exc} — HARDWARE.md "
            "records this library's coverage as unverified, so this is a finding about the "
            "library at least as much as about the device"
        )
        return

    run.observe(
        "features polar-python reported the device offers",
        result["features"],
        "get_available_features(); the LIBRARY's report of the device's capabilities",
    )
    run.observe(
        "stream settings the device returned",
        result["settings"],
        "request_stream_settings(); the device's own response, unmodified",
    )
    if result["used"]:
        run.observe(
            "stream parameters this run used",
            result["used"],
            "taken from the device's settings response, not chosen here",
        )
    if not result["received"]:
        run.fail(f"the PMD stream started but delivered nothing in {seconds:g}s")
        return
    _record_arrival_shape(run, result["arrivals"], "host monotonic clock at each callback")
    run.observe(
        "shape of each delivered frame",
        result["received"][:200],
        "type name, the device-supplied timestamp field verbatim, and how many "
        "samples the frame carried; no value is decoded or named",
    )
    device_times = [
        float(f["device_timestamp"])
        for f in result["received"]
        if isinstance(f.get("device_timestamp"), (int, float))
    ]
    if device_times:
        run.observe(
            "the timestamp field the frames carried, as a series",
            describe_series(device_times),
            "described only. Whether this is a device clock, a host-assigned value or "
            "something else is NOT decided here (HARDWARE.md: a library producing a "
            "timestamp does not establish where it came from)",
        )
    sample_counts = [
        float(f["sample_count"])
        for f in result["received"]
        if isinstance(f.get("sample_count"), int)
    ]
    if sample_counts:
        run.observe(
            "samples per delivered frame",
            describe_series(sample_counts),
            "length of each frame's payload; not converted to a rate here",
        )


def capture_reconnect(
    run: VerificationRun,
    capture: Callable[[VerificationRun], None],
    *,
    cycles: int = 2,
    gap_seconds: float = 5.0,
) -> None:
    """Capture, drop the link, capture again — and put the windows side by side.

    One of `HARDWARE.md`'s open questions is what a reconnect does to counters
    and to any device-side time base. It cannot be answered without two windows
    around a real disconnection, and it must not be answered BY this code: the
    probe records cycle 0 and cycle 1 under prefixed names and computes no
    difference between them. "The counter reset" is the conclusion a human draws
    from seeing both series; a wrong one baked in here would be invisible.

    Disconnection is by ending the capture's own connection and waiting: each
    cycle opens and closes its own link, which is what an operator's reconnect
    actually looks like.
    """
    for cycle in range(cycles):
        if cycle:
            run.observe(
                f"gap before cycle {cycle}",
                {"seconds": gap_seconds},
                "link fully closed and left down for this long before reconnecting",
            )
            time.sleep(gap_seconds)
        child = VerificationRun(
            subject=run.subject,
            purpose=f"reconnect cycle {cycle}",
            method=run.method,
            device_firmware=run.device_firmware,
        )
        capture(child)
        run.absorb(child, f"cycle {cycle}")


def capture_concurrent(
    run: VerificationRun,
    captures: dict[str, Callable[[VerificationRun], None]],
) -> None:
    """Hold several peripherals at once and record what each one did.

    `HARDWARE.md` says concurrent connections to two BLE peripherals from one
    host adapter are unmeasured and that nothing should be assumed about
    co-existence. This runs the captures at the same time on separate threads
    and folds each one's observations in under its own name.

    It deliberately does NOT compare against a solo run. Whether one adapter
    sustains both is answered by an operator running each device alone and then
    both, and reading the three reports — not by this function deciding that
    something degraded.
    """
    children = {
        name: VerificationRun(
            subject=name,
            purpose=f"concurrent capture: {name}",
            method=run.method,
            device_firmware=run.device_firmware,
        )
        for name in captures
    }
    threads = []
    started = time.monotonic()
    for name, capture in captures.items():
        thread = threading.Thread(
            target=_guarded, args=(children[name], capture), name=f"cl004-{name}", daemon=True
        )
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    run.observe(
        "peripherals held at the same time",
        {"names": sorted(captures), "wall_seconds": round(time.monotonic() - started, 3)},
        "each capture ran on its own thread against the same host adapter, started together",
    )
    for name, child in children.items():
        run.absorb(child, name)


def _guarded(child: VerificationRun, capture: Callable[[VerificationRun], None]) -> None:
    """Run one capture so a thrown exception becomes a recorded failure."""
    try:
        capture(child)
    except Exception as exc:
        child.fail(f"capture raised on its thread: {type(exc).__name__}: {exc}")


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
