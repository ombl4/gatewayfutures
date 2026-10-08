"""The `gf` command line. Subcommands are filled in part by part; see docs/spec.md."""

from __future__ import annotations

import typer

from gf import __version__

app = typer.Typer(help="Simulated phone calls for voice agents.", no_args_is_help=True)


@app.callback()
def _root(
    version: bool = typer.Option(False, "--version", help="Print the version and exit."),
) -> None:
    if version:
        typer.echo(f"gf {__version__}")
        raise typer.Exit()


@app.command()
def doctor() -> None:
    """Check keys, LiveKit, Deepgram, OpenAI and Docker."""
    from gf.doctor import run_doctor

    raise typer.Exit(code=run_doctor())


if __name__ == "__main__":
    app()
