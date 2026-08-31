"""Raw capture level invariant — P1-P7 (v2 §13; D13)."""

import pytest
from pydantic import ValidationError

from consciousness_lab.session.model import (
    Acquisition,
    AcquisitionBackend,
    RawCaptureLevel,
)
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.reader import open_package
from consciousness_lab.storage.verifier import Finding, read_chunk_index, verify_package
from consciousness_lab.synthetic.source import SyntheticStreamSpec
from tests.conftest import build_session


def _acquisition(level: RawCaptureLevel, preserved: bool) -> Acquisition:
    return Acquisition(
        backend=AcquisitionBackend(name="test"),
        raw_capture_level=level,
        transport_payload_preserved=preserved,
    )


def test_p1_transport_payload_with_preserved_false_is_invalid() -> None:
    """The state that let two implementers disagree about payload artifacts."""
    with pytest.raises(ValidationError):
        _acquisition(RawCaptureLevel.TRANSPORT_PAYLOAD, False)


def test_p2_transport_payload_with_preserved_true_is_valid() -> None:
    acquisition = _acquisition(RawCaptureLevel.TRANSPORT_PAYLOAD, True)
    assert acquisition.transport_payload_preserved is True


@pytest.mark.parametrize("level", [RawCaptureLevel.LIBRARY_DECODED, RawCaptureLevel.SYNTHETIC])
def test_p7_non_transport_levels_must_not_claim_preservation(level: RawCaptureLevel) -> None:
    with pytest.raises(ValidationError):
        _acquisition(level, True)
    assert _acquisition(level, False).transport_payload_preserved is False


def test_p3_missing_payload_artifact_fails_verification(data_root: DataRoot) -> None:
    built = build_session(data_root)
    payloads = next((built.allocated.paths.raw / "synthetic.eeg" / "payloads").iterdir())
    payloads.unlink()
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    assert Finding.CHUNK_ARTIFACT_INVALID in result.findings()


def test_p4_fabricated_payload_on_a_library_decoded_stream_is_invalid(
    data_root: DataRoot,
) -> None:
    """A payload artifact at library_decoded is a fabrication and must be caught."""
    built = build_session(
        data_root,
        streams=[SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED)],
        required=("synthetic.ecg",),
    )
    forged = built.allocated.paths.raw / "synthetic.ecg" / "payloads" / "000000.bin"
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_bytes(b"fabricated")
    result = verify_package(built.allocated.paths)
    assert not result.is_completed
    # v2 closes the layout, so the directory itself is the violation: a
    # ``payloads/`` tree may not exist at all below transport_payload.
    assert Finding.UNEXPECTED_FILE in result.findings()


def test_p5_the_packets_schema_is_identical_at_every_capture_level(
    data_root: DataRoot,
) -> None:
    """v2 removed ``payload_ref``, so capture level no longer varies the schema.

    In v1 the packets schema carried a ``payload_ref`` column that had to be
    non-null iff the level was ``transport_payload`` — a relation that vanished
    with the column (§5).
    """
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD),
            SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED),
        ],
        required=("synthetic.eeg", "synthetic.ecg"),
    )
    package = open_package(built.allocated.paths)
    for stream_id in ("synthetic.eeg", "synthetic.ecg"):
        rows = list(package.stream(stream_id).packets())
        assert rows and all("payload_ref" not in row for row in rows)
    decoded = package.stream("synthetic.ecg")
    assert list(decoded.payloads()) == [], "no bytes are invented on read"


def test_library_decoded_commit_omits_the_payloads_entry(data_root: DataRoot) -> None:
    """Omitted entirely, not null and not an empty path (spec §12.2)."""
    built = build_session(
        data_root,
        streams=[SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED)],
        required=("synthetic.ecg",),
    )
    commits, error = read_chunk_index(built.allocated.paths, "synthetic.ecg")
    assert error is None
    assert commits and all(not commit.has_payload_artifact for commit in commits)
    raw = built.allocated.paths.stream("synthetic.ecg").chunks_index.read_bytes()
    assert b'"payloads"' not in raw


def test_transport_payloads_resolve_by_frame_identity(data_root: DataRoot) -> None:
    """Each packet's bytes are found by the ``packet_seq`` its frame carries.

    There is no stored offset or length to trust: the index is derived by
    walking the file, so there is nothing that can drift out of step with it.
    """
    built = build_session(data_root)
    reader = open_package(built.allocated.paths).stream("synthetic.eeg")
    packets = list(reader.packets())
    payloads = list(reader.payloads())
    assert len(payloads) == len(packets)
    for packet, (packet_seq, blob) in zip(packets, payloads, strict=True):
        assert packet_seq == packet["packet_seq"]
        assert blob, "the exact device bytes come back, not a reconstruction"


def test_p6_mixed_capture_levels_in_one_session(data_root: DataRoot) -> None:
    """Each stream independently satisfies its own invariant."""
    built = build_session(
        data_root,
        streams=[
            SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD),
            SyntheticStreamSpec("synthetic.ecg", RawCaptureLevel.LIBRARY_DECODED),
            SyntheticStreamSpec("synthetic.marker", RawCaptureLevel.SYNTHETIC),
        ],
        required=("synthetic.eeg", "synthetic.ecg"),
        optional=("synthetic.marker",),
    )
    assert verify_package(built.allocated.paths).is_completed
    package = open_package(built.allocated.paths)
    assert (package.paths.raw / "synthetic.eeg" / "payloads").is_dir()
    assert not (package.paths.raw / "synthetic.ecg" / "payloads").exists()
    assert not (package.paths.raw / "synthetic.marker" / "payloads").exists()


def test_acquisition_provenance_survives_a_round_trip(data_root: DataRoot) -> None:
    """Backend, version and decode boundary are all readable years later."""
    built = build_session(data_root)
    descriptor = open_package(built.allocated.paths).stream("synthetic.eeg").descriptor
    acquisition = descriptor.acquisition
    assert acquisition.backend.name == "consciousness_lab.synthetic"
    assert acquisition.raw_capture_level is RawCaptureLevel.TRANSPORT_PAYLOAD
    assert acquisition.transport_payload_preserved is True
    assert acquisition.decode_boundary
