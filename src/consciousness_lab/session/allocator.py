"""Session allocation (spec §12.1; D8, D9, D15).

Ordering is the whole point:

    mkdir session directory        <- atomic uniqueness gate
    write + fsync allocation.json
    append + fsync ALLOCATED
    --- acquisition may begin ---
    update the derived registry     <- failure here is harmless

The package is created before the registry row, so no allocation can ever exist
only in the database. That is what makes the registry fully derived, and it is
why a scan can rebuild it completely.
"""

import contextlib
import platform
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from consciousness_lab.session import registry
from consciousness_lab.session.lifecycle import LifecycleLog, now_reading
from consciousness_lab.session.model import (
    Allocation,
    ClockReading,
    EnvironmentInfo,
    HostInfo,
    LifecycleState,
    Origin,
    Protocol,
    SoftwareInfo,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import atomic_write, sha256_file
from consciousness_lab.storage.paths import DataRoot, PackagePaths

MAX_ID_ATTEMPTS = 8


class AllocationError(RuntimeError):
    """A session could not be durably allocated."""


@dataclass(frozen=True)
class AllocatedSession:
    session_id: str
    paths: PackagePaths
    allocation: Allocation


def _host_info() -> HostInfo:
    """Host identity, by alias. No hostname that could identify a person."""
    return HostInfo(
        hostname_alias="workstation-01", os=platform.platform(), arch=platform.machine()
    )


def _software_info(repo_root: Path | None) -> SoftwareInfo:
    if repo_root is None or not (repo_root / ".git").exists():
        return SoftwareInfo(repo_commit=None, dirty=None)
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        # Provenance we cannot read is recorded as unknown, never as clean.
        return SoftwareInfo(repo_commit=None, dirty=None)
    return SoftwareInfo(repo_commit=commit, dirty=bool(status))


def _environment_info(repo_root: Path | None) -> EnvironmentInfo:
    lock_hash: str | None = None
    if repo_root is not None:
        lock = repo_root / "uv.lock"
        if lock.is_file():
            lock_hash = sha256_file(lock)
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return EnvironmentInfo(python_version=version, uv_lock_sha256=lock_hash)


def allocate_session(
    data_root: DataRoot,
    *,
    participant_pseudonym: str,
    protocol: Protocol | None = None,
    origin: Origin | None = None,
    repo_root: Path | None = None,
) -> AllocatedSession:
    """Allocate a session durably. Acquisition may not begin before this returns."""
    data_root.sessions.mkdir(parents=True, exist_ok=True)

    session_id = ""
    package: PackagePaths | None = None
    for _ in range(MAX_ID_ATTEMPTS):
        candidate = str(uuid.uuid4())
        target = data_root.sessions / candidate
        try:
            # The uniqueness gate lives where the data lives. A collision makes
            # mkdir fail; we generate another id and never merge into an
            # existing directory.
            target.mkdir()
        except FileExistsError:
            continue
        session_id = candidate
        package = PackagePaths(target)
        break
    if package is None:
        raise AllocationError("could not obtain an unused session id")

    utc_ns, monotonic_ns = now_reading()
    allocation = Allocation(
        session_id=session_id,
        allocated_at=ClockReading(utc_ns=utc_ns, monotonic_ns=monotonic_ns),
        origin=origin or Origin(),
        protocol=protocol or Protocol(),
        participant_pseudonym=participant_pseudonym,
        host=_host_info(),
        software=_software_info(repo_root),
        environment=_environment_info(repo_root),
    )
    atomic_write(
        package.allocation,
        canonical_json.canonicalize(allocation.model_dump(mode="json", exclude_none=True)),
    )
    LifecycleLog(package.lifecycle).append(LifecycleState.ALLOCATED)

    # Step 4 (spec §12.1): refresh the derived index. This happens AFTER the
    # package is durable, and a failure here is recoverable by a rescan, so it
    # is deliberately not allowed to break allocation.
    with contextlib.suppress(sqlite3.Error):
        registry.upsert(data_root, package)

    return AllocatedSession(session_id=session_id, paths=package, allocation=allocation)
