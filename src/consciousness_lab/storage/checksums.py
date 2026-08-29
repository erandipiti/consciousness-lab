"""Hashing and the atomic write primitives every writer uses (spec §12).

Durability here is deliberately plain: one workstation, no distributed
guarantees. What matters is that a file is either fully present under its final
name or not present at all, and that a crash can never leave a half-written file
under a name something else will trust.
"""

import hashlib
import os
from pathlib import Path

PART_SUFFIX = ".part"
TMP_SUFFIX = ".tmp"
INCOMPLETE_SUFFIXES = (PART_SUFFIX, TMP_SUFFIX, ".open")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fsync_dir(path: Path) -> None:
    """fsync a directory so a rename is durable, not just visible."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` via tmp + fsync + rename + fsync(dir)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + TMP_SUFFIX)
    with tmp.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def append_line(path: Path, line: bytes) -> None:
    """Append one durable line to an append-only log."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def find_incomplete(root: Path) -> list[Path]:
    """Every leftover ``.part`` / ``.tmp`` / ``.open`` file under ``root``.

    Their presence is a crash marker and blocks completion (predicate
    condition 6).
    """
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.rglob("*") if p.is_file() and p.name.endswith(INCOMPLETE_SUFFIXES)
    )
