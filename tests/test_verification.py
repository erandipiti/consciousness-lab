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
