"""Shared fixtures for Session Package v2 tests.

Every test writes into a pytest ``tmp_path``. Nothing here ever touches the
repository's own ``data/`` directory.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from consciousness_lab.session.allocator import AllocatedSession, allocate_session
from consciousness_lab.session.finalizer import FinalizationResult, finalize
from consciousness_lab.session.model import (
    ClockReading,
    ClosureCondition,
    RawCaptureLevel,
    RecordingOutcome,
    Run,
    StreamCloseStatus,
)
from consciousness_lab.session.writer import SessionWriter
from consciousness_lab.storage import canonical_json
from consciousness_lab.storage.chunk_writer import FaultHook
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.synthetic.source import (
    SyntheticSource,
    SyntheticStreamSpec,
    build_descriptor,
)


@dataclass
class BuiltSession:
    """A written session package plus the handles a test may want to poke."""

    data_root: DataRoot
    allocated: AllocatedSession
    writer: SessionWriter
    result: FinalizationResult | None


@pytest.fixture
def data_root(tmp_path: Path) -> DataRoot:
    return DataRoot(tmp_path / "data")


def now_reading() -> ClockReading:
    return ClockReading(utc_ns=time.time_ns(), monotonic_ns=time.monotonic_ns())


def build_session(
    data_root: DataRoot,
    *,
    streams: Sequence[SyntheticStreamSpec] | None = None,
    required: Sequence[str] = ("synthetic.eeg",),
    optional: Sequence[str] = (),
    chunks: int = 2,
    packets_per_chunk: int = 3,
    seed: int = 7,
    finalize_outcome: RecordingOutcome | None = RecordingOutcome.COMPLETED,
    closure: ClosureCondition = ClosureCondition.CLEAN,
    close_statuses: dict[str, StreamCloseStatus] | None = None,
    fault: FaultHook | None = None,
) -> BuiltSession:
    """Write a deterministic synthetic session, optionally finalizing it."""
    specs = list(
        streams
        or [
            SyntheticStreamSpec(
                stream_id="synthetic.eeg", capture_level=RawCaptureLevel.TRANSPORT_PAYLOAD
            )
        ]
    )
    allocated = allocate_session(data_root, participant_pseudonym="P001")
    writer = SessionWriter.open(allocated.paths)
    writer.start_recording(
        Run(
            sealed_at=now_reading(),
            required_streams=list(required),
            optional_streams=list(optional),
        )
    )
    for spec in specs:
        writer.open_stream(build_descriptor(spec))
        source = SyntheticSource(spec, seed=seed)
        for _ in range(chunks):
            writer.commit_chunk(spec.stream_id, source.next_chunk(packets_per_chunk), fault=fault)
    for stream_id, status in (close_statuses or {}).items():
        writer.close_stream(stream_id, status)

    result = None
    if finalize_outcome is not None:
        result = finalize(writer, outcome=finalize_outcome, closure_condition=closure)
    return BuiltSession(data_root=data_root, allocated=allocated, writer=writer, result=result)


def read_chunk_records(paths: PackagePaths, stream_id: str) -> list[dict[str, Any]]:
    """Every chunk record of a stream, as parsed JSON documents."""
    raw = paths.stream(stream_id).chunks_index.read_bytes()
    return [canonical_json.loads(line) for line in raw.split(b"\n") if line]


def rewrite_chunk_chain(paths: PackagePaths, stream_id: str, records: list[dict[str, Any]]) -> None:
    """Rewrite a chunk chain, recomputing the hash chain so it stays valid.

    Adversarial tests must assume the attacker can recompute every hash they can
    see. v2 chains on the SHA-256 of the previous record's canonical bytes, so a
    forger recomputes it — this helper does exactly that, which is what makes
    the surviving checks meaningful rather than incidental.
    """
    import hashlib

    prev = canonical_json.ZERO_HASH
    lines: list[bytes] = []
    for record in records:
        body = dict(record)
        body["prev_record_sha256"] = prev
        canonical = canonical_json.canonicalize(body)
        prev = hashlib.sha256(canonical).hexdigest()
        lines.append(canonical)
    paths.stream(stream_id).chunks_index.write_bytes(b"".join(line + b"\n" for line in lines))


def reseal_manifest(paths: PackagePaths) -> None:
    """Recompute every hash a tamperer could recompute, then reseal the pair.

    Used so a test proves a check survives a *fully coherent* rewrite rather
    than merely tripping the manifest hash.
    """
    from consciousness_lab.storage import package_layout
    from consciousness_lab.storage.checksums import sha256_bytes, sha256_file
    from consciousness_lab.storage.stream_state import list_physical_streams

    obj = canonical_json.loads(paths.manifest.read_bytes())
    events = paths.events.read_bytes() if paths.events.is_file() else b""
    schema_ids, _ = package_layout.referenced_schema_ids(events)
    obj["events_sha256"] = sha256_bytes(events)
    lifecycle = paths.lifecycle.read_bytes()
    obj["lifecycle_seal"] = {
        "sealed_len": str(len(lifecycle)),
        "sealed_sha256": sha256_bytes(lifecycle),
    }
    control = {}
    for relative in package_layout.expected_control_paths(list_physical_streams(paths), schema_ids):
        target = paths.root / relative
        if target.is_file():
            control[relative] = sha256_file(target)
    obj["control_sha256"] = control
    body = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(body)
    paths.manifest_sha256.write_text(sha256_bytes(body) + "\n", encoding="utf-8")
