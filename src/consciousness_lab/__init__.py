"""Consciousness Lab — Study 001 research platform.

This package holds session handling and storage — Session Package v2 and the
CL-003 multi-stream recorder. It contains **no acquisition and no analysis**:
no device adapter, no BLE or serial transport, no interpretation of any kind.
See docs/ARCHITECTURE.md for the module boundaries all code here must respect,
and AGENTS.md for the rules that outrank convenience.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("consciousness-lab")
except PackageNotFoundError:  # pragma: no cover - only hit outside an install
    # Running straight from a source tree that was never installed. The
    # fallback must stay a sentinel, never a guessed version number, so a
    # mislabelled recording is impossible.
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
