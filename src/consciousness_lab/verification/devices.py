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


#: Feature name fragment -> the polar-python method that starts that stream.
#: Only what the library actually exposes a starter for; anything else is
#: recorded as unstartable rather than silently skipped.
_PMD_STARTERS = {
    "ECG": "start_ecg_stream",
    "ACC": "start_acc_stream",
    "GYRO": "start_gyro_stream",
}


def _settings_to_kwargs(settings: Any, wanted: set[str]) -> tuple[dict[str, int], list[str]]:
    """Read a stream's parameters out of the DEVICE's own settings response.

    Returns (kwargs, unresolved). Choosing a sample rate, resolution or range in
    our code would be inventing a device parameter — the exact thing this
    package exists not to do — so every value comes from what the device
    answered, and anything it did not answer is reported unresolved rather than
    filled in with something plausible.

    The setting objects' attribute names vary by library version, so several are
    tried and whatever is found is used; nothing is assumed about their shape.
    """
    found: dict[str, int] = {}
    for setting in getattr(settings, "settings", None) or []:
        label = str(getattr(setting, "type", "")).upper()
        values: list[Any] = []
        for attribute in ("array_length", "values", "value", "settings"):
            candidate = getattr(setting, attribute, None)
            if isinstance(candidate, (list, tuple)) and candidate:
                values = list(candidate)
                break
            if isinstance(candidate, int):
                values = [candidate]
                break
        if not values:
            continue
        # Case-insensitive: the device labels settings SAMPLE_RATE while the
        # library's parameter is sample_rate, and comparing them as-is silently
        # matches nothing — which reads as "the device did not answer".
        flattened = label.replace("_", "").lower()
        for key in wanted:
            if key.replace("_", "").lower() in flattened:
                found[key] = int(values[0])
    return found, sorted(wanted - set(found))


def _capture_polar_pmd(run: VerificationRun, seconds: float, address: str) -> None:
    """Start every stream the device offers that this library can start.

    Every one, not a chosen subset. Deciding in advance which streams are worth
    recording would require knowing what each is for, and what any of these
    streams is good for is exactly what has not been established — `HARDWARE.md`
    records it as open. A capture that quietly dropped streams on a guess about
    their usefulness would look like it worked while making the discarded ones
    permanently unmeasurable.

    So the rule is the only one available without interpreting anything: **the
    device offers it, therefore it is recorded.** Nothing here decides what any
    stream is, what a value in it means, or what it might later be good for.
    """
    try:
        import asyncio
        import inspect

        from polar_python import PolarDevice
    except Exception as exc:
        run.fail(f"polar_python could not be imported: {type(exc).__name__}: {exc}")
        return

    async def _stream() -> dict[str, Any]:
        arrivals: dict[str, list[float]] = {}
        frames: dict[str, list[dict[str, Any]]] = {}
        features: list[str] = []
        settings_seen: list[dict[str, Any]] = []
        used: dict[str, dict[str, int]] = {}
        problems: list[str] = []

        def make_callback(label: str) -> Any:
            def on_data(data: Any) -> None:
                arrivals.setdefault(label, []).append(time.monotonic())
                payload = getattr(data, "data", None)
                if payload is None:
                    payload = getattr(data, "samples", None)
                frames.setdefault(label, []).append(
                    {
                        "type": type(data).__name__,
                        "device_timestamp": getattr(data, "timestamp", None),
                        "sample_count": len(payload) if payload is not None else None,
                    }
                )

            return on_data

        device = PolarDevice(address)
        await device.connect()
        try:
            available = await device.get_available_features()
            features = [str(f) for f in available]
            for feature in available:
                label = str(feature).upper()
                starter_name = next((m for k, m in _PMD_STARTERS.items() if k in label), None)
                if starter_name is None or not hasattr(device, starter_name):
                    problems.append(
                        f"{label}: the device offers this feature but polar-python exposes "
                        "no way to start it"
                    )
                    continue
                starter = getattr(device, starter_name)
                try:
                    settings = await device.request_stream_settings(feature)
                except Exception as exc:
                    problems.append(
                        f"{label}: settings request failed: {type(exc).__name__}: {exc}"
                    )
                    continue
                settings_seen.append(
                    {
                        "feature": label,
                        "measurement_type": str(getattr(settings, "measurement_type", "")),
                        "settings": [str(s) for s in (getattr(settings, "settings", None) or [])],
                        "error_code": str(getattr(settings, "error_code", "")),
                    }
                )
                # Ask the FUNCTION what it needs, rather than assuming which
                # parameters a stream takes.
                signature = inspect.signature(starter)
                wanted = {
                    name
                    for name, parameter in signature.parameters.items()
                    if name not in ("self",)
                    and parameter.default is inspect.Parameter.empty
                    and "callback" not in name
                }
                kwargs, unresolved = _settings_to_kwargs(settings, wanted)
                if unresolved:
                    problems.append(
                        f"{label}: the device's settings response did not carry "
                        f"{unresolved}; not started, because choosing those values here "
                        "would be inventing a device parameter"
                    )
                    continue
                try:
                    await starter(make_callback(label), **kwargs)
                    used[label] = kwargs
                except Exception as exc:
                    problems.append(f"{label}: start failed: {type(exc).__name__}: {exc}")
            if used:
                await asyncio.sleep(seconds)
        finally:
            with contextlib.suppress(Exception):
                await device.disconnect()
        return {
            "features": features,
            "settings": settings_seen,
            "used": used,
            "arrivals": arrivals,
            "frames": frames,
            "problems": problems,
        }

    try:
        result = asyncio.run(_stream())
    except Exception as exc:
        run.fail(
            f"polar-python PMD session failed: {type(exc).__name__}: {exc} — HARDWARE.md "
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
        "request_stream_settings() per feature; the device's own responses, unmodified",
    )
    run.observe(
        "streams started, and with what parameters",
        result["used"],
        "every parameter taken from the device's settings response, none chosen here",
    )
    for problem in result["problems"]:
        run.fail(problem)
    if not result["used"]:
        run.fail(f"no PMD stream could be started in {seconds:g}s")
        return

    for label in sorted(result["used"]):
        arrivals = result["arrivals"].get(label, [])
        frames = result["frames"].get(label, [])
        if not frames:
            run.fail(f"{label}: the stream started but delivered nothing in {seconds:g}s")
            continue
        run.observe(
            f"{label}: host arrival times of delivered frames",
            describe_series(arrivals),
            "monotonic host clock at each callback; a HOST arrival series, which "
            "TIMING.md distinguishes from when a sample was acquired",
        )
        run.observe(
            f"{label}: shape of each delivered frame",
            frames[:200],
            "type name, the device-supplied timestamp verbatim, and the frame's "
            "sample count; no value is decoded or named",
        )
        device_times = [
            float(f["device_timestamp"])
            for f in frames
            if isinstance(f.get("device_timestamp"), (int, float))
        ]
        if device_times:
            run.observe(
                f"{label}: the timestamp field the frames carried, as a series",
                describe_series(device_times),
                "described only. Whether this is a device clock, a host-assigned "
                "value or something else is NOT decided here",
            )
        counts = [
            float(f["sample_count"]) for f in frames if isinstance(f.get("sample_count"), int)
        ]
        if counts:
            run.observe(
                f"{label}: samples per delivered frame",
                describe_series(counts),
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


def capture_serial(
    run: VerificationRun,
    port: str,
    *,
    seconds: float = DEFAULT_SECONDS,
    pings: int = 200,
    baudrate: int = 115200,
) -> None:
    """Measure the marker channel's round trip, and record what it emits.

    This produces the measurement CL-007 is gated on. Per `DECISIONS.md` D42 the
    gate falls on the marker MECHANISM — how a mark is placed on a stream's
    timeline, and any electrical interface — not on the instrument: firmware
    that answers a ping and this probe that times it are prerequisites FOR the
    measurement, and had to exist before it could be taken at all.

    Latency AND ITS VARIABILITY, because the variability is the point. A mean
    round trip says nothing about whether a given mark is trustworthy, and a
    device whose jitter is unknown is a device whose marks have unknown error
    bars.

    Sends ``P <token>`` and waits for ``R <token> <device_ns>``. The token means
    a late reply cannot be mistaken for a prompt one, which a bare round-trip
    timer would do silently.

    Interprets nothing. It does not decide that the device clock is good, that
    the jitter is acceptable, or that a mark is aligned with anything.
    """
    try:
        import serial as pyserial
    except Exception as exc:
        run.fail(f"pyserial could not be imported: {type(exc).__name__}: {exc}")
        return

    try:
        link = pyserial.Serial(port, baudrate, timeout=0.5)
    except Exception as exc:
        run.fail(f"could not open {port}: {type(exc).__name__}: {exc}")
        return

    round_trips_ns: list[float] = []
    device_times: list[float] = []
    lost = 0
    lines: list[str] = []
    marks: list[dict[str, Any]] = []
    heartbeats: list[dict[str, Any]] = []
    banner: str | None = None

    try:
        deadline = time.monotonic() + seconds
        for index in range(pings):
            if time.monotonic() > deadline:
                break
            token = f"t{index}"
            sent_ns = time.monotonic_ns()
            link.write(f"P {token}\n".encode("ascii"))
            link.flush()
            matched = False
            while time.monotonic_ns() - sent_ns < 500_000_000:
                chunk = link.readline().decode("ascii", "replace").strip()
                if not chunk:
                    continue
                if len(lines) < 200:
                    lines.append(chunk)
                if chunk.startswith("B "):
                    banner = chunk
                elif chunk.startswith("M "):
                    marks.append({"line": chunk, "host_arrival_ns": time.monotonic_ns()})
                elif chunk.startswith("H "):
                    heartbeats.append({"line": chunk, "host_arrival_ns": time.monotonic_ns()})
                elif chunk.startswith(f"R {token} "):
                    round_trips_ns.append(float(time.monotonic_ns() - sent_ns))
                    with contextlib.suppress(ValueError, IndexError):
                        device_times.append(float(chunk.split()[2]))
                    matched = True
                    break
            if not matched:
                # A reply that never came, or came for a different token. Counted
                # rather than quietly excluded: a latency figure computed only
                # over the replies that arrived flatters the link.
                lost += 1
    except Exception as exc:
        run.fail(f"serial exchange failed: {type(exc).__name__}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            link.close()

    run.observe(
        "serial port and settings used",
        {"port": port, "baudrate": baudrate},
        "opened with pyserial; the port name is a host fact, not a device one",
    )
    if banner:
        run.observe(
            "what the device said it is at boot",
            banner,
            "the device's own boot line, verbatim and unparsed",
        )
    if not round_trips_ns:
        run.fail(
            f"no ping was answered on {port} in {seconds:g}s; the device may not be running "
            "the marker firmware, or boot.py may not have enabled the USB data channel"
        )
    else:
        run.observe(
            "round-trip time host->device->host, nanoseconds",
            describe_series(round_trips_ns),
            f"{len(round_trips_ns)} token-matched exchanges of 'P <token>' / 'R <token>'; "
            "the SPREAD is the number CL-007 is gated on, not the mean",
        )
        run.observe(
            "pings that went unanswered",
            {"lost": lost, "attempted": lost + len(round_trips_ns)},
            "counted, because a latency figure over only the replies that arrived "
            "would flatter the link",
        )
    if device_times:
        run.observe(
            "the device clock values carried in the replies, as a series",
            describe_series(device_times),
            "described only; whether this board's clock is usable, and how it drifts "
            "against the host's, is not decided here",
        )
    if marks:
        run.observe(
            "marks that arrived during the probe",
            marks[:50],
            "each device line verbatim beside the host monotonic time it arrived; "
            "the two are kept separate and neither is derived from the other",
        )
    if heartbeats:
        run.observe(
            "heartbeats that arrived during the probe",
            heartbeats[:50],
            "device line and host arrival, so offset over time is readable from the "
            "pair; drift is NOT computed here",
        )
    if lines:
        run.observe("raw lines received", lines, "verbatim, unparsed")


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
