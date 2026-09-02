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

#: Linux lets a file be created with no directory entry at all, so a write-once
#: publish can be atomic with no temporary name to clean up afterwards. Absent
#: elsewhere, in which case the named-temporary fallback below is used.
_O_TMPFILE: int | None = getattr(os, "O_TMPFILE", None)


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


class ImmutableFileError(RuntimeError):
    """An attempt was made to overwrite a file that is immutable once written."""


def atomic_write_new(path: Path, data: bytes) -> None:
    """Write ``path`` atomically, refusing to overwrite an existing file.

    Immutability is enforced here rather than by convention: ``os.replace``
    silently clobbers, so every write-once artifact goes through this instead
    (AGENTS.md §5, D10, D23).
    """
    if path.exists():
        raise ImmutableFileError(f"{path} already exists and is immutable once written")
    path.parent.mkdir(parents=True, exist_ok=True)

    # Publish from a file that has NO NAME until it is linked into place. A
    # named temporary would have to be unlinked afterwards, and a crash in that
    # window leaves both the published file and a `.tmp` beside it — which
    # condition 6 rejects and condition 1 accepts, producing a package that
    # looks sealed and can never verify. An anonymous file cannot leave residue
    # because there is nothing to clean up.
    if _O_TMPFILE is not None:
        try:
            fd = os.open(path.parent, os.O_WRONLY | _O_TMPFILE, 0o644)
        except OSError:
            fd = None  # the filesystem does not support it
        if fd is not None:
            try:
                os.write(fd, data)
                os.fsync(fd)
                os.link(f"/proc/self/fd/{fd}", path, follow_symlinks=True)
            except FileExistsError as exc:
                os.close(fd)
                raise ImmutableFileError(f"{path} was created concurrently") from exc
            except OSError:
                # Some filesystems refuse to link out of /proc (EXDEV under
                # overlayfs, for instance). Nothing was published: the file
                # still has no name, so closing it discards it cleanly.
                os.close(fd)
            else:
                os.close(fd)
                fsync_dir(path.parent)
                return

    # Fallback where O_TMPFILE is unavailable. The residue window exists here,
    # so recovery knows how to recognise and clear it (see
    # `recovery.publish_residue`).
    tmp = path.with_name(path.name + TMP_SUFFIX)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # os.link + unlink gives a no-clobber rename; plain rename would overwrite.
    try:
        os.link(tmp, path)
    except FileExistsError as exc:
        tmp.unlink(missing_ok=True)
        raise ImmutableFileError(f"{path} was created concurrently") from exc
    finally:
        tmp.unlink(missing_ok=True)
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
