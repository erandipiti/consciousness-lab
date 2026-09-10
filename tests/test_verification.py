"""CL-004: the verification harness records evidence and refuses to conclude.

Nothing here touches a device or a Bluetooth stack. The one test that would is
marked `hardware` and excluded from the default run (`AGENTS.md` §8), and a test
in this file asserts that exclusion is real rather than assumed.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

import pytest
from typer.testing import CliRunner

from consciousness_lab.cli import app
from consciousness_lab.verification.record import (
    IncompleteVerificationError,
    Observation,
    VerificationRun,
    ble_stack,
    describe_series,
    host_environment,
    library_versions,
    report_path,
    write_report,
)

runner = CliRunner()


def a_run(**overrides: object) -> VerificationRun:
    fields: dict[str, object] = {
        "subject": "muse-s-athena",
        "purpose": "establish which columns the board returns",
        "method": "worn, dry electrodes, 30s bench capture",
        "device_firmware": "1.2.3",
        "device_alias": "athena-01",
    }
    fields.update(overrides)
    run = VerificationRun(**fields)  # type: ignore[arg-type]
    run.observe("something", 1, "how it was seen")
    return run


# ------------------------------------------------- AGENTS.md §7 is structural


def test_a_run_without_its_provenance_cannot_be_written(tmp_path: Path) -> None:
    """A report missing its provenance is worse than none: it looks like evidence."""
    for missing, value in (("purpose", ""), ("method", "   "), ("device_firmware", None)):
        run = a_run(**{missing: value})
        with pytest.raises(IncompleteVerificationError, match=missing.split("_")[-1]):
            write_report(tmp_path, run)
    assert not list(tmp_path.rglob("*.json")), "nothing may reach disk before validation"


def test_a_host_run_needs_no_firmware_because_there_is_no_device(tmp_path: Path) -> None:
    run = VerificationRun(subject="host", purpose="is this host ready", method="read the host")
    run.observe("bluetooth support", {"kernel_adapters": []}, "read /sys/class/bluetooth")
    assert write_report(tmp_path, run).is_file()


def test_a_run_that_saw_nothing_and_failed_at_nothing_is_refused(tmp_path: Path) -> None:
    """Silence is not a result. It records that nobody looked."""
    run = VerificationRun(subject="host", purpose="p", method="m")
    with pytest.raises(IncompleteVerificationError, match="observations or failures"):
        write_report(tmp_path, run)
    run.fail("the device never advertised")
    assert write_report(tmp_path, run).is_file()


def test_the_report_carries_every_field_section_7_requires(tmp_path: Path) -> None:
    run = a_run()
    run.fail("stream stopped after 12s")
    document = json.loads(write_report(tmp_path, run).read_text(encoding="utf-8"))

    assert document["device_firmware"] == "1.2.3"
    assert document["purpose"] and document["method"]
    assert document["started_at_utc"] and document["finished_at_utc"]
    assert document["environment"]["os"] and document["environment"]["python"]
    assert document["environment"]["libraries"], "which library version is a §7 field"
    assert document["failures"] == ["stream stopped after 12s"], "failure modes are required"


def test_the_report_never_claims_to_be_a_verification(tmp_path: Path) -> None:
    document = json.loads(write_report(tmp_path, a_run()).read_text(encoding="utf-8"))
    assert document["verified"] is False
    assert "AGENTS.md §7" in document["note"]
    assert "not study data" in document["note"]


def test_an_observation_has_nowhere_to_put_a_conclusion() -> None:
    """The type itself refuses one: `what`, `value`, `how` — no `means`.

    This is the guardrail the whole package rests on. HARDWARE.md warns that
    BrainFlow producing a timestamp column does not establish that the timestamp
    came from the device; if an observation could carry an interpretation, that
    is exactly where the unverifiable claim would enter the record.
    """
    assert set(Observation.__dataclass_fields__) == {"what", "value", "how"}


# --------------------------------------------- verification is not study data


def test_a_report_never_lands_among_session_packages(tmp_path: Path) -> None:
    """Wearing a device to see whether it streams is not a recording session."""
    run = a_run()
    target = report_path(tmp_path, run)
    assert "sessions" not in target.parts
    assert target.parent.name == "verification"

    sessions_root = tmp_path / "data" / "sessions"
    run_into_sessions = a_run(subject="sessions")
    with pytest.raises(IncompleteVerificationError, match="never land"):
        write_report(sessions_root, run_into_sessions)


def test_writing_a_report_creates_no_session_package(tmp_path: Path) -> None:
    write_report(tmp_path, a_run())
    assert not (tmp_path / "sessions").exists()
    assert not list(tmp_path.rglob("manifest.json"))
    assert not list(tmp_path.rglob("lifecycle.jsonl"))


# -------------------------------------------------- describing without naming


def test_a_series_is_described_and_never_named() -> None:
    described = describe_series([10.0, 11.0, 12.0, 13.0])
    assert described["strictly_increasing"] is True
    assert described["n"] == 4
    # Nothing in the description says clock, counter, timestamp or sample.
    assert not {"clock", "counter", "timestamp", "device_time"} & set(described)


def test_a_series_that_goes_backwards_says_where() -> None:
    described = describe_series([0.0, 1.0, 2.0, 1.5, 3.0])
    assert described["non_decreasing"] is False
    assert described["went_backwards_at"] == [2]
    assert described["went_backwards_total"] == 1


def test_a_constant_series_is_not_mistaken_for_increasing() -> None:
    described = describe_series([7.0, 7.0, 7.0])
    assert described["non_decreasing"] is True
    assert described["strictly_increasing"] is False
    assert described["distinct_step_count"] == 1


def test_an_empty_series_reports_emptiness_rather_than_guessing() -> None:
    assert describe_series([]) == {"n": 0}


# --------------------------------------------------------- host facts, no crash


def test_the_host_environment_reads_without_a_bluetooth_stack() -> None:
    """A host with nothing installed must still produce a report saying so."""
    environment = host_environment()
    assert environment["os"] and environment["python"]
    assert set(environment["ble"]) >= {"kernel_adapters", "bluetoothd_present", "service_state"}


def test_a_library_that_will_not_import_is_a_finding_not_a_crash() -> None:
    versions = library_versions()
    assert versions, "the tracked libraries are a §7 field and must always be reported"
    assert all(isinstance(v, str) for v in versions.values())


def test_the_ble_stack_probe_never_raises() -> None:
    assert isinstance(ble_stack()["kernel_adapters"], list)


# ------------------------------------------------------------------- operator


def test_probe_env_writes_a_report_and_says_it_is_not_a_verdict(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["probe", "env", "--purpose", "is this host ready", "--root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.stdout
    written = list((tmp_path / "verification").glob("*.json"))
    assert len(written) == 1
    assert "evidence, not a verified fact" in result.stdout


def test_probe_refuses_a_device_run_with_no_purpose(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["probe", "muse", "--firmware", "1.0", "--method", "worn", "--root", str(tmp_path)]
    )
    assert result.exit_code != 0
    assert not list(tmp_path.rglob("*.json"))


def test_the_probe_group_is_wired_into_the_console_script() -> None:
    result = runner.invoke(app, ["probe", "--help"])
    assert result.exit_code == 0
    for command in ("env", "scan", "muse", "polar"):
        assert command in result.stdout


# ------------------------------- CL-004-R1: the measurement surface is complete


def test_reconnect_puts_both_windows_side_by_side_and_compares_nothing() -> None:
    """The open question is what a reconnect does. The probe must not answer it.

    Cycle 0 and cycle 1 are recorded under prefixed names and no difference
    between them is computed. "The counter reset on reconnect" is a conclusion a
    human draws from seeing both series; baked in here it would be invisible and
    unfalsifiable.
    """
    from consciousness_lab.verification import devices

    seen: list[float] = []

    def fake_capture(child: VerificationRun) -> None:
        # Both windows start from zero — a counter that restarted, which is the
        # very thing a reader is meant to notice and the probe must stay silent
        # about.
        seen.append(0.0)
        child.observe("row 0", describe_series([0.0, 1.0, 2.0]), "fake")

    run = VerificationRun(subject="muse-s-athena", purpose="p", method="m", device_firmware="1")
    devices.capture_reconnect(run, fake_capture, cycles=2, gap_seconds=0.0)

    labels = [o.what for o in run.observations]
    assert "[cycle 0] row 0" in labels
    assert "[cycle 1] row 0" in labels
    assert any("gap before cycle 1" in label for label in labels)
    # Nothing that reads as a verdict about the reconnect.
    joined = " ".join(labels).lower()
    assert "reset" not in joined and "changed" not in joined and "same" not in joined


def test_a_failing_cycle_is_recorded_rather_than_ending_the_run() -> None:
    from consciousness_lab.verification import devices

    calls: list[int] = []

    def flaky(child: VerificationRun) -> None:
        calls.append(1)
        if len(calls) == 1:
            child.fail("the device never advertised")
        else:
            child.observe("row 0", describe_series([1.0, 2.0]), "fake")

    run = VerificationRun(subject="polar-h10", purpose="p", method="m", device_firmware="1")
    devices.capture_reconnect(run, flaky, cycles=2, gap_seconds=0.0)
    assert run.failures == ["[cycle 0] the device never advertised"]
    assert any(o.what == "[cycle 1] row 0" for o in run.observations)


def test_concurrent_holds_both_and_names_each_without_judging_either() -> None:
    """HARDWARE.md calls two peripherals on one adapter unmeasured. This measures.

    It records what each device did while the other was connected, and does NOT
    compare against a solo run — that comparison is the operator reading three
    reports.
    """
    from consciousness_lab.verification import devices

    def good(child: VerificationRun) -> None:
        child.observe("arrivals", describe_series([0.0, 1.0, 2.0]), "fake")

    def broken(child: VerificationRun) -> None:
        raise RuntimeError("adapter refused the second connection")

    run = VerificationRun(subject="both", purpose="p", method="m", device_firmware="1")
    devices.capture_concurrent(run, {"muse-s-athena": good, "polar-h10": broken})

    labels = [o.what for o in run.observations]
    assert "[muse-s-athena] arrivals" in labels
    assert any("peripherals held at the same time" in label for label in labels)
    # A thread that blew up becomes a recorded failure, not a lost capture.
    assert any("polar-h10" in f and "adapter refused" in f for f in run.failures)
    assert any("degraded" not in o.what.lower() for o in run.observations)


def test_absorb_carries_failures_and_never_summarises_a_phase() -> None:
    parent = VerificationRun(subject="host", purpose="p", method="m")
    child = VerificationRun(subject="host", purpose="c", method="m")
    child.observe("a series", {"n": 3}, "fake")
    child.fail("something went wrong")
    parent.absorb(child, "phase 1")

    assert [o.what for o in parent.observations] == ["[phase 1] a series"]
    assert parent.observations[0].value == {"n": 3}, "the value is carried through untouched"
    assert parent.failures == ["[phase 1] something went wrong"]


def test_the_polar_capture_refuses_without_an_address() -> None:
    """'Nothing was found' and 'the wrong thing was connected' must stay distinct."""
    from consciousness_lab.verification import devices

    run = VerificationRun(subject="polar-h10", purpose="p", method="m", device_firmware="1")
    devices.capture_polar(run, 1.0, address="")
    assert run.observations == []
    assert any("discovery is a separate step" in f for f in run.failures)


def test_the_polar_capture_subscribes_to_every_notifiable_characteristic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The GATT path must actually stream, not just enumerate and poll a clock.

    Regression for the CL-004 review finding: the first version connected,
    listed services, and appended host times without ever subscribing to
    anything, so it could produce no evidence about delivered data at all.
    """
    from consciousness_lab.verification import devices

    subscribed: list[str] = []

    class FakeCharacteristic:
        def __init__(self, uuid: str, properties: list[str]) -> None:
            self.uuid, self.properties = uuid, properties

    class FakeService:
        uuid = "0000180d-0000-1000-8000-00805f9b34fb"
        characteristics: ClassVar[list[FakeCharacteristic]] = [
            FakeCharacteristic("00002a37-0000-1000-8000-00805f9b34fb", ["notify"]),
            FakeCharacteristic("00002a38-0000-1000-8000-00805f9b34fb", ["read"]),
        ]

    class FakeClient:
        def __init__(self, address: str) -> None:
            self.services = [FakeService()]

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def start_notify(
            self, characteristic: FakeCharacteristic, callback: Callable[..., None]
        ) -> None:
            subscribed.append(characteristic.uuid)
            callback(characteristic, bytearray(b"\x10\x50\x0a"))
            callback(characteristic, bytearray(b"\x10\x51\x0b"))

        async def stop_notify(self, uuid: object) -> None:
            return None

    import bleak

    monkeypatch.setattr(bleak, "BleakClient", FakeClient)
    run = VerificationRun(subject="polar-h10", purpose="p", method="m", device_firmware="1")
    devices._capture_polar_gatt(run, 0.01, "AA:BB:CC:DD:EE:FF")

    assert subscribed == ["00002a37-0000-1000-8000-00805f9b34fb"], "notify only, not read"
    labels = [o.what for o in run.observations]
    assert any("host arrival times of notifications" in label for label in labels)
    assert any("payload lengths" in label for label in labels)
    raw = next(o for o in run.observations if "raw notification payloads" in o.what)
    assert raw.value == ["10500a", "10510b"], "raw bytes, undecoded"
    assert not any("heart" in label.lower() or "bpm" in label.lower() for label in labels)


def test_silence_on_every_characteristic_is_recorded_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from consciousness_lab.verification import devices

    class Silent:
        uuid = "svc"
        characteristics: ClassVar[list[object]] = []

    class FakeClient:
        def __init__(self, address: str) -> None:
            self.services = [Silent()]

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    import bleak

    monkeypatch.setattr(bleak, "BleakClient", FakeClient)
    run = VerificationRun(subject="polar-h10", purpose="p", method="m", device_firmware="1")
    devices._capture_polar_gatt(run, 0.01, "AA:BB:CC:DD:EE:FF")
    assert any("no notification arrived" in f for f in run.failures)
    assert any("control-point handshake" in f for f in run.failures)


def test_the_reconnect_and_concurrent_commands_are_wired(tmp_path: Path) -> None:
    result = runner.invoke(app, ["probe", "--help"])
    assert result.exit_code == 0
    for command in ("reconnect", "concurrent"):
        assert command in result.stdout
    # An unknown device name is refused rather than silently defaulted.
    bad = runner.invoke(
        app,
        [
            "probe",
            "reconnect",
            "--purpose",
            "p",
            "--firmware",
            "1",
            "--method",
            "m",
            "--device",
            "eeg-cap",
            "--root",
            str(tmp_path),
        ],
    )
    assert bad.exit_code != 0
    assert not list(tmp_path.rglob("*.json"))


def test_polar_reconnect_requires_an_address(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "probe",
            "reconnect",
            "--purpose",
            "p",
            "--firmware",
            "1",
            "--method",
            "m",
            "--device",
            "polar",
            "--root",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    assert not list(tmp_path.rglob("*.json"))


# --------------------- CL-007-A-R1: every stream the device offers, not just ECG


class FakeSetting:
    def __init__(self, kind: str, values: list[int]) -> None:
        self.type, self.array_length = kind, values


class FakeSettings:
    def __init__(self, feature: str, settings: list[FakeSetting]) -> None:
        self.measurement_type, self.settings, self.error_code = feature, settings, "OK"


def test_settings_come_from_the_device_and_gaps_are_reported_not_filled() -> None:
    """Choosing a sample rate ourselves would be inventing a device parameter."""
    from consciousness_lab.verification.devices import _settings_to_kwargs

    settings = FakeSettings(
        "ACC",
        [
            FakeSetting("SAMPLE_RATE", [52, 104]),
            FakeSetting("RESOLUTION", [16]),
            FakeSetting("RANGE", [8]),
        ],
    )
    kwargs, unresolved = _settings_to_kwargs(settings, {"sample_rate", "resolution", "range"})
    assert kwargs == {"sample_rate": 52, "resolution": 16, "range": 8}
    assert unresolved == []

    partial, missing = _settings_to_kwargs(
        FakeSettings("ECG", [FakeSetting("SAMPLE_RATE", [130])]),
        {"sample_rate", "resolution"},
    )
    assert partial == {"sample_rate": 130}
    assert missing == ["resolution"], "a gap is reported, never filled with something plausible"


def test_a_feature_with_no_starter_is_reported_rather_than_skipped() -> None:
    """'The library cannot start this' is a finding about the library."""
    from consciousness_lab.verification import devices

    assert set(devices._PMD_STARTERS) >= {"ECG", "ACC"}, (
        "ACC is the channel that carries a tap or a cough; capturing only ECG would leave "
        "the alignment question unmeasurable while looking like a working capture"
    )


def test_every_offered_stream_is_started_and_described_without_being_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behavioural test: ACC must actually be captured, and never interpreted.

    Capturing only ECG would leave the alignment question unmeasurable while
    looking like a working capture — the accelerometer is the channel that
    carries a physical event. And nothing here may decide that a stream IS an
    accelerometer or that a transient in it IS anything at all.
    """
    from consciousness_lab.verification import devices

    started: list[str] = []

    class FakeFrame:
        def __init__(self, timestamp: int, n: int) -> None:
            self.timestamp, self.data = timestamp, list(range(n))

    class FakePolarDevice:
        def __init__(self, address: str) -> None:
            self.address = address

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def get_available_features(self) -> list[str]:
            return ["ECG", "ACC", "PPI"]  # PPI has no starter in the library

        async def request_stream_settings(self, feature: str) -> FakeSettings:
            common = [FakeSetting("SAMPLE_RATE", [130]), FakeSetting("RESOLUTION", [14])]
            if feature == "ACC":
                common.append(FakeSetting("RANGE", [8]))
            return FakeSettings(feature, common)

        async def start_ecg_stream(
            self, ecg_callback: Callable[..., None], sample_rate: int, resolution: int
        ) -> None:
            started.append("ECG")
            ecg_callback(FakeFrame(1000, 5))

        async def start_acc_stream(
            self,
            acc_callback: Callable[..., None],
            sample_rate: int,
            resolution: int,
            range: int,
            channels: int | None = None,
        ) -> None:
            started.append("ACC")
            acc_callback(FakeFrame(2000, 3))

    import polar_python

    monkeypatch.setattr(polar_python, "PolarDevice", FakePolarDevice)
    run = VerificationRun(subject="polar-h10", purpose="p", method="m", device_firmware="1")
    devices._capture_polar_pmd(run, 0.01, "AA:BB:CC:DD:EE:FF")

    assert sorted(started) == ["ACC", "ECG"], "the accelerometer must be captured too"
    used = next(o for o in run.observations if "streams started" in o.what)
    assert used.value["ACC"] == {"sample_rate": 130, "resolution": 14, "range": 8}
    assert "none chosen here" in used.how

    labels = [o.what for o in run.observations]
    assert any(label.startswith("ACC: host arrival times") for label in labels)
    assert any(label.startswith("ECG: shape of each delivered frame") for label in labels)
    # Structural description only — no physiological naming anywhere in the record.
    joined = " ".join(labels).lower()
    for word in ("tap", "cough", "impact", "heart", "bpm", "cardiac", "motion"):
        assert word not in joined, f"{word!r} is an interpretation, not an observation"

    # A feature the library cannot start is a reported finding, not a silent skip.
    assert any("PPI" in f and "no way to start it" in f for f in run.failures)


# ------------------------------------ CL-004-R2: the marker channel's round trip


class FakeSerialLink:
    """A QT Py that answers pings, with a mark and a heartbeat mixed in."""

    def __init__(self, port: str, baudrate: int, timeout: float) -> None:
        self.port, self.baudrate = port, baudrate
        self.closed = False
        self._outbox: list[bytes] = [b"B qtpy-marker/1 rp2040 1\n"]
        self._token: str | None = None
        self._replies = 0

    def write(self, payload: bytes) -> None:
        line = payload.decode("ascii").strip()
        if line.startswith("P "):
            self._token = line[2:].strip()

    def flush(self) -> None:
        return None

    def readline(self) -> bytes:
        if self._outbox:
            return self._outbox.pop(0)
        if self._token is None:
            return b""
        token, self._token = self._token, None
        self._replies += 1
        if self._replies == 2:
            self._outbox.append(b"M 0 123456789\n")
        if self._replies == 3:
            self._outbox.append(b"H 0 987654321\n")
        return f"R {token} {1000 * self._replies}\n".encode("ascii")

    def close(self) -> None:
        self.closed = True


def test_the_serial_probe_measures_the_spread_not_just_the_mean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate HANDOFF.md puts on CL-007 is latency AND ITS VARIABILITY."""
    import serial as pyserial

    from consciousness_lab.verification import devices

    monkeypatch.setattr(pyserial, "Serial", FakeSerialLink)
    run = VerificationRun(
        subject="qtpy-marker", purpose="p", method="m", device_firmware="qtpy-marker/1"
    )
    devices.capture_serial(run, "/dev/ttyACM0", seconds=5.0, pings=5)

    round_trip = next(o for o in run.observations if "round-trip time" in o.what)
    described = round_trip.value
    # The spread is what the ticket is gated on, so it must be in the record.
    for key in ("min", "max", "step_min", "n"):
        assert key in described
    assert "SPREAD" in round_trip.how


def test_unanswered_pings_are_counted_rather_than_quietly_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A latency figure over only the replies that arrived flatters the link."""
    import serial as pyserial

    from consciousness_lab.verification import devices

    monkeypatch.setattr(pyserial, "Serial", FakeSerialLink)
    run = VerificationRun(
        subject="qtpy-marker", purpose="p", method="m", device_firmware="qtpy-marker/1"
    )
    devices.capture_serial(run, "/dev/ttyACM0", seconds=5.0, pings=4)
    lost = next(o for o in run.observations if "unanswered" in o.what)
    assert set(lost.value) == {"lost", "attempted"}


def test_marks_keep_device_time_and_host_arrival_apart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TIMING.md rule 2: never overwrite a captured quantity with a derived one.

    The device's own clock value and the host's arrival time are two different
    quantities. The probe records both and derives neither from the other — no
    offset, no drift, no "corrected" time.
    """
    import serial as pyserial

    from consciousness_lab.verification import devices

    monkeypatch.setattr(pyserial, "Serial", FakeSerialLink)
    run = VerificationRun(
        subject="qtpy-marker", purpose="p", method="m", device_firmware="qtpy-marker/1"
    )
    devices.capture_serial(run, "/dev/ttyACM0", seconds=5.0, pings=5)

    marks = next(o for o in run.observations if o.what.startswith("marks that arrived"))
    assert marks.value[0]["line"] == "M 0 123456789", "the device line is kept verbatim"
    assert "host_arrival_ns" in marks.value[0]
    labels = " ".join(o.what for o in run.observations).lower()
    assert "offset" not in labels and "drift" not in labels and "corrected" not in labels


def test_a_port_that_will_not_open_is_a_recorded_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import serial as pyserial

    from consciousness_lab.verification import devices

    def refuse(port: str, baudrate: int, timeout: float) -> None:
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(pyserial, "Serial", refuse)
    run = VerificationRun(
        subject="qtpy-marker", purpose="p", method="m", device_firmware="qtpy-marker/1"
    )
    devices.capture_serial(run, "/dev/nope", seconds=1.0, pings=2)
    assert run.observations == []
    assert any("could not open /dev/nope" in f for f in run.failures)


def test_silence_names_the_two_likely_causes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead channel and a board without boot.py look identical; say both."""
    from consciousness_lab.verification import devices

    class Silent(FakeSerialLink):
        def readline(self) -> bytes:
            return b""

    import serial as pyserial

    monkeypatch.setattr(pyserial, "Serial", Silent)
    run = VerificationRun(
        subject="qtpy-marker", purpose="p", method="m", device_firmware="qtpy-marker/1"
    )
    devices.capture_serial(run, "/dev/ttyACM0", seconds=0.5, pings=2)
    failure = next(f for f in run.failures if "no ping was answered" in f)
    assert "marker firmware" in failure and "boot.py" in failure


def test_the_firmware_timestamps_before_it_debounces() -> None:
    """The one thing in the firmware that must not be got wrong.

    Debounce after the timestamp and the mark is true to first contact; debounce
    before it and an unmeasured delay has been added to a device whose entire
    purpose is knowing when. Asserted against the source because there is no way
    to run CircuitPython here.
    """
    source = Path("firmware/qtpy_marker/code.py").read_text(encoding="utf-8")
    body = source.split("while True:", 1)[1]
    now_at = body.index("now = time.monotonic_ns()")
    debounce_at = body.index("DEBOUNCE_MS")
    assert now_at < debounce_at, "the timestamp must be taken before any debounce logic"
    assert "time.sleep" not in body, "a sleep in the poll loop widens detection jitter"


def test_the_firmware_makes_no_electrical_contact_with_a_participant() -> None:
    """A switch to a GPIO and ground, and nothing driven anywhere.

    SAFETY.md S6 forbids inventing electrical limits; the way to obey it is to
    build something with no electrical interface to a person at all.
    """
    source = Path("firmware/qtpy_marker/code.py").read_text(encoding="utf-8")
    assert "Direction.OUTPUT" not in source, "nothing may be driven out"
    assert "analogio" not in source and "AnalogOut" not in source
    assert "digitalio.Direction.INPUT" in source


# --------------------------------------------- the Mac bootstrap is honest


def test_the_bootstrap_script_is_valid_bash_and_idempotent_by_construction() -> None:
    """It runs on a machine that is not this one, so it gets checked here.

    A broken bootstrap is discovered at the bench, which is the most expensive
    place to discover anything.
    """
    import subprocess

    script = Path("scripts/bootstrap-mac.sh")
    assert script.is_file() and script.stat().st_mode & 0o111, "must be executable"
    syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr

    source = script.read_text(encoding="utf-8")
    # Re-runnable: it must not assume a fresh machine.
    assert "already inside the checkout" in source
    assert "if command -v uv" in source, "an existing uv must not be reinstalled"
    # It ends by producing evidence rather than by declaring success.
    assert "probe env" in source


def test_the_bootstrap_does_not_pretend_to_grant_bluetooth_permission() -> None:
    """Only a human clicking in System Settings can, and saying otherwise wastes a session."""
    source = Path("scripts/bootstrap-mac.sh").read_text(encoding="utf-8")
    assert "cannot do" in source
    assert "Privacy & Security" in source
    assert "looks switched off" in source, "the symptom must be named, not just the fix"


def test_the_bench_runbook_orders_solo_probes_before_the_concurrent_one() -> None:
    """Step 7 has nothing to compare against unless 4 and 5 ran alone first."""
    doc = Path("docs/HARDWARE.md").read_text(encoding="utf-8")
    bench = doc[doc.index("### A bench session, in order") :]
    for earlier, later in (("probe scan", "probe polar"), ("probe muse", "probe concurrent")):
        assert bench.index(earlier) < bench.index(later), f"{earlier} must precede {later}"
    assert "must run **alone** before step 7" in bench


# ------------------------------------------------- hardware tests are excluded


def test_the_hardware_marker_is_registered_and_excluded_by_default() -> None:
    """AGENTS.md §8: a test needing hardware must be excludable, and excluded.

    Asserted against pyproject rather than trusted, because the whole point of
    the marker is that a default `pytest` run on a machine with no devices —
    or in CI, which has none — must not try to open a Bluetooth radio.
    """
    import tomllib

    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    options = config["tool"]["pytest"]["ini_options"]
    assert any("hardware" in marker for marker in options["markers"])
    assert "not hardware" in options["addopts"]


@pytest.mark.hardware
def test_a_muse_is_reachable_from_this_host() -> None:  # pragma: no cover - needs a device
    """Excluded by default. Running it does NOT verify anything (AGENTS.md §8).

    A passing hardware test says the code path ran, not that the device behaves
    as documented. Verification is a human observing the unit and writing it
    into HARDWARE.md with a date; nothing automated can stand in for that.
    """
    from consciousness_lab.verification.devices import capture_muse

    run = VerificationRun(
        subject="muse-s-athena",
        purpose="smoke: does the capture path run against a real board",
        method="live device attached to the test host",
        device_firmware="unknown-at-test-time",
    )
    capture_muse(run, seconds=5.0)
    assert run.observations or run.failures
