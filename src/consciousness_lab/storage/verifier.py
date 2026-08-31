"""Package verification and the completion predicate (v2 §9; D21, D22, D30).

One predicate, eight conditions, evaluating the **effective** outcome. There is
deliberately only one definition of completion in this codebase.

The referential relations enforced here are V01-V14 of `PACKAGE_INTEGRITY_V2.md`:

    V01 chain: prev_record_sha256 == SHA-256 of the previous record's canonical bytes
    V02 chunk_id strictly increases along the chain
    V03 every committed artifact exists at its deterministic path and hashes right
    V04 no raw artifact exists that no commit references
    V05 artifact_sha256 key set matches the capture level
    V06 packet_seq strictly increasing within a chunk and across the chain
    V07 sample/observation references, dense key identity, sparse triple uniqueness
    V08 frame packet_seq multiset == packets packet_seq multiset
    V09 physical streams declared in run.json; each has a valid stream_close.json;
        every required id is present and CLEAN
    V10 control_sha256 key set equals the derived expected set, both directions
    V11 every JSONL record is one complete canonical object plus one newline
    V12 every canonical document is canonical ON DISK, byte for byte
    V13 no immutable file outside the defined layout; schemas/ == referenced ids
    V14 lifecycle and events records verify their own record_sha256 (pre-seal layer)

Verification fails closed and reports *why*, as a list of structured findings,
rather than collapsing every distinct failure into one exception.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session.lifecycle import parse_records, summarize
from consciousness_lab.session.model import (
    SCHEMA_MAJOR,
    ChunkCommit,
    ClosureCondition,
    EventRecord,
    LifecycleRecord,
    LifecycleState,
    Manifest,
    RecordingOutcome,
    Run,
    StreamCloseStatus,
    load_on_disk,
)
from consciousness_lab.storage import canonical_json, package_layout
from consciousness_lab.storage.checksums import find_incomplete, sha256_bytes, sha256_file
from consciousness_lab.storage.package_layout import read_manifest
from consciousness_lab.storage.paths import PackagePaths
from consciousness_lab.storage.safe_paths import (
    UnsafePathError,
    find_symlinks,
    resolve_within,
)
from consciousness_lab.storage.stream_state import (
    REQUIRED_STREAM_FILES,
    PhysicalStreamState,
    read_all_physical_streams,
)


class Finding(StrEnum):
    """Every distinct way verification can fail."""

    # condition 1 — the manifest pair
    MISSING_MANIFEST = "missing_manifest"
    MISSING_MANIFEST_SHA = "missing_manifest_sha"
    BAD_MANIFEST_HASH = "bad_manifest_hash"
    UNREADABLE_MANIFEST = "unreadable_manifest"
    SCHEMA_VERSION_UNSUPPORTED = "schema_version_unsupported"

    # condition 2 — the package file set is closed and sealed
    CONTROL_SET_MISMATCH = "control_set_mismatch"
    CONTROL_FILE_MISSING = "control_file_missing"
    CONTROL_HASH_MISMATCH = "control_hash_mismatch"
    SCHEMA_SET_MISMATCH = "schema_set_mismatch"
    UNEXPECTED_FILE = "unexpected_file"
    SYMLINK_IN_PACKAGE = "symlink_in_package"
    UNSAFE_PATH = "unsafe_path"

    # condition 3 — the sealed logs
    BROKEN_LIFECYCLE_SEAL = "broken_lifecycle_seal"
    BROKEN_EVENTS_SEAL = "broken_events_seal"
    NOT_CANONICAL_ON_DISK = "not_canonical_on_disk"
    REMOVED_FIELD_PRESENT = "removed_field_present"
    INVALID_ALLOCATION = "invalid_allocation"

    # condition 4 — the sealed outcome
    NOT_CLEANLY_CLOSED = "not_cleanly_closed"
    SEALED_OUTCOME_NOT_COMPLETED = "sealed_outcome_not_completed"

    # condition 5 — stream set agreement and closure
    UNREADABLE_RUN = "unreadable_run"
    STREAM_NOT_DECLARED = "stream_not_declared"
    MISSING_STREAM_CLOSE = "missing_stream_close"
    INVALID_STREAM_CLOSE = "invalid_stream_close"
    REQUIRED_STREAM_MISSING = "required_stream_missing"
    REQUIRED_STREAM_UNCLEAN = "required_stream_unclean"

    # condition 6 — no incomplete write markers
    INCOMPLETE_FILE_PRESENT = "incomplete_file_present"

    # condition 7 — physical raw integrity
    MISSING_STREAM_STRUCTURE = "missing_stream_structure"
    MISSING_DESCRIPTOR = "missing_descriptor"
    UNREADABLE_DESCRIPTOR = "unreadable_descriptor"
    DESCRIPTOR_STREAM_ID_MISMATCH = "descriptor_stream_id_mismatch"
    BROKEN_CHUNK_CHAIN = "broken_chunk_chain"
    CHAIN_ORDER_INVALID = "chain_order_invalid"
    CHUNK_ARTIFACT_INVALID = "chunk_artifact_invalid"
    ARROW_SCHEMA_INVALID = "arrow_schema_invalid"
    OBSERVATION_ROW_INVALID = "observation_row_invalid"
    ROW_REFERENCE_INVALID = "row_reference_invalid"
    PAYLOAD_FRAMING_INVALID = "payload_framing_invalid"
    UNREADABLE_PACKETS_ARTIFACT = "unreadable_packets_artifact"
    ORPHAN_FILE = "orphan_file"

    # condition 8 — the effective outcome
    ANNOTATION_INTEGRITY_INDETERMINATE = "annotation_integrity_indeterminate"
    ANNOTATION_REJECTED = "annotation_rejected"
    EFFECTIVE_OUTCOME_NOT_COMPLETED = "effective_outcome_not_completed"


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
    """Parse and hash-chain-verify one stream's chunk index.

    Kept as the reader's entry point; the work lives in ``stream_state`` so
    there is one definition of what "committed" means.
    """
    from consciousness_lab.storage.stream_state import read_chunk_chain

    records, error = read_chunk_chain(paths.stream(stream_id).chunks_index)
    return [record.model for record in records], error


def verify_package(paths: PackagePaths) -> VerificationResult:
    """Verify a package and evaluate the eight-condition completion predicate."""
    result = VerificationResult(session_id=paths.root.name)
    physical = read_all_physical_streams(paths)
    stream_ids = sorted(physical)

    manifest = _condition_1(paths, result)
    events_bytes = paths.events.read_bytes() if paths.events.is_file() else b""
    schema_ids, schema_error = package_layout.referenced_schema_ids(events_bytes)
    result.conditions[2] = _condition_2(paths, manifest, result, stream_ids, schema_ids)
    result.conditions[3] = _condition_3(paths, manifest, events_bytes, schema_error, result)
    result.conditions[4] = _condition_4(paths, manifest, result)
    result.conditions[5] = _condition_5(paths, physical, result)

    incomplete = find_incomplete(paths.root)
    for path in incomplete:
        result.add(
            Finding.INCOMPLETE_FILE_PRESENT,
            "incomplete write marker present",
            path.relative_to(paths.root).as_posix(),
        )
    result.conditions[6] = not incomplete

    result.conditions[7] = _condition_7(paths, physical, result)
    result.conditions[8] = _condition_8(paths, result)
    return result


def _condition_1(paths: PackagePaths, result: VerificationResult) -> Manifest | None:
    """manifest.json exists, manifest.sha256 matches it, schema major is 2."""
    manifest_ok = True
    manifest: Manifest | None = None
    if not paths.manifest.exists():
        result.add(Finding.MISSING_MANIFEST, "manifest.json is absent")
        manifest_ok = False
    elif not paths.manifest_sha256.exists():
        result.add(Finding.MISSING_MANIFEST_SHA, "manifest.sha256 is absent")
        manifest_ok = False
    else:
        raw = paths.manifest.read_bytes()
        recorded = paths.manifest_sha256.read_text(encoding="utf-8").strip()
        if recorded != sha256_bytes(raw):
            result.add(Finding.BAD_MANIFEST_HASH, "manifest.sha256 does not match manifest.json")
            manifest_ok = False
        canonical_error = package_layout.canonical_document_error(raw)
        if canonical_error is not None:
            result.add(Finding.NOT_CANONICAL_ON_DISK, f"manifest.json {canonical_error}")
            manifest_ok = False
        else:
            # Minor-version tolerance ignores an unknown *new* field, which is
            # deliberate. A key v2 DELETED is not a future field: it is a
            # resurrected v1 summary that would contradict the authority the
            # fact actually lives in, so it fails closed (§7, D29).
            parsed = canonical_json.loads(raw)
            resurrected = sorted(
                set(parsed) & package_layout.REMOVED_MANIFEST_KEYS
                if isinstance(parsed, dict)
                else ()
            )
            for key in resurrected:
                result.add(
                    Finding.REMOVED_FIELD_PRESENT,
                    f"manifest.json carries {key!r}, which v2 removed",
                )
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
    return manifest


def _condition_2(
    paths: PackagePaths,
    manifest: Manifest | None,
    result: VerificationResult,
    stream_ids: list[str],
    schema_ids: set[str],
) -> bool:
    """The package file set is closed and sealed (V10, V13).

    The expected control set is DERIVED from the physical stream directories and
    the schema ids the sealed events log references — never read back from the
    manifest it is being compared against. Equality in both directions.
    """
    ok = manifest is not None

    for link in find_symlinks(paths.root):
        # A symlink lets an artifact be moved out of the package and faked back
        # in: is_file(), stat() and sha256 all follow it and all succeed.
        result.add(
            Finding.SYMLINK_IN_PACKAGE,
            "sealed package content may not contain a symlink",
            link.relative_to(paths.root).as_posix(),
        )
        ok = False

    # allocation.json is the authority for package identity and for the schema
    # major a reader must implement. Hashing it is not validating it.
    allocation_problem = package_layout.allocation_error(paths)
    if allocation_problem is not None:
        result.add(Finding.INVALID_ALLOCATION, allocation_problem, "allocation.json")
        ok = False

    for offender in package_layout.noncanonical_documents(paths, stream_ids):
        result.add(
            Finding.NOT_CANONICAL_ON_DISK,
            "structural document is not canonical on disk",
            offender,
        )
        ok = False

    scan = package_layout.scan_layout(paths, schema_ids)
    for name in scan.unexpected:
        result.add(
            Finding.UNEXPECTED_FILE,
            "present in a sealed package but not part of the v2 layout",
            name,
        )
        ok = False
    for schema_id in scan.unreferenced_schemas:
        result.add(
            Finding.SCHEMA_SET_MISMATCH,
            "schema snapshot is referenced by no sealed event",
            f"schemas/{schema_id}.json",
        )
        ok = False
    for schema_id in scan.missing_schemas:
        result.add(
            Finding.SCHEMA_SET_MISMATCH,
            "sealed events reference a schema with no snapshot on disk",
            f"schemas/{schema_id}.json",
        )
        ok = False

    if manifest is None:
        return False

    expected = package_layout.expected_control_paths(stream_ids, schema_ids)
    recorded = set(manifest.control_sha256)
    for missing in sorted(set(expected) - recorded):
        result.add(Finding.CONTROL_SET_MISMATCH, "control file is not sealed", missing)
        ok = False
    for extra in sorted(recorded - set(expected)):
        result.add(
            Finding.CONTROL_SET_MISMATCH,
            "control_sha256 seals a path the derived control set does not contain",
            extra,
        )
        ok = False

    for entry, digest in sorted(manifest.control_sha256.items()):
        try:
            target = resolve_within(paths.root, entry)
        except UnsafePathError as exc:
            result.add(Finding.UNSAFE_PATH, str(exc), entry)
            ok = False
            continue
        if not target.is_file():
            result.add(Finding.CONTROL_FILE_MISSING, "sealed control file is absent", entry)
            ok = False
            continue
        if sha256_file(target) != digest:
            result.add(Finding.CONTROL_HASH_MISMATCH, "control file hash mismatch", entry)
            ok = False
    return ok


def _condition_3(
    paths: PackagePaths,
    manifest: Manifest | None,
    events_bytes: bytes,
    schema_error: str | None,
    result: VerificationResult,
) -> bool:
    """The sealed lifecycle prefix and the events log (V11, V12, V14)."""
    if manifest is None:
        return False
    if not paths.lifecycle.exists():
        result.add(Finding.BROKEN_LIFECYCLE_SEAL, "lifecycle.jsonl is absent")
        return False

    ok = True
    raw = paths.lifecycle.read_bytes()
    sealed_len = manifest.lifecycle_seal.sealed_len
    if len(raw) < sealed_len:
        result.add(Finding.BROKEN_LIFECYCLE_SEAL, "lifecycle.jsonl is shorter than its seal")
        return False
    prefix = raw[:sealed_len]
    if sha256_bytes(prefix) != manifest.lifecycle_seal.sealed_sha256:
        result.add(Finding.BROKEN_LIFECYCLE_SEAL, "sealed lifecycle prefix hash mismatch")
        return False

    # The file hash proves the bytes are the sealed bytes. It does NOT prove
    # each record is canonical, valid, or that its own record_sha256 still
    # verifies — and a tamperer who can rewrite the manifest can restore the
    # file hash. So every record inside the sealed region is checked.
    error = package_layout.verify_jsonl_region(prefix, LifecycleRecord, require_record_hash=True)
    if error is not None:
        result.add(Finding.BROKEN_LIFECYCLE_SEAL, f"lifecycle {error}")
        ok = False

    if sha256_bytes(events_bytes) != manifest.events_sha256:
        result.add(Finding.BROKEN_EVENTS_SEAL, "events seal hash mismatch")
        return False
    if schema_error is not None:
        result.add(Finding.BROKEN_EVENTS_SEAL, schema_error)
        ok = False
    error = package_layout.verify_jsonl_region(events_bytes, EventRecord, require_record_hash=True)
    if error is not None:
        result.add(Finding.BROKEN_EVENTS_SEAL, f"events {error}")
        ok = False
    return ok


def _condition_4(
    paths: PackagePaths, manifest: Manifest | None, result: VerificationResult
) -> bool:
    """Within the sealed prefix: CLOSED / CLEAN / sealed COMPLETED."""
    if manifest is None or not result.conditions.get(3, False):
        return False
    prefix = paths.lifecycle.read_bytes()[: manifest.lifecycle_seal.sealed_len]
    summary = summarize(parse_records(prefix))
    result.sealed_outcome = summary.sealed_outcome
    if summary.terminal_state is not LifecycleState.CLOSED:
        result.add(Finding.NOT_CLEANLY_CLOSED, f"terminal state is {summary.terminal_state}")
        return False
    if summary.closure_condition is not ClosureCondition.CLEAN:
        result.add(Finding.NOT_CLEANLY_CLOSED, f"closure condition is {summary.closure_condition}")
        return False
    if summary.sealed_outcome is not RecordingOutcome.COMPLETED:
        result.add(
            Finding.SEALED_OUTCOME_NOT_COMPLETED, f"sealed outcome is {summary.sealed_outcome}"
        )
        return False
    return True


def _condition_5(
    paths: PackagePaths, physical: dict[str, PhysicalStreamState], result: VerificationResult
) -> bool:
    """Stream set agreement and closure, in three parts (V09).

    a. every physical stream directory is declared in the run contract;
    b. every physical stream directory has a valid ``stream_close.json``;
    c. every required id has a directory whose close status is ``CLEAN``.

    A declared **optional** stream that was never opened has no directory and no
    close file; that is valid. A required stream recovered ``RECOVERED_UNCLEAN``
    fails this condition, as it should.
    """
    run: Run | None = None
    if paths.run.exists():
        raw = paths.run.read_bytes()
        error = package_layout.canonical_document_error(raw)
        if error is not None:
            result.add(Finding.UNREADABLE_RUN, f"run.json {error}")
        else:
            try:
                run = load_on_disk(Run, canonical_json.loads(raw))
            except ValueError as exc:
                result.add(Finding.UNREADABLE_RUN, f"run.json could not be parsed ({exc})")
    else:
        result.add(Finding.UNREADABLE_RUN, "run.json is absent")
    result.run = run
    if run is None:
        return False

    ok = True
    declared = set(run.required_streams) | set(run.optional_streams)
    for stream_id in sorted(physical):
        state = physical[stream_id]
        if stream_id not in declared:
            result.add(
                Finding.STREAM_NOT_DECLARED,
                "raw directory is not declared in run.required_streams or optional_streams",
                stream_id,
            )
            ok = False
        if state.close_error is not None:
            result.add(Finding.INVALID_STREAM_CLOSE, state.close_error, stream_id)
            ok = False
        elif not state.close_present:
            result.add(
                Finding.MISSING_STREAM_CLOSE,
                "stream has no durable stream_close.json",
                stream_id,
            )
            ok = False

    for stream_id in run.required_streams:
        required_state = physical.get(stream_id)
        if required_state is None or not required_state.directory_exists:
            result.add(
                Finding.REQUIRED_STREAM_MISSING,
                "required stream has no raw directory on disk",
                stream_id,
            )
            ok = False
            continue
        if required_state.close_status is not StreamCloseStatus.CLEAN:
            result.add(
                Finding.REQUIRED_STREAM_UNCLEAN,
                f"required stream closed {required_state.close_status}",
                stream_id,
            )
            ok = False
    return ok


def _condition_7(
    paths: PackagePaths, physical: dict[str, PhysicalStreamState], result: VerificationResult
) -> bool:
    """Physical raw integrity for every stream (V01-V08, V13 in the raw tree)."""
    ok = True
    for stream_id in sorted(physical):
        if not _verify_stream(paths, physical[stream_id], result):
            ok = False
    return ok


def _verify_stream(
    paths: PackagePaths, state: PhysicalStreamState, result: VerificationResult
) -> bool:
    """Verify one physical stream against the v2 contract."""
    stream_id = state.stream_id
    ok = True

    for name in REQUIRED_STREAM_FILES:
        if not (paths.stream(stream_id).root / name).is_file():
            result.add(Finding.MISSING_STREAM_STRUCTURE, f"{name} is absent", stream_id)
            ok = False

    if not state.descriptor_present:
        result.add(Finding.MISSING_DESCRIPTOR, "descriptor.json is absent", stream_id)
        return False
    if state.descriptor is None:
        result.add(Finding.UNREADABLE_DESCRIPTOR, state.descriptor_error or "unreadable", stream_id)
        return False
    if state.descriptor.stream_id != stream_id:
        result.add(
            Finding.DESCRIPTOR_STREAM_ID_MISMATCH,
            f"descriptor declares {state.descriptor.stream_id!r} in directory {stream_id!r}",
            stream_id,
        )
        ok = False

    if state.chain_error is not None:
        result.add(Finding.BROKEN_CHUNK_CHAIN, state.chain_error, stream_id)
        ok = False
    for order_error in state.order_errors:
        result.add(Finding.CHAIN_ORDER_INVALID, order_error, stream_id)
        ok = False

    for chunk in state.chunks:
        prefix = f"chunk {chunk.chunk_id}"
        if chunk.packet_error is not None:
            result.add(
                Finding.UNREADABLE_PACKETS_ARTIFACT, f"{prefix}: {chunk.packet_error}", stream_id
            )
            ok = False
        for detail in chunk.artifact_errors:
            result.add(Finding.CHUNK_ARTIFACT_INVALID, f"{prefix}: {detail}", stream_id)
            ok = False
        for detail in chunk.schema_errors:
            result.add(Finding.ARROW_SCHEMA_INVALID, f"{prefix}: {detail}", stream_id)
            ok = False
        for detail in chunk.row_semantics_errors:
            result.add(Finding.OBSERVATION_ROW_INVALID, f"{prefix}: {detail}", stream_id)
            ok = False
        for detail in chunk.reference_errors:
            result.add(Finding.ROW_REFERENCE_INVALID, f"{prefix}: {detail}", stream_id)
            ok = False
        for detail in chunk.payload_errors:
            result.add(Finding.PAYLOAD_FRAMING_INVALID, f"{prefix}: {detail}", stream_id)
            ok = False

    # Both directions. Missing committed artifacts are reported above; these are
    # files the layout does not define, and artifacts no commit record names.
    for name in state.unexpected_files:
        result.add(
            Finding.UNEXPECTED_FILE,
            "present in a raw stream directory but not part of the v2 layout",
            f"{stream_id}/{name}",
        )
        ok = False
    for name in state.orphan_artifacts:
        result.add(Finding.ORPHAN_FILE, "file has no commit record", f"{stream_id}/{name}")
        ok = False
    return ok


def _condition_8(paths: PackagePaths, result: VerificationResult) -> bool:
    """The effective outcome, failing closed on an indeterminate annotation log."""
    effective = annotations_mod.resolve_effective_outcome(
        sealed_outcome=result.sealed_outcome,
        annotations_path=paths.annotations,
        head_path=paths.annotations_head,
    )
    result.effective = effective
    if effective.status is annotations_mod.AnnotationStatus.INDETERMINATE:
        result.add(Finding.ANNOTATION_INTEGRITY_INDETERMINATE, effective.detail)
        return False
    ok = True
    for rejected in effective.rejected:
        result.add(
            Finding.ANNOTATION_REJECTED, f"annotation {rejected.seq} rejected: {rejected.detail}"
        )
        ok = False
    if effective.outcome is not RecordingOutcome.COMPLETED:
        result.add(
            Finding.EFFECTIVE_OUTCOME_NOT_COMPLETED, f"effective outcome is {effective.outcome}"
        )
        ok = False
    return ok


def is_completed(paths: PackagePaths) -> bool:
    """The single completion entry point. A valid manifest pair is not enough."""
    return verify_package(paths).is_completed
