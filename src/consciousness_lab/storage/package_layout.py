"""The v2 package layout, closed rather than open-ended (§9.2, §9.3).

The integrity DAG claims every immutable sealed byte is reachable from the
manifest. That claim is only true if the package cannot contain a file the DAG
never mentions — otherwise an unexpected immutable file simply sits outside it,
unhashed and unnoticed. So the layout is exhaustively defined here, once, and
both finalization and verification derive the expected file set from this module
rather than each holding its own idea of it.

The expected control set is **derived** — from the physical stream directories
and the schema ids the sealed events log references — and never read back from
the manifest it is compared against.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from consciousness_lab.session.model import Manifest, load_on_disk
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.paths import PackagePaths

#: Exactly the files the package root may hold.
ROOT_FILES = frozenset(
    {
        "allocation.json",
        "run.json",
        "lifecycle.jsonl",
        "annotations.jsonl",
        "annotations.head.json",
        "manifest.json",
        "manifest.sha256",
    }
)
#: Exactly the directories the package root may hold. ``logs/`` may contain
#: anything: it is operational, never authoritative, and outside the DAG.
ROOT_DIRS = frozenset({"events", "schemas", "raw", "logs"})
EVENTS_FILES = frozenset({"events.jsonl"})
#: Control files at the package root, in the order the manifest example uses.
ROOT_CONTROL_FILES = ("allocation.json", "run.json")
#: Per-stream control files (§7.1, D33).
STREAM_CONTROL_FILES = ("descriptor.json", "chunks.jsonl", "stream_close.json")


class JsonlRegionError(ValueError):
    """A sealed JSONL region violates §4.1 or §9.3."""


def referenced_schema_ids(events: bytes) -> tuple[set[str], str | None]:
    """The ``payload_schema`` ids the sealed events log references.

    This is the authority for which schema snapshots a package must carry: the
    physical ``schemas/`` set must equal it exactly, so no unreferenced snapshot
    can sit silently outside the integrity DAG.
    """
    ids: set[str] = set()
    for number, line in enumerate(events.split(b"\n")):
        if not line:
            continue
        try:
            obj = canonical_json.loads(line)
        except (canonical_json.CanonicalizationError, ValueError) as exc:
            return ids, f"events line {number} is not parseable ({exc})"
        if not isinstance(obj, dict):
            return ids, f"events line {number} is not a JSON object"
        schema_id = obj.get("payload_schema")
        if not isinstance(schema_id, str) or not schema_id:
            return ids, f"events line {number} names no payload_schema"
        ids.add(schema_id)
    return ids, None


def expected_control_paths(stream_ids: list[str], schema_ids: set[str]) -> list[str]:
    """The derived control set: exactly what ``control_sha256`` must key on."""
    paths = list(ROOT_CONTROL_FILES)
    for stream_id in sorted(stream_ids):
        paths.extend(f"raw/{stream_id}/{name}" for name in STREAM_CONTROL_FILES)
    paths.extend(f"schemas/{schema_id}.json" for schema_id in sorted(schema_ids))
    return paths


@dataclass(frozen=True)
class LayoutScan:
    """Files the package holds that the v2 layout does not define."""

    unexpected: tuple[str, ...]
    #: Schema snapshots present but referenced by no sealed event.
    unreferenced_schemas: tuple[str, ...]
    #: Schema ids referenced by a sealed event with no snapshot on disk.
    missing_schemas: tuple[str, ...]


def scan_layout(paths: PackagePaths, schema_ids: set[str]) -> LayoutScan:
    """Check the root, ``events/`` and ``schemas/`` against the closed layout.

    The raw tree is scanned per stream in ``stream_state`` and lands under
    condition 7; this covers the parts condition 2 owns.
    """
    unexpected: list[str] = []
    root = paths.root
    if not root.is_dir():
        return LayoutScan((), (), ())

    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            if entry.name not in ROOT_DIRS:
                unexpected.append(f"{entry.name}/")
        elif entry.name not in ROOT_FILES:
            unexpected.append(entry.name)

    events_dir = root / "events"
    if events_dir.is_dir():
        for entry in sorted(events_dir.iterdir()):
            if entry.is_dir() or entry.name not in EVENTS_FILES:
                unexpected.append(f"events/{entry.name}")

    present: set[str] = set()
    schemas_dir = root / "schemas"
    if schemas_dir.is_dir():
        for entry in sorted(schemas_dir.iterdir()):
            if entry.is_dir() or not entry.name.endswith(".json"):
                unexpected.append(f"schemas/{entry.name}")
                continue
            present.add(entry.name[: -len(".json")])

    return LayoutScan(
        unexpected=tuple(unexpected),
        unreferenced_schemas=tuple(sorted(present - schema_ids)),
        missing_schemas=tuple(sorted(schema_ids - present)),
    )


def canonical_document_error(raw: bytes) -> str | None:
    """Why a JSON document is not canonical on disk, or ``None`` (§9.3).

    "It parses, and canonicalizing it would produce equivalent content" is not
    sufficient. Two byte sequences that parse alike are still two different
    files, and a format that tolerates both has two spellings for one record.
    """
    try:
        obj = canonical_json.loads(raw)
    except (canonical_json.CanonicalizationError, ValueError) as exc:
        return f"is not parseable ({exc})"
    if canonical_json.canonicalize(obj) != raw:
        return "is not canonical on disk"
    return None


def verify_jsonl_region(
    raw: bytes, model: type[BaseModel], *, require_record_hash: bool
) -> str | None:
    """Validate a sealed JSONL region. Returns the first violation, or ``None``.

    Enforces, in order: §4.1 (a record is one complete canonical JSON object
    terminated by ``\\n``, and non-empty bytes after the last newline are not a
    record — in a finalized package they make the file invalid), §9.3 (canonical
    **on disk**, byte for byte), the model contract, and — where the log carries
    one — the record's own ``record_sha256``, which is the pre-seal integrity
    layer recovery depends on (§6.1, D34).
    """
    lines = raw.split(b"\n")
    tail = lines.pop() if lines else b""
    if tail:
        return "has trailing bytes after the last newline"
    for number, line in enumerate(lines):
        if not line:
            return f"line {number} is empty; a record is one object per line"
        try:
            obj = canonical_json.loads(line)
        except (canonical_json.CanonicalizationError, ValueError) as exc:
            return f"line {number} is not parseable ({exc})"
        if not isinstance(obj, dict):
            return f"line {number} is not a JSON object"
        if require_record_hash and not canonical_json.verify_record(obj):
            return f"line {number}: record_sha256 does not verify"
        if canonical_json.canonicalize(obj) != line:
            return f"line {number} is not canonical on disk"
        try:
            load_on_disk(model, obj)
        except ValueError as exc:
            return f"line {number} is invalid on disk ({exc})"
    return None


def read_canonical_json(path: Path) -> tuple[Any, str | None]:
    """Read a JSON document, requiring it to be canonical on disk."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable ({exc})"
    error = canonical_document_error(raw)
    if error is not None:
        return None, error
    return canonical_json.loads(raw), None


def read_manifest(path: Path) -> Manifest | None:
    """Parse ``manifest.json``, or ``None`` when it is absent or unreadable.

    It lives here rather than beside finalization because both the verifier and
    the derived registry need it, and routing them through the finalizer made
    the module graph circular for no benefit.
    """
    if not path.exists():
        return None
    try:
        return load_on_disk(Manifest, canonical_json.loads(path.read_bytes()))
    except (canonical_json.CanonicalizationError, ValueError):
        return None
