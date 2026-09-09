"""The verification record: what AGENTS.md §7 requires, made structural.

§7 says a device is verified only when someone connected the physical unit and
measured it, and that recording a verification requires *all* of: the date, the
device firmware or hardware revision, the host OS and library version, what was
measured and how, and what was observed including failure modes.

Those are not conventions here. A run that cannot supply them cannot be written,
because a verification report missing its provenance is worse than no report: it
looks like evidence.

Nothing in this module interprets anything. `Observation` carries what was seen
and how it was seen; it has no field for what it means.
"""

import itertools
import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Where reports go. Deliberately NOT under `data/sessions/`: a verification run
#: is not a recording session, and its output must never be mistaken for study
#: data or picked up by anything that scans for packages.
VERIFICATION_DIR = "verification"

#: Libraries whose versions belong in every report, because "which library
#: version" is one of §7's required fields and the answer changes behaviour.
TRACKED_LIBRARIES = ("brainflow", "bleak", "polar_python", "serial", "pyarrow", "pydantic")


class IncompleteVerificationError(RuntimeError):
    """The run cannot be recorded because §7's required provenance is missing."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def library_versions() -> dict[str, str]:
    """Installed version of each tracked library, or why it could not be read.

    Imports rather than reading metadata, because what matters is the version
    that *this interpreter would actually use*, which is not always what the
    metadata of a shadowed install says.
    """
    from importlib.metadata import PackageNotFoundError, version

    #: Import name -> distribution name, where they differ.
    distributions = {"serial": "pyserial", "polar_python": "polar-python"}
    out: dict[str, str] = {}
    for name in TRACKED_LIBRARIES:
        try:
            __import__(name)
        except Exception as exc:  # a library that will not import is a finding
            out[name] = f"<not importable: {type(exc).__name__}>"
            continue
        try:
            out[name] = version(distributions.get(name, name))
        except PackageNotFoundError:
            out[name] = "<installed but no distribution metadata>"
    return out


def _run(*args: str) -> str | None:
    """One short command's stdout, or None. Never raises; absence is a finding."""
    if shutil.which(args[0]) is None:
        return None
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout or "").strip() or None


def ble_stack() -> dict[str, Any]:
    """What Bluetooth support this host actually has, as facts not judgements.

    Reported separately from "does a device work", because they fail
    independently and confusing them wastes a bench session: a missing stack is
    a host problem, a silent device is a device problem, and the two look
    identical from inside a library that just times out.
    """
    adapters = sorted(p.name for p in Path("/sys/class/bluetooth").glob("hci*"))
    daemon = next(
        (
            p
            for p in ("/usr/lib/bluetooth/bluetoothd", "/usr/libexec/bluetooth/bluetoothd")
            if Path(p).is_file()
        ),
        None,
    )
    return {
        "kernel_adapters": adapters,
        "bluetoothd_present": daemon,
        "bluetoothctl_on_path": shutil.which("bluetoothctl"),
        "bluetoothctl_version": _run("bluetoothctl", "--version"),
        "service_state": _run("systemctl", "is-active", "bluetooth"),
        "dbus_state": _run("systemctl", "is-active", "dbus"),
    }


def host_environment() -> dict[str, Any]:
    """The host, captured automatically. Facts only — nothing here is a choice."""
    return {
        "captured_at_utc": _utc_now(),
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "os_detail": platform.version(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "libraries": library_versions(),
        "ble": ble_stack(),
    }


@dataclass(frozen=True)
class Observation:
    """One thing that was seen, and how it was seen.

    There is deliberately no field for what it means. A reader of the report
    supplies that; this package does not, because the difference between "a
    column increased monotonically" and "the device has a clock" is exactly
    where an unverifiable claim would enter the record.
    """

    what: str
    #: Whatever was actually observed. Left untyped on purpose: constraining it
    #: would mean deciding in advance what a device is allowed to produce.
    value: Any
    #: How it was obtained, precisely enough for someone to repeat it.
    how: str


@dataclass
class VerificationRun:
    """One bench session against one device, or against the host alone.

    ``device_firmware`` is required for a device run and has no default. It is
    the field most likely to be skipped and the one that most often explains a
    contradiction between two runs a month apart.
    """

    #: What was under test. ``"host"`` for a run with no device attached.
    subject: str
    #: What the run set out to measure, in the operator's words.
    purpose: str
    #: How it was carried out, precisely enough to be repeated.
    method: str
    #: Firmware or hardware revision. Required unless subject is "host".
    device_firmware: str | None = None
    #: Study-local alias, never a serial number (D26, SAFETY.md S4).
    device_alias: str | None = None
    observations: list[Observation] = field(default_factory=list)
    #: What went wrong. An empty list is a claim that nothing did, so it is
    #: recorded explicitly rather than left to be inferred from silence.
    failures: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=host_environment)
    started_at_utc: str = field(default_factory=_utc_now)

    def observe(self, what: str, value: Any, how: str) -> None:
        self.observations.append(Observation(what=what, value=value, how=how))

    def fail(self, detail: str) -> None:
        """Record a failure mode. §7 requires these; they are not noise."""
        self.failures.append(detail)

    def absorb(self, child: "VerificationRun", prefix: str) -> None:
        """Fold a sub-run's observations and failures in under a prefix.

        Multi-phase probes — reconnect cycles, two devices held at once — are
        several bounded captures whose whole value is being COMPARABLE. Keeping
        them in one report under prefixed names is what lets a reader put cycle
        0 beside cycle 1, or the Muse beside the Polar, without cross-referencing
        files. The prefix is the only thing added: no phase is summarised, and
        no difference between phases is computed, because "the counter reset on
        reconnect" is exactly the conclusion this package does not draw.
        """
        for observation in child.observations:
            self.observe(f"[{prefix}] {observation.what}", observation.value, observation.how)
        for failure in child.failures:
            self.fail(f"[{prefix}] {failure}")

    def _missing(self) -> list[str]:
        missing = [
            name
            for name in ("subject", "purpose", "method")
            if not (getattr(self, name) or "").strip()
        ]
        if self.subject != "host" and not (self.device_firmware or "").strip():
            missing.append("device_firmware")
        if not self.observations and not self.failures:
            # A run that saw nothing and reports no failure has not recorded
            # what happened; it has recorded that nobody looked.
            missing.append("observations or failures")
        return missing

    def to_document(self) -> dict[str, Any]:
        missing = self._missing()
        if missing:
            raise IncompleteVerificationError(
                "AGENTS.md §7 requires every verification to carry its provenance; "
                f"this run is missing: {', '.join(missing)}"
            )
        return {
            "schema": "hardware-verification/1",
            "subject": self.subject,
            "device_alias": self.device_alias,
            "device_firmware": self.device_firmware,
            "purpose": self.purpose,
            "method": self.method,
            "started_at_utc": self.started_at_utc,
            "finished_at_utc": _utc_now(),
            "environment": self.environment,
            "observations": [
                {"what": o.what, "value": o.value, "how": o.how} for o in self.observations
            ],
            "failures": list(self.failures),
            "verified": False,
            "note": (
                "Evidence only. Nothing here is a verified device fact: promoting a row in "
                "docs/HARDWARE.md requires a human who connected the unit and observed it "
                "(AGENTS.md §7). This file is not study data and is not a session package."
            ),
        }


def report_path(root: Path, run: VerificationRun) -> Path:
    stamp = run.started_at_utc.replace(":", "").replace("-", "")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in run.subject)
    return Path(root) / VERIFICATION_DIR / f"{stamp}-{safe}.json"


def write_report(root: Path, run: VerificationRun) -> Path:
    """Write the report outside data/sessions/, refusing an incomplete one.

    Ordinary write, not the atomic ceremony raw acquisition uses: this is
    evidence for a human to read, not immutable study data, and pretending
    otherwise would blur exactly the line this package exists to hold.
    """
    document = run.to_document()  # raises before anything touches disk
    target = report_path(root, run)
    if "sessions" in target.parts:
        raise IncompleteVerificationError(
            f"refusing to write a verification report inside {target}; verification "
            "output must never land among session packages"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return target


def describe_series(values: list[float]) -> dict[str, Any]:
    """Describe a numeric series without naming what it is.

    Used for whatever a library hands back that looks like it could be a clock
    or a counter. It reports shape — monotonic, strictly increasing, its steps,
    where it went backwards — and never says which of those it is. That naming
    is the conclusion a human draws from the report, and one wrong guess about
    a device's timebase is not recoverable from the recorded data later.
    """
    if not values:
        return {"n": 0}
    steps = [b - a for a, b in itertools.pairwise(values)]
    backwards = [i for i, s in enumerate(steps) if s < 0]
    return {
        "n": len(values),
        "first": values[0],
        "last": values[-1],
        "min": min(values),
        "max": max(values),
        "non_decreasing": all(s >= 0 for s in steps),
        "strictly_increasing": all(s > 0 for s in steps),
        "distinct_step_count": len({round(s, 12) for s in steps}),
        "step_min": min(steps) if steps else None,
        "step_max": max(steps) if steps else None,
        "went_backwards_at": backwards[:20],
        "went_backwards_total": len(backwards),
    }


def data_root_default() -> Path:
    """Where reports go by default. Overridable so a bench run can be isolated."""
    return Path(os.environ.get("CL_VERIFICATION_ROOT") or "data")
