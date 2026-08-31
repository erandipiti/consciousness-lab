"""Durable per-stream closure — ``raw/<id>/stream_close.json`` (v2 §7.1; D33).

R1-1's crash window existed because terminal closure lived only in process
memory until the final manifest was written. These tests hold the fix in place:
the file is the fact, it is written before the terminal lifecycle record, it is
immutable once written, and nothing else on disk repeats it.
"""

import pytest

from consciousness_lab.session import recovery
from consciousness_lab.session.finalizer import FinalizationError, finalize
from consciousness_lab.session.model import (
    LifecycleState,
    RawCaptureLevel,
    RecordingOutcome,
    StreamClose,
    StreamCloseStatus,
)
from consciousness_lab.session.writer import SealedPackageError
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.verifier import Finding, verify_package
from consciousness_lab.synthetic.source import SyntheticStreamSpec
from tests.conftest import build_session, reseal_manifest

STREAM = "synthetic.eeg"


def test_the_close_file_carries_only_a_status(data_root: DataRoot) -> None:
    """No ``stream_id``: the path already owns identity, and one copy is enough."""
    built = build_session(data_root)
    raw = built.allocated.paths.stream(STREAM).stream_close.read_bytes()
    assert raw == b'{"close_status":"CLEAN"}'
    assert canonical_json.loads(raw) == {"close_status": "CLEAN"}


@pytest.mark.parametrize(
    "status",
    [
        StreamCloseStatus.CLEAN,
        StreamCloseStatus.DISCONNECTED,
        StreamCloseStatus.RECONFIGURED,
        StreamCloseStatus.FAILED,
        StreamCloseStatus.RECOVERED_UNCLEAN,
    ],
)
def test_every_status_round_trips(data_root: DataRoot, status: StreamCloseStatus) -> None:
    built = build_session(
        data_root,
        required=(),
        optional=(STREAM,),
        finalize_outcome=None,
    )
    built.writer.close_stream(STREAM, status)
    finalize(built.writer, outcome=RecordingOutcome.COMPLETED)
    path = built.allocated.paths.stream(STREAM).stream_close
    assert (
        StreamClose.model_validate(canonical_json.loads(path.read_bytes())).close_status is status
    )


def test_recovered_unclean_is_new_in_v2_and_never_inferred() -> None:
    """It says what recovery observed, never a device-specific cause (D33)."""
    assert StreamCloseStatus.RECOVERED_UNCLEAN.value == "RECOVERED_UNCLEAN"
    assert StreamCloseStatus.RECOVERED_UNCLEAN not in (
        StreamCloseStatus.FAILED,
        StreamCloseStatus.DISCONNECTED,
    )


def test_the_close_file_is_immutable_once_written(data_root: DataRoot) -> None:
    """A stream that closed earlier must not have its record rewritten."""
    built = build_session(data_root, finalize_outcome=None)
    built.writer.close_stream(STREAM, StreamCloseStatus.CLEAN)
    with pytest.raises(SealedPackageError):
        built.writer.close_stream(STREAM, StreamCloseStatus.FAILED)
    raw = built.allocated.paths.stream(STREAM).stream_close.read_bytes()
    assert b"CLEAN" in raw and b"FAILED" not in raw


def test_closure_is_durable_before_the_terminal_lifecycle_record(
    data_root: DataRoot,
) -> None:
    """Step 2 precedes step 3, or a crash between them loses the fact (§9.4)."""
    built = build_session(data_root, finalize_outcome=None)
    close_path = built.allocated.paths.stream(STREAM).stream_close
    assert not close_path.exists()
    built.writer.close_stream(STREAM, StreamCloseStatus.CLEAN)
    assert close_path.is_file(), "the file lands before finalization even begins"
    assert built.writer.lifecycle.state is not LifecycleState.CLOSED


def test_finalization_writes_a_close_file_for_a_stream_never_closed_explicitly(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root)
    assert built.allocated.paths.stream(STREAM).stream_close.is_file()
    assert verify_package(built.allocated.paths).is_completed


def test_a_missing_close_file_fails_condition_five(data_root: DataRoot) -> None:
    built = build_session(data_root)
    built.allocated.paths.stream(STREAM).stream_close.unlink()
    reseal_manifest(built.allocated.paths)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.MISSING_STREAM_CLOSE in result.findings()
    assert result.conditions[5] is False


def test_a_required_stream_that_did_not_close_clean_blocks_completion(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root, finalize_outcome=None)
    built.writer.close_stream(STREAM, StreamCloseStatus.DISCONNECTED)
    with pytest.raises(FinalizationError, match="not CLEAN"):
        finalize(built.writer, outcome=RecordingOutcome.COMPLETED)


@pytest.mark.parametrize(
    "status",
    [StreamCloseStatus.DISCONNECTED, StreamCloseStatus.FAILED, StreamCloseStatus.RECOVERED_UNCLEAN],
)
def test_an_unclean_required_stream_is_rejected_by_the_verifier(
    data_root: DataRoot, status: StreamCloseStatus
) -> None:
    built = build_session(data_root)
    path = built.allocated.paths.stream(STREAM).stream_close
    path.write_bytes(canonical_json.canonicalize({"close_status": status.value}))
    reseal_manifest(built.allocated.paths)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.REQUIRED_STREAM_UNCLEAN in result.findings()


def test_an_optional_stream_never_opened_is_valid(data_root: DataRoot) -> None:
    """No directory, no close file — that is a legitimate shape (§9, cond. 5)."""
    built = build_session(
        data_root,
        required=(STREAM,),
        optional=("synthetic.never_opened",),
    )
    assert verify_package(built.allocated.paths).is_completed
    assert not (built.allocated.paths.raw / "synthetic.never_opened").exists()


def test_an_unclean_optional_stream_does_not_block_completion(
    data_root: DataRoot,
) -> None:
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
    finalize(built.writer, outcome=RecordingOutcome.COMPLETED)
    assert verify_package(built.allocated.paths).is_completed


def test_a_malformed_close_file_is_a_finding_not_a_crash(data_root: DataRoot) -> None:
    built = build_session(data_root)
    path = built.allocated.paths.stream(STREAM).stream_close
    path.write_bytes(b'{"close_status":"INVENTED"}')
    reseal_manifest(built.allocated.paths)
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.INVALID_STREAM_CLOSE in result.findings()


def test_recovery_writes_recovered_unclean_for_a_stream_that_never_closed(
    data_root: DataRoot,
) -> None:
    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    assert not paths.stream(STREAM).stream_close.exists()
    report = recovery.scan(paths)
    assert STREAM in report.unclosed_streams

    recovery.close_unclean(paths)
    status = StreamClose.model_validate(
        canonical_json.loads(paths.stream(STREAM).stream_close.read_bytes())
    ).close_status
    assert status is StreamCloseStatus.RECOVERED_UNCLEAN
