"""Path safety for package content (Codex CL-002B review pass 2).

Every path that comes *out of a package file* — a manifest inventory entry, a
chunk artifact path, a `payload_ref.file` — is untrusted input. Two attacks
follow if it is used naively:

* **traversal**: ``../../etc/passwd`` or an absolute path resolves outside the
  package, so verification hashes a file that is not package content;
* **symlink**: an artifact is moved out of the package and replaced by a link
  to it, so ``is_file()``, ``stat()`` and ``sha256_file()`` all succeed against
  bytes the package no longer contains.

Either makes a package with missing data verify clean, which is a
false-complete. So every such path is validated, and no component inside a
sealed package may be a symlink.
"""

from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    """A path from package content is not a safe relative path inside the package."""


def validate_relative(value: str) -> PurePosixPath:
    """Check a package-relative POSIX path and return it."""
    if not value or value != value.strip():
        raise UnsafePathError(f"{value!r} is empty or padded")
    if "\\" in value or "\x00" in value:
        raise UnsafePathError(f"{value!r} contains an illegal character")
    if value.startswith("/"):
        raise UnsafePathError(f"{value!r} is absolute")
    # Validate the RAW segments, not PurePosixPath.parts: the latter normalizes
    # "a//b" and "a/./b" down to ("a", "b"), so a check against parts would pass
    # a path that is not the spelling the manifest recorded. One file must have
    # exactly one spelling, for the same reason one integer must.
    for segment in value.split("/"):
        if segment in {"", ".", ".."}:
            raise UnsafePathError(f"{value!r} contains an empty or traversal segment")
    return PurePosixPath(value)


def contains_symlink(root: Path, relative: PurePosixPath) -> bool:
    """True if any component of ``root/relative`` is a symlink."""
    current = root
    if current.is_symlink():
        return True
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def resolve_within(root: Path, value: str) -> Path:
    """Resolve a package-relative path, refusing traversal and symlinks."""
    relative = validate_relative(value)
    if contains_symlink(root, relative):
        raise UnsafePathError(f"{value!r} traverses a symlink")
    target = root / relative
    try:
        resolved = target.resolve(strict=False)
        root_resolved = root.resolve(strict=False)
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise UnsafePathError(f"{value!r} resolves outside the package") from exc
    return target


def find_symlinks(root: Path) -> list[Path]:
    """Every symlink anywhere under ``root``. Sealed content must contain none."""
    if not root.exists():
        return []
    found: list[Path] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            found.append(path)
    return found
