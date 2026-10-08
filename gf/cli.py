"""The `gf` command line. Subcommands are filled in part by part; see docs/spec.md."""

from __future__ import annotations

import json

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


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def agent(ctx: typer.Context) -> None:
    """Run the support agent worker: `gf agent dev|start|console|download-files`."""
    import sys

    from gf.agent.worker import main

    sys.argv = ["gf-agent", *ctx.args]
    main()


PROBE_LINES = [
    "Hi, I need a refund. My order number is G W 4 8 2 1 3 and the zip code is 9 4 1 1 0.",
    "The blender arrived broken. I'd like the full amount back.",
    "Yes, that's right, go ahead.",
]


@app.command()
def probe(
    room: str = "gf-probe",
    say: list[str] | None = None,
    out: str = "runs/_probe",
) -> None:
    """Scripted voice call against the running agent worker (no LLM on the caller side).

    --room: room name (the agent is dispatched into it). --say: a line, repeatable.
    --out: output folder for audio and summary.
    """
    from pathlib import Path

    from gf.caller.probe import main

    raise typer.Exit(code=main(room, say or PROBE_LINES, Path(out)))


@app.command()
def call(
    session: str = "sessions/refund-basic.yaml",
    out: str = "",
    attempt: int = 1,
) -> None:
    """One simulated call with the gf-caller engine. Needs the agent worker and backend up.

    --session: session YAML. --out: record folder (default runs/_single/<session id>/<n>).
    """
    import asyncio
    import logging
    from pathlib import Path

    from gf.runner.call import run_call
    from gf.sessions.schema import Session

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("opentelemetry", "livekit", "httpx", "aiohttp"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    sess = Session.load(session)
    call_id = f"{sess.id}-{attempt}-{__import__('uuid').uuid4().hex[:6]}"
    folder = Path(out) if out else Path("runs/_single") / sess.id / str(attempt)
    meta = asyncio.run(run_call(sess, call_id, folder, attempt=attempt))
    typer.echo(
        json.dumps(
            {
                k: meta[k]
                for k in (
                    "call_id",
                    "ended_by",
                    "end_reason",
                    "goal_met",
                    "tool_calls",
                    "agent_record_complete",
                )
            },
            indent=2,
        )
    )
    typer.echo(f"record: {folder}")


@app.command()
def config() -> None:
    """Print the agent configuration and its hash."""
    from gf.agent.config import agent_config

    cfg = agent_config()
    typer.echo(f"{cfg.name}  hash={cfg.config_hash}")
    typer.echo(
        f"stt={cfg.models.stt.model}  llm={cfg.models.llm.model}  tts={cfg.models.tts.model}"
    )
    typer.echo(f"tools={', '.join(cfg.tools)}")


if __name__ == "__main__":
    app()
