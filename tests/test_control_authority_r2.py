"""Regressions for the CL-002B-R2-R2 human-review findings.

Two invariants, both about *which bytes* a decision was made from:

* the control documents finalization decides on are the same physical bytes
  ``control_sha256`` seals;
* a digest is never created over a manifest the verifier would reject.
"""

import pytest

from consciousness_lab.session import recovery
from consciousness_lab.session.finalizer import FinalizationError, finalize
from consciousness_lab.session.lifecycle import parse_records, summarize
from consciousness_lab.session.model import LifecycleState, RecordingOutcome
from consciousness_lab.storage import canonical_json, package_layout
from consciousness_lab.storage.checksums import sha256_bytes
from consciousness_lab.storage.paths import DataRoot, PackagePaths
from consciousness_lab.storage.verifier import verify_package
from tests.conftest import build_session

STREAM = "synthetic.eeg"


def _nothing_was_written(paths: PackagePaths) -> None:
    """No terminal record, no manifest, no digest."""
    summary = summarize(parse_records(paths.lifecycle.read_bytes()))
    assert summary.terminal_state is not LifecycleState.CLOSED
    assert not paths.manifest.exists()
    assert not paths.manifest_sha256.exists()


# --- FINDING 1: finalize decides on validated PHYSICAL control documents ----


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema_version", "1.0"),
        ("participant_pseudonym", "NOT-A-PSEUDONYM"),
        ("session_id", "00000000-0000-0000-0000-000000000000"),
    ],
)
def test_finalize_refuses_an_invalid_physical_allocation(
    data_root: DataRoot, key: str, value: str
) -> None:
    """Canonical on disk is not the same as valid."""
    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.allocation.read_bytes())
    obj[key] = value
    paths.allocation.write_bytes(canonical_json.canonicalize(obj))

    with pytest.raises(FinalizationError):
        finalize(built.writer, outcome=RecordingOutcome.COMPLETED)
    _nothing_was_written(paths)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("required_streams", "not-a-list"),
        ("sealed_at", "not-a-clock-reading"),
        ("devices", 7),
    ],
)
def test_finalize_refuses_an_invalid_physical_run(
    data_root: DataRoot, key: str, value: object
) -> None:
    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.run.read_bytes())
    obj[key] = value
    paths.run.write_bytes(canonical_json.canonicalize(obj))

    with pytest.raises(FinalizationError, match="run"):
        finalize(built.writer, outcome=RecordingOutcome.COMPLETED)
    _nothing_was_written(paths)


@pytest.mark.parametrize(
    "outcome",
    [RecordingOutcome.COMPLETED, RecordingOutcome.ABORTED, RecordingOutcome.TECHNICAL_FAILURE],
)
def test_finalize_refuses_when_the_durable_run_differs_from_the_running_one(
    data_root: DataRoot, outcome: RecordingOutcome
) -> None:
    """A valid-but-different run.json is the dangerous case, not the malformed one.

    Deciding from ``writer.run`` while hashing different bytes into
    ``control_sha256`` would make the sealed contract and the enforced contract
    two separate things. ``run.json`` is sealed once and never rewritten, so a
    divergence is external mutation — reported, never silently resolved in
    either direction.
    """
    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.run.read_bytes())
    obj["required_streams"] = ["synthetic.someone_else"]
    obj["optional_streams"] = []
    paths.run.write_bytes(canonical_json.canonicalize(obj))

    with pytest.raises(FinalizationError, match="disagree"):
        finalize(built.writer, outcome=outcome)
    _nothing_was_written(paths)


def test_finalize_refuses_a_noncanonical_physical_run(data_root: DataRoot) -> None:
    import json

    built = build_session(data_root, finalize_outcome=None)
    paths = built.allocated.paths
    obj = canonical_json.loads(paths.run.read_bytes())
    paths.run.write_bytes(json.dumps(obj, indent=2).encode("utf-8"))

    with pytest.raises(FinalizationError, match="canonical"):
        finalize(built.writer, outcome=RecordingOutcome.COMPLETED)
    _nothing_was_written(paths)


def test_the_sealed_control_bytes_are_the_bytes_finalization_decided_on(
    data_root: DataRoot,
) -> None:
    """The invariant itself, asserted directly."""
    built = build_session(data_root)
    paths = built.allocated.paths
    assert built.result is not None
    control = built.result.manifest.control_sha256
    for relative in ("allocation.json", "run.json"):
        assert control[relative] == sha256_bytes((paths.root / relative).read_bytes())
    assert package_layout.allocation_error(paths) is None
    assert verify_package(paths).is_completed


# --- FINDING 2: State B applies the full manifest contract ------------------


def _state_b(data_root: DataRoot) -> PackagePaths:
    """A package in the real post-step-10, pre-step-11 state."""
    built = build_session(data_root)
    built.allocated.paths.manifest_sha256.unlink()
    return built.allocated.paths


def test_state_b_writes_only_the_digest_for_a_valid_manifest(
    data_root: DataRoot,
) -> None:
    paths = _state_b(data_root)
    manifest_bytes = paths.manifest.read_bytes()
    recovery.resume_finalization(paths)
    assert paths.manifest.read_bytes() == manifest_bytes
    assert paths.manifest_sha256.read_text().strip() == sha256_bytes(manifest_bytes)
    assert verify_package(paths).is_completed


def test_state_b_refuses_an_unsupported_major(data_root: DataRoot) -> None:
    paths = _state_b(data_root)
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj["schema_version"] = "1.0"
    forged = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(forged)

    with pytest.raises(recovery.ResumeError, match="invalid"):
        recovery.resume_finalization(paths)
    assert not paths.manifest_sha256.exists()
    assert paths.manifest.read_bytes() == forged, "no replacement"


@pytest.mark.parametrize(
    "removed", ["session_id", "streams", "inventory", "schemas", "scope_note", "events_seal"]
)
def test_state_b_refuses_each_removed_v1_manifest_key(data_root: DataRoot, removed: str) -> None:
    paths = _state_b(data_root)
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj[removed] = "resurrected"
    forged = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(forged)

    with pytest.raises(recovery.ResumeError, match="invalid"):
        recovery.resume_finalization(paths)
    assert not paths.manifest_sha256.exists()
    assert paths.manifest.read_bytes() == forged


def test_state_b_refuses_a_noncanonical_manifest(data_root: DataRoot) -> None:
    import json

    paths = _state_b(data_root)
    obj = canonical_json.loads(paths.manifest.read_bytes())
    paths.manifest.write_bytes(json.dumps(obj, indent=2).encode("utf-8"))
    with pytest.raises(recovery.ResumeError, match="invalid"):
        recovery.resume_finalization(paths)
    assert not paths.manifest_sha256.exists()


def test_state_b_tolerates_an_unknown_future_field(data_root: DataRoot) -> None:
    """Forward tolerance is deliberate and survives the tightening.

    ``extra="forbid"`` would have closed this finding and broken v2.x
    compatibility with it; a known-deleted key is rejected, an unknown future
    one is not.
    """
    paths = _state_b(data_root)
    obj = canonical_json.loads(paths.manifest.read_bytes())
    obj["some_future_v2_1_field"] = "ignored"
    forged = canonical_json.canonicalize(obj)
    paths.manifest.write_bytes(forged)

    recovery.resume_finalization(paths)
    assert paths.manifest.read_bytes() == forged, "still not replaced"
    assert paths.manifest_sha256.read_text().strip() == sha256_bytes(forged)
    assert verify_package(paths).is_completed


def test_state_b_never_creates_a_digest_the_verifier_would_reject(
    data_root: DataRoot,
) -> None:
    """The property behind all of the above, stated once.

    Recovery and verification share one definition of a valid manifest, so a
    digest recovery writes cannot produce a sealed package that then fails
    condition 1.
    """
    mutations: list[tuple[str, object]] = [
        ("schema_version", "1.0"),
        ("streams", []),
        ("inventory", []),
    ]
    for key, value in mutations:
        paths = _state_b(data_root)
        obj = canonical_json.loads(paths.manifest.read_bytes())
        obj[key] = value
        paths.manifest.write_bytes(canonical_json.canonicalize(obj))

        with pytest.raises(recovery.ResumeError):
            recovery.resume_finalization(paths)
        assert not paths.manifest_sha256.exists()
        # And had the digest been written, the verifier would have rejected it.
        paths.manifest_sha256.write_text(
            sha256_bytes(paths.manifest.read_bytes()) + "\n", encoding="utf-8"
        )
        assert not verify_package(paths).conditions[1]


def test_verifier_and_recovery_share_one_manifest_definition(
    data_root: DataRoot,
) -> None:
    """Not two almost-identical ones — that is the drift v2 exists to remove."""
    built = build_session(data_root)
    raw = built.allocated.paths.manifest.read_bytes()
    assert package_layout.check_manifest_contract(raw).ok

    import inspect

    from consciousness_lab.storage import verifier

    assert "check_manifest_contract" in inspect.getsource(verifier)
    assert "check_manifest_contract" in inspect.getsource(recovery)
