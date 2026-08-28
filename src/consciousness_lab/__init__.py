"""Consciousness Lab — Study 001 research platform.

At CL-001 this package contains no acquisition, no session handling and no
analysis. It exists to hold the locked environment, the package boundary and
the operator entry point that later tickets build on. See docs/ARCHITECTURE.md
for the module boundaries that future code must respect.
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
