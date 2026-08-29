"""Package verification and the completion predicate (spec §14; D21, D22).

One predicate, eight conditions, evaluating the **effective** outcome. There is
deliberately only one definition of completion in this codebase: a second,
subtly different one is exactly the failure five review passes were spent
eliminating.

Verification fails closed and reports *why*, as a list of structured findings,
rather than collapsing every distinct failure into one exception.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import pyarrow as pa

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session.finalizer import read_manifest
from consciousness_lab.session.lifecycle import parse_records, summarize
from consciousness_lab.session.model import (
    SCHEMA_MAJOR,
    ChunkCommit,
    ClosureCondition,
    LifecycleState,
    Manifest,
    RecordingOutcome,
    Run,
    StreamCloseStatus,
    StreamDescriptor,
    load_on_disk,
)
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import find_incomplete, sha256_bytes, sha256_file
from consciousness_lab.storage.paths import PackagePaths
from consciousness_lab.storage.payload import PayloadFramingError, PayloadRef, read_at
from consciousness_lab.storage.safe_paths import (
    UnsafePathError,
    find_symlinks,
    resolve_within,
)

#: The only files a sealed package may hold outside the manifest inventory
#: (spec 14.1). Everything else is either inventoried or unexpected.
POST_SEAL_MUTABLE = frozenset(
    {"manifest.json", "manifest.sha256", "annotations.jsonl", "annotations.head.json"}
)


class Finding(StrEnum):
    """Every distinct way verification can fail."""

    MISSING_MANIFEST = "missing_manifest"
    MISSING_MANIFEST_SHA = "missing_manifest_sha"
    BAD_MANIFEST_HASH = "bad_manifest_hash"
    UNREADABLE_MANIFEST = "unreadable_manifest"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"
    INVENTORY_FILE_MISSING = "inventory_file_missing"
    INVENTORY_HASH_MISMATCH = "inventory_hash_mismatch"
    BROKEN_LIFECYCLE_SEAL = "broken_lifecycle_seal"
    BROKEN_EVENTS_SEAL = "broken_events_seal"
    NOT_CLEANLY_CLOSED = "not_cleanly_closed"
    SEALED_OUTCOME_NOT_COMPLETED = "sealed_outcome_not_completed"
    REQUIRED_STREAM_UNCLEAN = "required_stream_unclean"
    REQUIRED_STREAM_MISSING = "required_stream_missing"
    INCOMPLETE_FILE_PRESENT = "incomplete_file_present"
    BROKEN_CHUNK_CHAIN = "broken_chunk_chain"
    CHUNK_ARTIFACT_MISSING = "chunk_artifact_missing"
    CHUNK_ARTIFACT_HASH_MISMATCH = "chunk_artifact_hash_mismatch"
    ORPHAN_FILE = "orphan_file"
    MISSING_PAYLOAD_ARTIFACT = "missing_payload_artifact"
    UNEXPECTED_PAYLOAD_ARTIFACT = "unexpected_payload_artifact"
    PAYLOAD_REF_INVALID = "payload_ref_invalid"
    ANNOTATION_INTEGRITY_INDETERMINATE = "annotation_integrity_indeterminate"
    ANNOTATION_REJECTED = "annotation_rejected"
    EFFECTIVE_OUTCOME_NOT_COMPLETED = "effective_outcome_not_completed"
    UNREADABLE_DESCRIPTOR = "unreadable_descriptor"
    UNREADABLE_RUN = "unreadable_run"
    UNREADABLE_CHUNK_ARTIFACT = "unreadable_chunk_artifact"
    UNEXPECTED_FILE = "unexpected_file"
    SYMLINK_IN_PACKAGE = "symlink_in_package"
    UNSAFE_PATH = "unsafe_path"


@dataclass(frozen=True)
class Issue:
    finding: Finding
    detail: str
    path: str | None = None


@dataclass
class VerificationResult:
    """What verification learned about a package."""

    session_id: str
    issues: list[Issue] = field(default_factory=list)
    manifest: Manifest | None = None
    run: Run | None = None
    sealed_outcome: RecordingOutcome | None = None
    effective: annotations_mod.EffectiveOutcome | None = None
    conditions: dict[int, bool] = field(default_factory=dict)

    @property
    def is_completed(self) -> bool:
        """The eight-condition predicate. True only when every condition holds."""
        return len(self.conditions) == 8 and all(self.conditions.values())

    @property
    def is_sealed(self) -> bool:
        """Whether the package was cleanly finalized. NOT the same as completed."""
        return self.conditions.get(1, False)

    def findings(self) -> set[Finding]:
        return {issue.finding for issue in self.issues}

    def add(self, finding: Finding, detail: str, path: str | None = None) -> None:
        self.issues.append(Issue(finding, detail, path))


def read_chunk_index(paths: PackagePaths, stream_id: str) -> tuple[list[ChunkCommit], str | None]:
    """Parse and hash-chain-verify one stream's chunk index."""
    index = paths.stream(stream_id).chunks_index
    if not index.exists():
        return [], None
    commits: list[ChunkCommit] = []
    prev = canonical_json.ZERO_HASH
    for number, line in enumerate(index.read_bytes().split(b"\n")):
        if not line.strip():
            continue
        try:
            obj = canonical_json.loads(line)
        except (canonical_json.CanonicalizationError, ValueError):
            return commits, f"line {number} is not parseable"
        if not isinstance(obj, dict) or not canonical_json.verify_record(obj):
            return commits, f"line {number} record_sha256 does not verify"
        try:
            commit = load_on_disk(ChunkCommit, obj)
        except ValueError as exc:
            return commits, f"line {number} is malformed ({exc})"
        if commit.prev_record_sha256 != prev:
            return commits, f"line {number} breaks the hash chain"
        prev = str(commit.record_sha256)
        commits.append(commit)
    return commits, None


def verify_package(paths: PackagePaths) -> VerificationResult:
    """Verify a package and evaluate the eight-condition completion predicate."""
    result = VerificationResult(session_id=paths.root.name)

    # --- Condition 1: manifest.json exists and manifest.sha256 matches it ----
    manifest_ok = True
    manifest = None
    if not paths.manifest.exists():
        result.add(Finding.MISSING_MANIFEST, "manifest.json is absent")
        manifest_ok = False
    elif not paths.manifest_sha256.exists():
        result.add(Finding.MISSING_MANIFEST_SHA, "manifest.sha256 is absent")
        manifest_ok = False
    else:
        recorded = paths.manifest_sha256.read_text(encoding="utf-8").strip()
        actual = sha256_bytes(paths.manifest.read_bytes())
        if recorded != actual:
            result.add(Finding.BAD_MANIFEST_HASH, "manifest.sha256 does not match manifest.json")
            manifest_ok = False
        manifest = read_manifest(paths.manifest)
        if manifest is None:
            result.add(Finding.UNREADABLE_MANIFEST, "manifest.json could not be parsed")
            manifest_ok = False
        elif int(manifest.schema_version.split(".")[0]) != SCHEMA_MAJOR:
            result.add(
                Finding.SCHEMA_VERSION_UNSUPPORTED,
                f"schema_version {manifest.schema_version} is not major {SCHEMA_MAJOR}",
            )
            manifest_ok = False
    result.manifest = manifest
    result.conditions[1] = manifest_ok

    # --- Condition 2: the package holds EXACTLY the sealed files ------------
    # Both directions. Every inventory entry must be present and hash correctly,
    # AND nothing outside the inventory may exist except the objects 14.1 permits
    # to change after sealing. Checking only the first direction would let extra
    # immutable content be ADDED to a sealed package unnoticed.
    inventory_ok = manifest is not None

    for link in find_symlinks(paths.root):
        # A symlink lets an artifact be moved out of the package and faked back
        # in: is_file(), stat() and sha256 all follow it and all succeed.
        result.add(
            Finding.SYMLINK_IN_PACKAGE,
            "sealed package content may not contain a symlink",
            link.relative_to(paths.root).as_posix(),
        )
        inventory_ok = False

    if manifest is not None:
        for entry in manifest.inventory:
            try:
                target = resolve_within(paths.root, entry.path)
            except UnsafePathError as exc:
                result.add(Finding.UNSAFE_PATH, str(exc), entry.path)
                inventory_ok = False
                continue
            if not target.is_file():
                result.add(Finding.INVENTORY_FILE_MISSING, "inventory file is absent", entry.path)
                inventory_ok = False
                continue
            if sha256_file(target) != entry.sha256:
                result.add(Finding.INVENTORY_HASH_MISMATCH, "inventory hash mismatch", entry.path)
                inventory_ok = False

        listed = {entry.path for entry in manifest.inventory}
        for path in sorted(paths.root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(paths.root).as_posix()
            if rel in listed or rel in POST_SEAL_MUTABLE or rel.startswith("logs/"):
                continue
            result.add(
                Finding.UNEXPECTED_FILE,
                "present in a sealed package but absent from the manifest inventory",
                rel,
            )
            inventory_ok = False
    result.conditions[2] = inventory_ok

    # --- Condition 3: the sealed lifecycle prefix hashes as recorded --------
    seal_ok = False
    if manifest is not None and paths.lifecycle.exists():
        raw = paths.lifecycle.read_bytes()
        sealed_len = manifest.lifecycle_seal.sealed_len
        if len(raw) < sealed_len:
            result.add(Finding.BROKEN_LIFECYCLE_SEAL, "lifecycle.jsonl is shorter than its seal")
        elif sha256_bytes(raw[:sealed_len]) != manifest.lifecycle_seal.sealed_sha256:
            result.add(Finding.BROKEN_LIFECYCLE_SEAL, "sealed lifecycle prefix hash mismatch")
        else:
            seal_ok = True
        if manifest.events_seal.bytes or paths.events.exists():
            events = paths.events.read_bytes() if paths.events.exists() else b""
            if sha256_bytes(events) != manifest.events_seal.sha256:
                result.add(Finding.BROKEN_EVENTS_SEAL, "events seal hash mismatch")
                seal_ok = False
    elif manifest is not None:
        result.add(Finding.BROKEN_LIFECYCLE_SEAL, "lifecycle.jsonl is absent")
    result.conditions[3] = seal_ok

    # --- Condition 4: sealed prefix says CLOSED / CLEAN / COMPLETED ---------
    sealed_ok = False
    if seal_ok and manifest is not None:
        # Parsed in memory. Writing a temp file inside the package would put a
        # post-seal file into a sealed unit, which §14.1 forbids — and a crash
        # mid-verification would leave it there.
        prefix = paths.lifecycle.read_bytes()[: manifest.lifecycle_seal.sealed_len]
        summary = summarize(parse_records(prefix))
        result.sealed_outcome = summary.sealed_outcome
        if summary.terminal_state is not LifecycleState.CLOSED:
            result.add(Finding.NOT_CLEANLY_CLOSED, f"terminal state is {summary.terminal_state}")
        elif summary.closure_condition is not ClosureCondition.CLEAN:
            result.add(
                Finding.NOT_CLEANLY_CLOSED, f"closure condition is {summary.closure_condition}"
            )
        elif summary.sealed_outcome is not RecordingOutcome.COMPLETED:
            result.add(
                Finding.SEALED_OUTCOME_NOT_COMPLETED,
                f"sealed outcome is {summary.sealed_outcome}",
            )
        else:
            sealed_ok = True
    result.conditions[4] = sealed_ok

    # --- Condition 5: every required stream closed CLEAN --------------------
    run = None
    if paths.run.exists():
        try:
            run = load_on_disk(Run, canonical_json.loads(paths.run.read_bytes()))
        except (canonical_json.CanonicalizationError, ValueError) as exc:
            result.add(Finding.UNREADABLE_RUN, f"run.json could not be parsed ({exc})")
    result.run = run
    required_ok = run is not None and manifest is not None
    if run is not None and manifest is not None:
        by_id = {s.stream_id: s for s in manifest.streams}
        for stream_id in run.required_streams:
            stream_entry = by_id.get(stream_id)
            if stream_entry is None:
                result.add(Finding.REQUIRED_STREAM_MISSING, "required stream absent", stream_id)
                required_ok = False
            elif stream_entry.close_status is not StreamCloseStatus.CLEAN:
                result.add(
                    Finding.REQUIRED_STREAM_UNCLEAN,
                    f"required stream closed {stream_entry.close_status}",
                    stream_id,
                )
                required_ok = False
    result.conditions[5] = required_ok

    # --- Condition 6: no .part/.tmp/.open anywhere --------------------------
    incomplete = find_incomplete(paths.root)
    for path in incomplete:
        result.add(
            Finding.INCOMPLETE_FILE_PRESENT,
            "incomplete write marker present",
            path.relative_to(paths.root).as_posix(),
        )
    result.conditions[6] = not incomplete

    # --- Condition 7: chunk chains verify, no orphans, payload shape correct -
    chains_ok = _verify_streams(paths, manifest, result)
    result.conditions[7] = chains_ok

    # --- Condition 8: the effective outcome is COMPLETED --------------------
    effective = annotations_mod.resolve_effective_outcome(
        sealed_outcome=result.sealed_outcome,
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
    )
    result.effective = effective
    effective_ok = True
    if effective.status is annotations_mod.AnnotationStatus.INDETERMINATE:
        result.add(Finding.ANNOTATION_INTEGRITY_INDETERMINATE, effective.detail)
        effective_ok = False
    else:
        for rejected in effective.rejected:
            result.add(
                Finding.ANNOTATION_REJECTED,
                f"annotation {rejected.seq} rejected: {rejected.detail}",
            )
            effective_ok = False
        if effective.outcome is not RecordingOutcome.COMPLETED:
            result.add(
                Finding.EFFECTIVE_OUTCOME_NOT_COMPLETED,
                f"effective outcome is {effective.outcome}",
            )
            effective_ok = False
    result.conditions[8] = effective_ok
    return result


def _verify_streams(
    paths: PackagePaths, manifest: Manifest | None, result: VerificationResult
) -> bool:
    ok = True
    if not paths.raw.is_dir():
        return manifest is not None
    for stream_dir in sorted(p for p in paths.raw.iterdir() if p.is_dir()):
        stream_id = stream_dir.name
        stream_paths = paths.stream(stream_id)
        try:
            descriptor = load_on_disk(
                StreamDescriptor, canonical_json.loads(stream_paths.descriptor.read_bytes())
            )
        except (OSError, canonical_json.CanonicalizationError, ValueError) as exc:
            result.add(Finding.UNREADABLE_DESCRIPTOR, f"{exc}", stream_id)
            ok = False
            continue

        commits, chain_error = read_chunk_index(paths, stream_id)
        if chain_error is not None:
            result.add(Finding.BROKEN_CHUNK_CHAIN, chain_error, stream_id)
            ok = False

        committed_paths: set[str] = set()
        #: Chunks whose artifacts all hash correctly. Only these are worth
        #: opening: reading a corrupt Arrow file would raise out of a function
        #: whose contract is to REPORT failures, not raise them.
        intact: set[int] = {commit.chunk_id for commit in commits}
        for commit in commits:
            artifacts = [commit.packets, commit.observations, commit.samples]
            if commit.payloads is not None:
                artifacts.append(commit.payloads)
            if descriptor.expects_payload_artifact and commit.payloads is None:
                result.add(
                    Finding.MISSING_PAYLOAD_ARTIFACT,
                    f"chunk {commit.chunk_id} has no payload artifact",
                    stream_id,
                )
                ok = False
            if not descriptor.expects_payload_artifact and commit.payloads is not None:
                result.add(
                    Finding.UNEXPECTED_PAYLOAD_ARTIFACT,
                    f"chunk {commit.chunk_id} carries a payload artifact at "
                    f"raw_capture_level={descriptor.acquisition.raw_capture_level.value}",
                    stream_id,
                )
                ok = False
            for artifact in artifacts:
                committed_paths.add(artifact.path)
                try:
                    target = resolve_within(stream_dir, artifact.path)
                except UnsafePathError as exc:
                    result.add(Finding.UNSAFE_PATH, str(exc), f"{stream_id}/{artifact.path}")
                    ok = False
                    intact.discard(commit.chunk_id)
                    continue
                if not target.is_file():
                    result.add(Finding.CHUNK_ARTIFACT_MISSING, artifact.path, stream_id)
                    ok = False
                    intact.discard(commit.chunk_id)
                elif sha256_file(target) != artifact.sha256:
                    result.add(Finding.CHUNK_ARTIFACT_HASH_MISMATCH, artifact.path, stream_id)
                    ok = False
                    intact.discard(commit.chunk_id)

        # Files present under raw/ that no commit record names are orphans.
        # Recovery reports them; it never adopts them.
        for kind in ("payloads", "packets", "observations", "samples"):
            directory = stream_dir / kind
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                rel = f"{kind}/{path.name}"
                if path.is_file() and rel not in committed_paths:
                    result.add(
                        Finding.ORPHAN_FILE, "file has no commit record", f"{stream_id}/{rel}"
                    )
                    ok = False

        readable = [commit for commit in commits if commit.chunk_id in intact]
        if not _verify_payload_refs(stream_dir, descriptor, readable, result, stream_id):
            ok = False
    return ok


def _verify_payload_refs(
    root: Path,
    descriptor: StreamDescriptor,
    commits: list[ChunkCommit],
    result: VerificationResult,
    stream_id: str,
) -> bool:
    """payload_ref must be non-null iff the capture level is transport_payload."""
    ok = True
    expects = descriptor.expects_payload_artifact
    for commit in commits:
        packets_file = root / commit.packets.path
        if not packets_file.is_file():
            continue
        try:
            with packets_file.open("rb") as handle:
                table = pa.ipc.open_stream(handle).read_all()
            refs = table.column("payload_ref").to_pylist()
        except (pa.ArrowException, OSError, KeyError, TypeError, ValueError) as exc:
            # A corrupt artifact is a finding, never an exception out of a
            # function whose job is to report findings.
            result.add(
                Finding.UNREADABLE_CHUNK_ARTIFACT,
                f"chunk {commit.chunk_id}: {exc}",
                stream_id,
            )
            ok = False
            continue
        if not expects:
            if any(ref is not None for ref in refs):
                result.add(
                    Finding.PAYLOAD_REF_INVALID,
                    f"chunk {commit.chunk_id} has a non-null payload_ref at "
                    f"raw_capture_level={descriptor.acquisition.raw_capture_level.value}",
                    stream_id,
                )
                ok = False
            continue
        if any(ref is None for ref in refs):
            result.add(
                Finding.PAYLOAD_REF_INVALID,
                f"chunk {commit.chunk_id} has a null payload_ref at transport_payload",
                stream_id,
            )
            ok = False
            continue
        if commit.payloads is None:
            continue
        payload_file = root / commit.payloads.path
        if not payload_file.is_file():
            # Already reported as CHUNK_ARTIFACT_MISSING; do not raise on top
            # of it, and do not treat the refs as verified either.
            ok = False
            continue
        blob = payload_file.read_bytes()
        for ref in refs:
            # The reference must name the chunk's own payload file. Otherwise a
            # forged ref could point at another file entirely and still resolve.
            if ref["file"] != commit.payloads.path:
                result.add(
                    Finding.PAYLOAD_REF_INVALID,
                    f"payload_ref.file {ref['file']!r} is not {commit.payloads.path!r}",
                    stream_id,
                )
                ok = False
                break
            try:
                read_at(blob, PayloadRef(ref["file"], int(ref["offset"]), int(ref["length"])))
            except PayloadFramingError as exc:
                result.add(Finding.PAYLOAD_REF_INVALID, str(exc), stream_id)
                ok = False
                break
    return ok


def is_completed(paths: PackagePaths) -> bool:
    """The single completion entry point. A valid manifest pair is not enough."""
    return verify_package(paths).is_completed
