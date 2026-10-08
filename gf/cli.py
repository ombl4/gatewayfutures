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
    _exit_quietly()


def _exit_quietly() -> None:
    """livekit-rtc's FFI handles assert in __del__ at interpreter exit (harmless, noisy)."""
    import os
    import sys

    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


@app.command()
def run(
    sessions: list[str] | None = None,
    all_sessions: bool = typer.Option(False, "--all", help="Run every session in sessions/."),
    repeat: int = 3,
    concurrency: int = 4,
    run_id: str = "",
) -> None:
    """Run sessions N times each, concurrently, into runs/<run_id>/ with a manifest."""
    import asyncio
    import logging

    from gf.runner.batch import run_batch

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("opentelemetry", "livekit", "httpx", "aiohttp", "root"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not sessions and not all_sessions:
        raise typer.BadParameter("give session paths or --all")
    manifest = asyncio.run(
        run_batch(
            sessions or [],
            all_sessions=all_sessions,
            repeat=repeat,
            concurrency=concurrency,
            run_id=run_id,
        )
    )
    typer.echo(
        json.dumps(
            {k: manifest[k] for k in ("run_id", "calls", "duration_s", "ended_by")}, indent=2
        )
    )
    typer.echo(f"run folder: {manifest['folder']}")
    _exit_quietly()


@app.command()
def score(run_id: str) -> None:
    """Score every call in runs/<run_id> (scores.json per call, summary.json per run)."""
    from gf.scoring.score import score_run

    s = score_run(run_id)
    o = s["overall"]
    typer.echo(
        f"run {run_id}: {o['passed']}/{o['n']} passed "
        f"({o['rate']:.0%}, 95% CI {o['ci_low']:.0%}–{o['ci_high']:.0%}), "
        f"{s['invalid']} invalid, flaky: {s['flaky_sessions'] or 'none'}"
    )
    for sess in s["sessions"]:
        typer.echo(
            f"  {sess['session_id']}  {sess['passed']}/{sess['n']}  "
            f"{'FLAKY ' if sess['flaky'] else ''}{sess['title'][:50]}"
            + (f"  — {sess['main_failure'][:70]}" if sess["main_failure"] else "")
        )
    typer.echo(f"summary: runs/{run_id}/summary.json")


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
