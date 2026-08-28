"""Operator command line entry point.

CL-001 ships exactly one command: ``version``. Its purpose is to prove that
the locked environment, the package build and the console script are wired
together end to end. Acquisition, session and analysis commands belong to
later tickets and must not be stubbed here in advance.
"""

import typer

from consciousness_lab import __version__

app = typer.Typer(
    name="consciousness-lab",
    help="Consciousness Lab research tooling (Study 001).",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """Consciousness Lab research tooling (Study 001).

    This callback exists to keep the app a subcommand group. Without it Typer
    collapses a single-command app into a bare command, which would silently
    change the invocation contract (``consciousness-lab version`` would stop
    working) the moment a second command is added.
    """


@app.command()
def version() -> None:
    """Print the installed package version."""
    typer.echo(__version__)


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    app()
