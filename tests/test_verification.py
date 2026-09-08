"""CL-004: the verification harness records evidence and refuses to conclude.

Nothing here touches a device or a Bluetooth stack. The one test that would is
marked `hardware` and excluded from the default run (`AGENTS.md` §8), and a test
in this file asserts that exclusion is real rather than assumed.
"""

import json
from pathlib import Path

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
