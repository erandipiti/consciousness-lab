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
from pydantic import BaseModel

from consciousness_lab.session import annotations as annotations_mod
from consciousness_lab.session.finalizer import read_manifest
from consciousness_lab.session.lifecycle import parse_records, summarize
from consciousness_lab.session.model import (
    SCHEMA_MAJOR,
    ChunkCommit,
    ClosureCondition,
    EventRecord,
    LifecycleRecord,
    LifecycleState,
    Manifest,
    ManifestStream,
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
from consciousness_lab.storage.stream_state import (
    REQUIRED_STREAM_FILES,
    PhysicalStreamState,
    read_all_physical_streams,
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
    MANIFEST_STREAM_MISSING_RAW = "manifest_stream_missing_raw"
    RAW_STREAM_MISSING_MANIFEST = "raw_stream_missing_manifest"
    DUPLICATE_MANIFEST_STREAM = "duplicate_manifest_stream"
    REQUIRED_FLAG_MISMATCH = "required_flag_mismatch"
    MISSING_DESCRIPTOR = "missing_descriptor"
    DESCRIPTOR_STREAM_ID_MISMATCH = "descriptor_stream_id_mismatch"
    MANIFEST_DESCRIPTOR_HASH_MISMATCH = "manifest_descriptor_hash_mismatch"
    CHUNK_DESCRIPTOR_HASH_MISMATCH = "chunk_descriptor_hash_mismatch"
    CHUNK_COUNT_MISMATCH = "chunk_count_mismatch"
    CHAIN_HEAD_MISMATCH = "chain_head_mismatch"
    PACKET_RANGE_MISMATCH = "packet_range_mismatch"
    MISSING_STREAM_STRUCTURE = "missing_stream_structure"
    SIDECAR_MISMATCH = "sidecar_mismatch"
    PACKET_SUMMARY_MISMATCH = "packet_summary_mismatch"
    UNREADABLE_PACKETS_ARTIFACT = "unreadable_packets_artifact"
    ARTIFACT_PATH_NOT_CANONICAL = "artifact_path_not_canonical"
    ARTIFACT_PATH_REUSED = "artifact_path_reused"
    CHAIN_ORDER_INVALID = "chain_order_invalid"
    PAYLOAD_KEY_PRESENCE_INVALID = "payload_key_presence_invalid"
    ROW_REFERENCE_INVALID = "row_reference_invalid"
    DUPLICATE_INVENTORY_PATH = "duplicate_inventory_path"
    INVENTORY_BYTES_MISMATCH = "inventory_bytes_mismatch"
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


def _verify_sealed_jsonl(
    raw: bytes, model: type[BaseModel], label: str, result: VerificationResult
) -> bool:
    """Every record in a sealed JSONL region parses, verifies and validates."""
    finding = Finding.BROKEN_LIFECYCLE_SEAL if label == "lifecycle" else Finding.BROKEN_EVENTS_SEAL
    for number, line in enumerate(raw.split(b"\n")):
        if not line.strip():
            continue
        try:
            obj = canonical_json.loads(line)
        except (canonical_json.CanonicalizationError, ValueError) as exc:
            result.add(finding, f"{label} line {number} is not parseable ({exc})")
            return False
        if not isinstance(obj, dict) or not canonical_json.verify_record(obj):
            result.add(finding, f"{label} line {number}: record_sha256 does not verify")
            return False
        try:
            load_on_disk(model, obj)
        except ValueError as exc:
            result.add(finding, f"{label} line {number} is invalid on disk ({exc})")
            return False
    return True


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
        # The inventory is a BIJECTION over in-scope files, not a set. Collapsing
        # it with `{e.path for e in ...}` would let a duplicate entry disappear.
        inventory_paths = [entry.path for entry in manifest.inventory]
        duplicates = sorted({p for p in inventory_paths if inventory_paths.count(p) > 1})
        for duplicate in duplicates:
            result.add(
                Finding.DUPLICATE_INVENTORY_PATH,
                "path appears more than once in the manifest inventory",
                duplicate,
            )
            inventory_ok = False

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
            # bytes is an independent leaf; a matching SHA does not validate it.
            actual_bytes = target.stat().st_size
            if entry.bytes != actual_bytes:
                result.add(
                    Finding.INVENTORY_BYTES_MISMATCH,
                    f"inventory declares {entry.bytes} bytes, file holds {actual_bytes}",
                    entry.path,
                )
                inventory_ok = False

        listed = set(inventory_paths)
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
        prefix_bytes = raw[:sealed_len]
        if len(raw) < sealed_len:
            result.add(Finding.BROKEN_LIFECYCLE_SEAL, "lifecycle.jsonl is shorter than its seal")
        elif sha256_bytes(raw[:sealed_len]) != manifest.lifecycle_seal.sealed_sha256:
            result.add(Finding.BROKEN_LIFECYCLE_SEAL, "sealed lifecycle prefix hash mismatch")
        else:
            seal_ok = True
        # The file hash proves the bytes are the sealed bytes. It does NOT prove
        # each record's own record_sha256 still verifies, and a tamperer who can
        # rewrite the manifest can restore the file hash. So every record inside
        # the sealed region is checked individually.
        if seal_ok and not _verify_sealed_jsonl(prefix_bytes, LifecycleRecord, "lifecycle", result):
            seal_ok = False
        if manifest.events_seal.bytes or paths.events.exists():
            events = paths.events.read_bytes() if paths.events.exists() else b""
            if sha256_bytes(events) != manifest.events_seal.sha256:
                result.add(Finding.BROKEN_EVENTS_SEAL, "events seal hash mismatch")
                seal_ok = False
            elif not _verify_sealed_jsonl(events, EventRecord, "events", result):
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
    # Condition 5 owns SEMANTIC required-stream closure: run declares it, the
    # manifest agrees it is required, and it closed CLEAN — and the stream
    # physically exists, because a manifest entry alone proves nothing about
    # what is on disk (CL-002B-R1).
    physical = read_all_physical_streams(paths)
    required_ok = run is not None and manifest is not None
    if run is not None and manifest is not None:
        by_id = {s.stream_id: s for s in manifest.streams}
        declared_required = set(run.required_streams)
        for stream_id in run.required_streams:
            stream_entry = by_id.get(stream_id)
            if stream_entry is None:
                result.add(Finding.REQUIRED_STREAM_MISSING, "required stream absent", stream_id)
                required_ok = False
                continue
            if stream_id not in physical or not physical[stream_id].directory_exists:
                result.add(
                    Finding.MANIFEST_STREAM_MISSING_RAW,
                    "required stream has no raw directory on disk",
                    stream_id,
                )
                required_ok = False
            if stream_entry.close_status is not StreamCloseStatus.CLEAN:
                result.add(
                    Finding.REQUIRED_STREAM_UNCLEAN,
                    f"required stream closed {stream_entry.close_status}",
                    stream_id,
                )
                required_ok = False
        # The required flag is fully determined by run.json, for every stream.
        for stream_summary in manifest.streams:
            expected = stream_summary.stream_id in declared_required
            if stream_summary.required is not expected:
                result.add(
                    Finding.REQUIRED_FLAG_MISMATCH,
                    f"manifest says required={stream_summary.required}, "
                    f"run.json implies {expected}",
                    stream_summary.stream_id,
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

    # --- Condition 7: physical raw integrity, reconciled with the manifest ---
    chains_ok = _verify_streams(paths, manifest, result, physical)
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
    paths: PackagePaths,
    manifest: Manifest | None,
    result: VerificationResult,
    physical: dict[str, PhysicalStreamState],
) -> bool:
    """Condition 7: physical raw integrity, reconciled against the manifest.

    Every summary the manifest carries about a stream is re-derived from disk
    and compared. The manifest is a *summary of* the raw data, never evidence
    that the raw data exists (CL-002B-R1).
    """
    ok = True
    if manifest is None:
        # No manifest yet (an unfinalized or interrupted package). The
        # manifest-reconciliation checks do not apply, but the physical checks
        # still do: recovery relies on orphan and chain reporting here.
        for stream_id in sorted(physical):
            if not _verify_stream(paths, None, physical[stream_id], result):
                ok = False
        return False

    # --- bidirectional stream-set reconciliation ---------------------------
    manifest_ids = [entry.stream_id for entry in manifest.streams]
    seen: set[str] = set()
    for stream_id in manifest_ids:
        if stream_id in seen:
            result.add(
                Finding.DUPLICATE_MANIFEST_STREAM, "stream listed twice in the manifest", stream_id
            )
            ok = False
        seen.add(stream_id)

    physical_ids = set(physical)
    for stream_id in sorted(seen - physical_ids):
        result.add(
            Finding.MANIFEST_STREAM_MISSING_RAW,
            "manifest describes a stream with no raw directory",
            stream_id,
        )
        ok = False
    for stream_id in sorted(physical_ids - seen):
        result.add(
            Finding.RAW_STREAM_MISSING_MANIFEST,
            "raw directory is not described by the manifest",
            stream_id,
        )
        ok = False

    for entry in manifest.streams:
        state = physical.get(entry.stream_id)
        if state is None:
            continue  # already reported above
        if not _verify_stream(paths, entry, state, result):
            ok = False
    return ok


def _verify_stream(
    paths: PackagePaths,
    entry: ManifestStream | None,
    state: PhysicalStreamState,
    result: VerificationResult,
) -> bool:
    """Verify one physical stream, reconciling it against its manifest summary.

    ``entry`` is ``None`` for a package with no manifest yet: the physical
    checks still run, only the summary comparisons are skipped. Recovery relies
    on the orphan and chain reporting here.
    """
    stream_id = state.stream_id
    stream_dir = paths.stream(stream_id).root
    ok = True

    # --- structural files ---------------------------------------------------
    for name in REQUIRED_STREAM_FILES:
        if not (stream_dir / name).is_file():
            result.add(Finding.MISSING_STREAM_STRUCTURE, f"{name} is absent", stream_id)
            ok = False

    # --- descriptor ---------------------------------------------------------
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
    if entry is not None and state.descriptor_sha256 != entry.descriptor_sha256:
        result.add(
            Finding.MANIFEST_DESCRIPTOR_HASH_MISMATCH,
            "manifest descriptor_sha256 does not match the stored descriptor bytes",
            stream_id,
        )
        ok = False

    # --- chunk chain --------------------------------------------------------
    if state.chain_error is not None:
        result.add(Finding.BROKEN_CHUNK_CHAIN, state.chain_error, stream_id)
        ok = False
    for order_error in state.order_errors:
        # Chain-level, not per-chunk: every chunk of a reversed chain still
        # matches its own artifact (matrix rows R24, R25).
        result.add(Finding.CHAIN_ORDER_INVALID, order_error, stream_id)
        ok = False

    if entry is not None:
        if entry.chunk_count != state.chunk_count:
            result.add(
                Finding.CHUNK_COUNT_MISMATCH,
                f"manifest says {entry.chunk_count} chunks, the chain holds {state.chunk_count}",
                stream_id,
            )
            ok = False
        if entry.chunk_chain_head_sha256 != state.chain_head_sha256:
            result.add(
                Finding.CHAIN_HEAD_MISMATCH,
                "manifest chunk_chain_head_sha256 is not the chain's final record hash",
                stream_id,
            )
            ok = False
        if (entry.first_packet_seq, entry.last_packet_seq) != (
            state.first_packet_seq,
            state.last_packet_seq,
        ):
            result.add(
                Finding.PACKET_RANGE_MISMATCH,
                f"manifest packet range ({entry.first_packet_seq}, {entry.last_packet_seq}) "
                f"is not ({state.first_packet_seq}, {state.last_packet_seq})",
                stream_id,
            )
            ok = False

    # --- artifacts referenced by each commit --------------------------------
    committed_paths: set[str] = set()
    intact: set[int] = {chunk.record.chunk_id for chunk in state.chunks}
    seen_artifact_paths: dict[str, int] = {}
    for chunk in state.chunks:
        commit = chunk.record.model
        chunk_id = commit.chunk_id

        # --- the summary must match the PHYSICAL packet rows (B2) -----------
        if chunk.packet_error is not None:
            result.add(
                Finding.UNREADABLE_PACKETS_ARTIFACT,
                f"chunk {chunk_id}: {chunk.packet_error}",
                stream_id,
            )
            ok = False
            intact.discard(chunk_id)
        elif (commit.first_packet_seq, commit.last_packet_seq) != (
            chunk.physical_first_packet_seq,
            chunk.physical_last_packet_seq,
        ):
            # Checked for EVERY chunk, not only the stream endpoints, so a
            # forged middle chunk cannot hide behind correct-looking ends.
            result.add(
                Finding.PACKET_SUMMARY_MISMATCH,
                f"chunk {chunk_id} claims packets "
                f"({commit.first_packet_seq}, {commit.last_packet_seq}) but the artifact "
                f"holds ({chunk.physical_first_packet_seq}, {chunk.physical_last_packet_seq})",
                stream_id,
            )
            ok = False

        # --- payload KEY presence, which pairwise equality cannot see -------
        # Both copies carrying an explicit null agree with each other and both
        # violate §12.2, which requires the key omitted (matrix row R12).
        expects_payload = state.descriptor.expects_payload_artifact
        if chunk.record.payloads_key_present != expects_payload:
            result.add(
                Finding.PAYLOAD_KEY_PRESENCE_INVALID,
                f"chunk {chunk_id}: payloads key is "
                f"{'present' if chunk.record.payloads_key_present else 'absent'} at "
                f"raw_capture_level={state.descriptor.acquisition.raw_capture_level.value}",
                stream_id,
            )
            ok = False
        sidecar_record = state.sidecars.get(chunk_id)
        if sidecar_record is not None and sidecar_record.payloads_key_present != expects_payload:
            result.add(
                Finding.PAYLOAD_KEY_PRESENCE_INVALID,
                f"chunk {chunk_id} sidecar: payloads key presence violates §12.2",
                stream_id,
            )
            ok = False

        # --- structural foreign keys from samples/observations to packets ---
        for reference_error in chunk.reference_errors:
            result.add(
                Finding.ROW_REFERENCE_INVALID,
                f"chunk {chunk_id}: {reference_error}",
                stream_id,
            )
            ok = False

        # --- artifact paths must be this chunk's own, and used once ---------
        for kind, artifact in (
            ("packets", commit.packets),
            ("observations", commit.observations),
            ("samples", commit.samples),
        ):
            expected_path = f"{kind}/{chunk_id:06d}.arrow"
            if artifact.path != expected_path:
                result.add(
                    Finding.ARTIFACT_PATH_NOT_CANONICAL,
                    f"chunk {chunk_id} {kind} artifact is {artifact.path!r}, "
                    f"expected {expected_path!r}",
                    stream_id,
                )
                ok = False
        if commit.payloads is not None:
            expected_payload = f"payloads/{chunk_id:06d}.bin"
            if commit.payloads.path != expected_payload:
                result.add(
                    Finding.ARTIFACT_PATH_NOT_CANONICAL,
                    f"chunk {chunk_id} payload artifact is {commit.payloads.path!r}, "
                    f"expected {expected_payload!r}",
                    stream_id,
                )
                ok = False

        if commit.descriptor_sha256 != state.descriptor_sha256:
            result.add(
                Finding.CHUNK_DESCRIPTOR_HASH_MISMATCH,
                f"chunk {chunk_id} was written under a different descriptor",
                stream_id,
            )
            ok = False
        artifacts = [commit.packets, commit.observations, commit.samples]
        if commit.payloads is not None:
            artifacts.append(commit.payloads)
        if state.descriptor.expects_payload_artifact and commit.payloads is None:
            result.add(
                Finding.MISSING_PAYLOAD_ARTIFACT,
                f"chunk {chunk_id} has no payload artifact",
                stream_id,
            )
            ok = False
        if not state.descriptor.expects_payload_artifact and commit.payloads is not None:
            result.add(
                Finding.UNEXPECTED_PAYLOAD_ARTIFACT,
                f"chunk {chunk_id} carries a payload artifact at "
                f"raw_capture_level={state.descriptor.acquisition.raw_capture_level.value}",
                stream_id,
            )
            ok = False
        for artifact in artifacts:
            committed_paths.add(artifact.path)
            owner = seen_artifact_paths.setdefault(artifact.path, chunk_id)
            if owner != chunk_id:
                result.add(
                    Finding.ARTIFACT_PATH_REUSED,
                    f"{artifact.path} is claimed by chunks {owner} and {chunk_id}",
                    stream_id,
                )
                ok = False
            try:
                target = resolve_within(stream_dir, artifact.path)
            except UnsafePathError as exc:
                result.add(Finding.UNSAFE_PATH, str(exc), f"{stream_id}/{artifact.path}")
                ok = False
                intact.discard(chunk_id)
                continue
            if not target.is_file():
                result.add(Finding.CHUNK_ARTIFACT_MISSING, artifact.path, stream_id)
                ok = False
                intact.discard(chunk_id)
            elif sha256_file(target) != artifact.sha256:
                result.add(Finding.CHUNK_ARTIFACT_HASH_MISMATCH, artifact.path, stream_id)
                ok = False
                intact.discard(chunk_id)

    # --- sidecars must agree with the authoritative chain -------------------
    # chunks.jsonl is the commit log; a sidecar is a convenience copy. If the
    # two can disagree, a package carries two contradictory accounts of the
    # same chunk, which is the defect class this ticket exists to close.
    for error in state.sidecar_errors:
        result.add(Finding.SIDECAR_MISMATCH, error, stream_id)
        ok = False
    for chunk in state.chunks:
        chunk_id = chunk.record.chunk_id
        sidecar = state.sidecars.get(chunk_id)
        if sidecar is None:
            result.add(
                Finding.SIDECAR_MISMATCH,
                f"chunk {chunk_id} has no {chunk_id:06d}.commit.json sidecar",
                stream_id,
            )
            ok = False
        elif not sidecar.same_record_as(chunk.record):
            # Canonical on-disk records, not parsed models. Two different
            # documents can normalize to the same ChunkCommit — an ignored
            # unknown field, or an explicit null where the contract requires the
            # key omitted — so model equality would call them identical.
            result.add(
                Finding.SIDECAR_MISMATCH,
                f"chunk {chunk_id} sidecar is not the same on-disk record as chunks.jsonl",
                stream_id,
            )
            ok = False
    chain_ids = {chunk.record.chunk_id for chunk in state.chunks}
    for chunk_id in sorted(set(state.sidecars) - chain_ids):
        result.add(
            Finding.ORPHAN_FILE,
            "sidecar has no commit record in chunks.jsonl",
            f"{stream_id}/{chunk_id:06d}.commit.json",
        )
        ok = False

    # Files under raw/ that no commit record names are orphans. Recovery
    # reports them; verification never adopts them.
    for kind in ("payloads", "packets", "observations", "samples"):
        directory = stream_dir / kind
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            rel = f"{kind}/{path.name}"
            if path.is_file() and rel not in committed_paths:
                result.add(Finding.ORPHAN_FILE, "file has no commit record", f"{stream_id}/{rel}")
                ok = False

    readable = [c.record.model for c in state.chunks if c.record.chunk_id in intact]
    if not _verify_payload_refs(stream_dir, state.descriptor, readable, result, stream_id):
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
