"""Regressions for the CL-002B-R2-R1 human-review findings.

Every test here mutates **physical bytes** and then recomputes every ordinary
hash a forger could recompute — the chain, ``control_sha256`` and the manifest
pair — so what the package fails on is conformance, not a stale SHA. That is the
whole point: a check that only survives because a digest went stale is not a
check at all.
"""

import hashlib
import time

import pytest

from consciousness_lab.session import recovery
from consciousness_lab.session.finalizer import FinalizationError, finalize
from consciousness_lab.session.lifecycle import parse_records, summarize
from consciousness_lab.session.model import (
    ClockReading,
    LifecycleState,
    RawCaptureLevel,
    RecordingOutcome,
    Run,
    StreamCloseStatus,
)
from consciousness_lab.session.writer import SessionWriter
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.checksums import sha256_bytes
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.reader import UnverifiedPackageError, open_package
from consciousness_lab.storage.verifier import Finding, verify_package
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSpec,
    build_descriptor,
)
from tests.conftest import (
    build_session,
    read_chunk_records,
    reseal_manifest,
    rewrite_chunk_chain,
)

STREAM = "synthetic.eeg"


# --- FINDING 1: allocation structural validity ------------------------------


@pytest.mark.parametrize(
    ("label", "key", "value"),
    [
        ("wrong major", "schema_version", "1.0"),
        ("invalid pseudonym", "participant_pseudonym", "NOT-A-PSEUDONYM"),
        ("identity mismatch", "session_id", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_an_invalid_allocation_cannot_verify_as_completed(
    data_root: DataRoot, label: str, key: str, value: str
) -> None:
    """Sealing a document proves identity, not validity.

    ``allocation.json`` is the authority for package identity and for the schema
    major a reader must implement. Hashed into ``control_sha256`` but never
    typed, it could violate the model and the package still looked complete.
    """
    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.allocation.read_bytes())
    obj[key] = value
    paths.allocation.write_bytes(canonical_json.canonicalize(obj))
    reseal_manifest(paths)  # every ordinary hash restored

    result = verify_package(paths)
    assert not result.is_completed, f"{label} verified clean"
    assert Finding.INVALID_ALLOCATION in result.findings()
    assert result.conditions[2] is False, "inside condition 2, not a ninth condition"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", "1.0"),
        ("participant_pseudonym", "NOT-A-PSEUDONYM"),
        ("session_id", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_reader_and_verifier_agree_on_an_invalid_allocation(
    data_root: DataRoot, key: str, value: str
) -> None:
    """Neither accepts what the other rejects."""
    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.allocation.read_bytes())
    obj[key] = value
    paths.allocation.write_bytes(canonical_json.canonicalize(obj))
    reseal_manifest(paths)

    assert not verify_package(paths).is_completed
    with pytest.raises((UnverifiedPackageError, Exception)):
        open_package(paths)


def test_the_predicate_still_has_exactly_eight_conditions(data_root: DataRoot) -> None:
    built = build_session(data_root)
    assert sorted(verify_package(built.allocated.paths).conditions) == [1, 2, 3, 4, 5, 6, 7, 8]


# --- FINDING 2: resurrected v1 ChunkCommit fields ---------------------------


@pytest.mark.parametrize(
    "removed",
    [
        "record_sha256",
        "first_packet_seq",
        "last_packet_seq",
        "descriptor_sha256",
        "packets",
        "observations",
        "samples",
        "payloads",
    ],
)
def test_a_resurrected_v1_chunk_field_fails_closed_after_a_full_reseal(
    data_root: DataRoot, removed: str
) -> None:
    """Minor-version tolerance covers unknown *future* fields, not deleted ones.

    The forger adds the field, rebuilds every ``prev_record_sha256`` down the
    chain, rehashes ``chunks.jsonl`` into ``control_sha256`` and reseals the
    manifest pair. The package must still be rejected.
    """
    built = build_session(data_root, chunks=3)
    paths = built.allocated.paths
    records = read_chunk_records(paths, STREAM)
    records[0][removed] = "resurrected"
    rewrite_chunk_chain(paths, STREAM, records)  # rebuilds the whole chain
    reseal_manifest(paths)  # rebuilds control_sha256 + manifest.sha256

    result = verify_package(paths)
    assert not result.is_completed, f"{removed} survived a full reseal"
    assert Finding.BROKEN_CHUNK_CHAIN in result.findings()


def test_an_unknown_future_chunk_field_is_still_tolerated(data_root: DataRoot) -> None:
    """The other direction: forward tolerance is deliberate and is preserved."""
    built = build_session(data_root, chunks=2)
    paths = built.allocated.paths
    records = read_chunk_records(paths, STREAM)
    records[0]["some_future_v2_1_field"] = "ignored"
    rewrite_chunk_chain(paths, STREAM, records)
    reseal_manifest(paths)
    assert verify_package(paths).is_completed


# --- FINDING 3: the manifest-only crash window ------------------------------


def _drop_sha_only(paths: PackagePaths) -> None:
    """The state a crash after step 10 and before step 11 actually leaves."""
    paths.manifest_sha256.unlink()


def test_state_b_manifest_without_its_digest_is_resumable(data_root: DataRoot) -> None:
    """A crash between step 10 and step 11 — the real boundary, not both deleted."""
    built = build_session(data_root)
    paths = built.allocated.paths
    manifest_bytes = paths.manifest.read_bytes()
    lifecycle_before = paths.lifecycle.read_bytes()
    _drop_sha_only(paths)

    report = recovery.scan(paths)
    assert report.state is recovery.StructuralState.INTERRUPTED_FINALIZATION
    assert report.can_resume_finalization

    recovery.resume_finalization(paths)
    assert paths.manifest.read_bytes() == manifest_bytes, "the manifest is never replaced"
    assert paths.lifecycle.read_bytes() == lifecycle_before, "history is never rewritten"
    assert paths.manifest_sha256.read_text().strip() == sha256_bytes(manifest_bytes)
    assert verify_package(paths).is_completed


def test_state_b_blocks_when_the_existing_manifest_disagrees(
    data_root: DataRoot,
) -> None:
    """Disagreement means BLOCKED, never replacement."""
    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj["events_sha256"] = "0" * 64
    forged = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(forged)
    _drop_sha_only(paths)

    with pytest.raises(recovery.ResumeError, match="disagrees"):
        recovery.resume_finalization(paths)
    assert paths.manifest.read_bytes() == forged, "not replaced"
    assert not paths.manifest_sha256.exists(), "and not sealed"


def test_state_c_a_lone_digest_fails_closed(data_root: DataRoot) -> None:
    """Impossible under the specified order, and no manifest is invented."""
    built = build_session(data_root)
    paths = built.allocated.paths
    paths.manifest.unlink()
    with pytest.raises(recovery.ResumeError, match=r"no manifest\.json"):
        recovery.resume_finalization(paths)
    assert not paths.manifest.exists()


def test_state_d_a_nonverifying_pair_rewrites_neither_file(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    stale = "f" * 64
    paths.manifest_sha256.write_text(stale + "\n", encoding="utf-8")
    manifest_bytes = paths.manifest.read_bytes()

    with pytest.raises(recovery.ResumeError, match="does not match"):
        recovery.resume_finalization(paths)
    assert paths.manifest.read_bytes() == manifest_bytes
    assert paths.manifest_sha256.read_text().strip() == stale


def test_state_a_neither_file_present_reconstructs_the_pair(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root)
    paths = built.allocated.paths
    lifecycle_before = paths.lifecycle.read_bytes()
    paths.manifest.unlink()
    paths.manifest_sha256.unlink()
    recovery.resume_finalization(paths)
    assert paths.lifecycle.read_bytes() == lifecycle_before
    assert verify_package(paths).is_completed


def test_resume_refuses_over_an_invalid_control_document(data_root: DataRoot) -> None:
    """A resumable transaction must never seal a structurally invalid control file."""
    built = build_session(data_root)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.allocation.read_bytes())
    obj["participant_pseudonym"] = "NOT-A-PSEUDONYM"
    paths.allocation.write_bytes(canonical_json.canonicalize(obj))
    _drop_sha_only(paths)
    with pytest.raises(recovery.ResumeError, match=r"allocation\.json"):
        recovery.resume_finalization(paths)
    assert not paths.manifest_sha256.exists()


# --- FINDING 4: durable closure on every terminal path ----------------------


def _multi_stream_writer(data_root: DataRoot) -> tuple[PackagePaths, SessionWriter]:
    from consciousness_lab.session.allocator import allocate_session

    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(
        Run(
            sealed_at=ClockReading(utc_ns=time.time_ns(), monotonic_ns=time.monotonic_ns()),
            required_streams=[STREAM],
            optional_streams=["synthetic.marker"],
        )
    )
    for spec in (
        SyntheticStreamSpec(STREAM, RawCaptureLevel.TRANSPORT_PAYLOAD),
        SyntheticStreamSpec("synthetic.marker", RawCaptureLevel.SYNTHETIC),
    ):
        writer.open_stream(build_descriptor(spec))
        writer.commit_chunk(spec.stream_id, SyntheticSource(spec, seed=7).next_chunk(2))
    return allocated.paths, writer


def test_a_fatal_write_closes_every_open_stream_before_writing_closed(
    data_root: DataRoot,
) -> None:
    """CLOSED is terminal: a stream left unclosed after it can never get a record.

    Recovery would then be permanently blocked on a fact this path could still
    have written, so D33 is enforced here too, not only in ``finalize``.
    """
    paths, writer = _multi_stream_writer(data_root)
    writer.fail_technical("simulated ENOSPC", stream_id=STREAM)

    summary = summarize(parse_records(paths.lifecycle.read_bytes()))
    assert summary.terminal_state is LifecycleState.CLOSED
    assert summary.sealed_outcome is RecordingOutcome.TECHNICAL_FAILURE
    for stream_id in (STREAM, "synthetic.marker"):
        assert paths.stream(stream_id).stream_close.is_file(), f"{stream_id} has no closure"

    from consciousness_lab.storage.stream_state import read_stream_close

    failing, _ = read_stream_close(paths.stream(STREAM).stream_close)
    healthy, _ = read_stream_close(paths.stream("synthetic.marker").stream_close)
    assert failing is StreamCloseStatus.FAILED, "the diagnosed stream is FAILED"
    assert healthy is StreamCloseStatus.CLEAN, "and nothing is invented for the other"


def test_no_closed_record_is_written_when_closure_cannot_be_made_durable(
    data_root: DataRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Better an interrupted session, which is true, than a terminal one that is not."""
    paths, writer = _multi_stream_writer(data_root)

    def refuse(target, data):  # type: ignore[no-untyped-def]
        raise OSError("simulated ENOSPC on the closure record")

    monkeypatch.setattr("consciousness_lab.session.writer.atomic_write_new", refuse)
    writer.fail_technical("simulated ENOSPC", stream_id=STREAM)

    summary = summarize(parse_records(paths.lifecycle.read_bytes()))
    assert summary.terminal_state is not LifecycleState.CLOSED
    assert recovery.scan(paths).state is recovery.StructuralState.INTERRUPTED_RECORDING


def test_finalize_refuses_a_terminal_record_without_durable_closure(
    data_root: DataRoot, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = build_session(data_root, finalize_outcome=None)
    monkeypatch.setattr(
        "consciousness_lab.session.finalizer.write_stream_close",
        lambda path, status: None,
    )
    with pytest.raises(FinalizationError, match=r"no durable stream_close\.json"):
        finalize(built.writer, outcome=RecordingOutcome.ABORTED)
    summary = summarize(parse_records(built.allocated.paths.lifecycle.read_bytes()))
    assert summary.terminal_state is not LifecycleState.CLOSED


# --- FINDING 5: structural declaration is outcome-independent ---------------


@pytest.mark.parametrize(
    "outcome",
    [
        RecordingOutcome.COMPLETED,
        RecordingOutcome.ABORTED,
        RecordingOutcome.TECHNICAL_FAILURE,
        RecordingOutcome.UNCLASSIFIED,
    ],
)
def test_an_undeclared_stream_is_refused_for_every_outcome(
    data_root: DataRoot, outcome: RecordingOutcome
) -> None:
    """V09's stream-set agreement is structural, not a completion rule."""
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec(STREAM, RawCaptureLevel.TRANSPORT_PAYLOAD),
            SyntheticStreamSpec("synthetic.rogue", RawCaptureLevel.SYNTHETIC),
        ],
        required=(STREAM,),
        finalize_outcome=None,
    )
    with pytest.raises(FinalizationError, match="declared in neither"):
        finalize(built.writer, outcome=outcome)
    summary = summarize(parse_records(built.allocated.paths.lifecycle.read_bytes()))
    assert summary.terminal_state is not LifecycleState.CLOSED


def test_a_declared_optional_unclean_stream_still_seals_aborted(
    data_root: DataRoot,
) -> None:
    """The completion rules did not leak into the structural ones."""
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec(STREAM, RawCaptureLevel.TRANSPORT_PAYLOAD),
            SyntheticStreamSpec("synthetic.marker", RawCaptureLevel.SYNTHETIC),
        ],
        required=(STREAM,),
        optional=("synthetic.marker",),
        finalize_outcome=None,
    )
    built.writer.close_stream("synthetic.marker", StreamCloseStatus.DISCONNECTED)
    finalize(built.writer, outcome=RecordingOutcome.ABORTED)
    result = verify_package(built.allocated.paths)
    assert result.conditions[1], "sealed"
    assert result.conditions[5], "and structurally sound"
    assert not result.is_completed, "but not completed, because it never was"
    assert open_package(built.allocated.paths), "and still readable"


def test_a_required_unclean_stream_still_seals_aborted(data_root: DataRoot) -> None:
    """An aborted session whose required device dropped is a normal shape."""
    built = build_session(data_root, finalize_outcome=None)
    built.writer.close_stream(STREAM, StreamCloseStatus.DISCONNECTED)
    finalize(built.writer, outcome=RecordingOutcome.ABORTED)
    result = verify_package(built.allocated.paths)
    assert result.conditions[1]
    assert not result.is_completed
    assert Finding.REQUIRED_STREAM_UNCLEAN in result.findings()
    assert open_package(built.allocated.paths), "outcome-bearing, so still readable"


def test_hash_recomputation_alone_never_rescues_a_package(data_root: DataRoot) -> None:
    """The premise these regressions rest on, asserted directly.

    ``reseal_manifest`` really does restore every ordinary hash: on an untouched
    package it is a no-op that leaves the package complete. So when a mutated
    package fails after the same call, it failed on conformance.
    """
    built = build_session(data_root, chunks=2)
    paths = built.allocated.paths
    before = paths.manifest.read_bytes()
    reseal_manifest(paths)
    assert verify_package(paths).is_completed
    obj_before = canonical_json.loads(before)
    obj_after = canonical_json.loads(paths.manifest.read_bytes())
    assert obj_before["control_sha256"] == obj_after["control_sha256"]
    assert obj_before["events_sha256"] == obj_after["events_sha256"]
    assert hashlib.sha256(paths.manifest.read_bytes()).hexdigest() == (
        paths.manifest_sha256.read_text().strip()
    )
