"""Package layout (spec §3). One place that knows where things live."""

from dataclasses import dataclass
from pathlib import Path

SESSIONS_DIR = "sessions"
DERIVED_DIR = "derived"
REGISTRY_FILE = "registry.sqlite"


@dataclass(frozen=True)
class PackagePaths:
    """Absolute paths inside one acquisition package."""

    root: Path

    @property
    def allocation(self) -> Path:
        return self.root / "allocation.json"

    @property
    def run(self) -> Path:
        return self.root / "run.json"

    @property
    def lifecycle(self) -> Path:
        return self.root / "lifecycle.jsonl"

    @property
    def annotations(self) -> Path:
        return self.root / "annotations.jsonl"

    @property
    def annotations_head(self) -> Path:
        return self.root / "annotations.head.json"

    @property
    def events(self) -> Path:
        return self.root / "events" / "events.jsonl"

    @property
    def schemas(self) -> Path:
        return self.root / "schemas"

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def manifest_sha256(self) -> Path:
        return self.root / "manifest.sha256"

    def stream(self, stream_id: str) -> "StreamPaths":
        return StreamPaths(self.raw / stream_id)


@dataclass(frozen=True)
class StreamPaths:
    """Absolute paths inside one raw stream directory."""

    root: Path

    @property
    def descriptor(self) -> Path:
        return self.root / "descriptor.json"

    @property
    def chunks_index(self) -> Path:
        return self.root / "chunks.jsonl"

    def artifact(self, kind: str, chunk_id: int) -> Path:
        suffix = "bin" if kind == "payloads" else "arrow"
        return self.root / kind / f"{chunk_id:06d}.{suffix}"

    def relative_artifact(self, kind: str, chunk_id: int) -> str:
        suffix = "bin" if kind == "payloads" else "arrow"
        return f"{kind}/{chunk_id:06d}.{suffix}"

    def sidecar(self, chunk_id: int) -> Path:
        return self.root / f"{chunk_id:06d}.commit.json"


@dataclass(frozen=True)
class DataRoot:
    """A data root. Tests point this at a temporary directory."""

    root: Path

    @property
    def sessions(self) -> Path:
        return self.root / SESSIONS_DIR

    @property
    def derived(self) -> Path:
        return self.root / DERIVED_DIR

    @property
    def registry(self) -> Path:
        return self.root / REGISTRY_FILE

    def package(self, session_id: str) -> PackagePaths:
        return PackagePaths(self.sessions / session_id)

    def iter_packages(self) -> list[PackagePaths]:
        if not self.sessions.is_dir():
            return []
        return [PackagePaths(p) for p in sorted(self.sessions.iterdir()) if p.is_dir()]
