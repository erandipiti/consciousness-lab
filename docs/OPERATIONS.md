# OPERATIONS

How this repository is worked on. Study operations — running sessions with
participants — are not covered here; that belongs to the protocol
(`PHASE0_PROTOCOL.md`), which does not exist yet.

---

## Environment

The environment is defined by `pyproject.toml` and frozen by `uv.lock`. The
interpreter series is pinned in `.python-version`.

```bash
uv sync --locked --all-groups
```

`--locked` refuses to run if the lock has drifted from `pyproject.toml`. That
refusal is the point: a validation run against an unlocked environment proves
nothing about reproducibility.

To change dependencies: edit `pyproject.toml`, run `uv lock`, commit
`pyproject.toml` and `uv.lock` together in the same commit. A lock update that
arrives separately from the manifest change makes bisecting an environment
regression much harder.

## Validation gates

Run in this order; CI runs the same four.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

Optionally, run them automatically before each commit:

```bash
uv run pre-commit install
```

`ruff` and `mypy` in the pre-commit config run through `uv run`, so hooks and
CI use the same locked tool versions rather than pre-commit's own isolated
environments.

## CI

`.github/workflows/ci.yml` installs the locked environment and runs the four
gates on push to `main` and on every pull request. CI does not update the lock,
does not reformat, and does not tolerate drift.

## Reproducibility contract

An analysis result is reproducible when it can be regenerated from:

1. the raw inputs,
2. this repository at a recorded commit,
3. `uv sync --locked` on that commit.

Anything that breaks one of those three breaks the contract. Practical
consequences: no analysis reads from an unrecorded location, no analysis
depends on a tool installed outside the lock, and no derived artefact is
produced by a step that only exists in someone's shell history.

## Data handling

- `data/` is a local working directory. Its contents are git-ignored.
- Raw data is immutable (`AGENTS.md` §5). Never write into an existing raw
  file.
- Where recordings ultimately live, how they are backed up, and how long they
  are kept are **open** — `SESSION_FORMAT.md` Q8, `SAFETY.md`.

## Recording a session (CL-002B)

The storage and session layer is implemented. A deterministic synthetic session,
end to end, with no hardware attached:

```python
from consciousness_lab.session.allocator import allocate_session
from consciousness_lab.session.finalizer import finalize
from consciousness_lab.session.model import ClockReading, RawCaptureLevel, RecordingOutcome, Run
from consciousness_lab.session.writer import SessionWriter
from consciousness_lab.session import registry
from consciousness_lab.storage.paths import DataRoot
from consciousness_lab.storage.reader import open_package
from consciousness_lab.storage.verifier import verify_package
from consciousness_lab.synthetic.source import SyntheticSource, SyntheticStreamSpec, build_descriptor

root = DataRoot(Path("data"))
allocated = allocate_session(root, participant_pseudonym="P001")   # durable before anything else
writer = SessionWriter.open(allocated.paths)
writer.start_recording(Run(sealed_at=reading, required_streams=["synthetic.eeg"]))

spec = SyntheticStreamSpec("synthetic.eeg", RawCaptureLevel.TRANSPORT_PAYLOAD)
writer.open_stream(build_descriptor(spec))
source = SyntheticSource(spec, seed=7)
for _ in range(2):
    writer.commit_chunk("synthetic.eeg", source.next_chunk(3))

finalize(writer, outcome=RecordingOutcome.COMPLETED, data_root=root)
assert verify_package(allocated.paths).is_completed
registry.rebuild(root)                     # the index is derived; this recovers it entirely
package = open_package(allocated.paths)    # verifies before handing out any raw data
```

`verify_package()` returns structured findings, not a bare boolean. Use
`registry.query_sessions()` rather than `read_sessions()` when the answer
matters: the latter reads the cache as-is and may be stale, and the package is
always the authority.

## Change hygiene

- One CL ticket per change.
- Structural decisions go in `DECISIONS.md`, in the same change that makes them.
- Schema and event-semantics changes are announced explicitly, never folded
  into a refactor (`AGENTS.md` §3).
- `CHANGELOG.md` is updated as part of the work, not afterwards.

## Open

- Branching model and review requirements.
- Release and versioning policy, and how a session records the code version it
  was recorded under (`SESSION_FORMAT.md` Q4).
- Whether CI needs to run on more than one host OS. Relevant because BLE stacks
  differ per platform (`HARDWARE.md`), and no BLE code exists to test yet.
- Secret handling. Nothing here needs credentials today; if that changes, they
  go in the environment or a secret manager, never in the repository.
