"""Package-level contract tests.

These guard the two things CL-001 actually promises: the package imports, and
the version it reports is the version declared in pyproject.toml. A silent
version drift would make a recording impossible to attribute to a code state,
which is why this is asserted rather than assumed.
"""

import tomllib
from pathlib import Path

import consciousness_lab

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_package_imports() -> None:
    assert consciousness_lab.__version__


def test_version_matches_pyproject() -> None:
    with PYPROJECT.open("rb") as handle:
        declared = tomllib.load(handle)["project"]["version"]
    assert consciousness_lab.__version__ == declared
