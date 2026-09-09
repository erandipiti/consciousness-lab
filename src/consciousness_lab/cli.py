"""Operator command line entry point.

Two command groups: ``version``, which proves the locked environment, the
package build and the console script are wired together end to end; and
``probe``, the hardware verification surface added at CL-004.

Acquisition, session and analysis commands belong to later tickets and must not
be stubbed here in advance. ``probe`` is not one of them: it records what a
device does, and takes no data anywhere.
"""

import typer

from consciousness_lab import __version__
from consciousness_lab.verification.probe import app as probe_app

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


# Hardware verification is an operator surface, not an acquisition one: it
# records what a device does so `docs/HARDWARE.md` can stop saying "pending
# verification". It writes evidence, never study data, and it cannot promote a
# claim — that is a human act under AGENTS.md §7.
app.add_typer(probe_app, name="probe")


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    app()
