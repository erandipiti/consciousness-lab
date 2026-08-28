"""CLI wiring tests.

The console script is the only operator surface at CL-001. If it stops
resolving, every later ticket inherits a broken entry point, so it is checked
directly rather than through the installed shim.
"""

from typer.testing import CliRunner

from consciousness_lab import __version__
from consciousness_lab.cli import app

runner = CliRunner()


def test_version_command_prints_installed_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_bare_invocation_shows_help() -> None:
    # no_args_is_help means a bare call must not silently succeed; an operator
    # who mistypes should get the command list, not an empty exit 0.
    result = runner.invoke(app, [])
    assert result.exit_code != 0
    assert "version" in result.stdout
