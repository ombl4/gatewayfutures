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


@app.command()
def env(
    as_json: bool = typer.Option(False, "--json", help="Machine-readable output."),
    variant: str = typer.Option("", "--variant", help="Agent variant the run would use."),
    backfill: bool = typer.Option(
        False, "--backfill", help="Tag earlier runs that recorded versions but no tag."
    ),
) -> None:
    """Print the environment tag a run started now would carry, and its components."""
    from gf.environment import backfill as _backfill
    from gf.environment import current

    if backfill:
        for rid in _backfill():
            typer.echo(f"tagged {rid}")
    cur = current(agent_variant=variant or None)
    if as_json:
        typer.echo(json.dumps(cur, indent=2))
        return
    typer.echo(f"{cur['env_tag']}")
    c = cur["components"]
    typer.echo(
        f"  agent config   {c['agent_config_hash']}{' +' + c['agent_variant'] if c['agent_variant'] else ''}"
    )
    typer.echo(
        f"  models         {c['models']['stt']} → {c['models']['llm']} → {c['models']['tts']}"
    )
    typer.echo(f"  engine         {c['engine']}")
    typer.echo(f"  scoring        {c['scoring_method']}")
    typer.echo("  libraries      " + ", ".join(f"{k} {v}" for k, v in c["libraries"].items()))
    typer.echo(f"  python         {c['python']}")
    typer.echo(
        "Sessions get their own tag (set-…) per run; two runs are like-for-like when both match."
    )


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
    session: str = typer.Argument("sessions/refund-basic.yaml", help="session YAML"),
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
    variant: str = typer.Option("", help="Agent variant, e.g. 'dishonest' (detector check)."),
    suite: str = typer.Option("", help="Run a named suite from sessions/suites.yaml."),
    parent: str = typer.Option(
        "", help="Run id this is a re-run of (recorded, used as the baseline)."
    ),
    max_cost: float = typer.Option(
        0.0, "--max-cost", help="USD budget for this run (default: GF_MAX_COST_USD, else 10)."
    ),
    persona: str = typer.Option(
        "", help="Comma-separated persona names: run each session once per persona (matrix)."
    ),
    persona_group: str = typer.Option(
        "", "--persona-group", help="A persona group (standard, hard-line, difficult, or all)."
    ),
    target: str = typer.Option(
        "", help="Agent under test to run against (see `gf targets list`; default: the active one)."
    ),
) -> None:
    """Run sessions N times each, concurrently, into runs/<run_id>/ with a manifest."""
    import asyncio
    import logging

    from gf.runner.batch import run_batch

    if suite:
        from gf.config import settings
        from gf.sessions.taxonomy import sessions_in_suite

        sessions = [x.source_path for x in sessions_in_suite(settings().sessions_dir, suite)]
        if not sessions:
            raise typer.BadParameter(f"suite {suite!r} has no runnable sessions")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("opentelemetry", "livekit", "httpx", "aiohttp", "root"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not sessions and not all_sessions:
        raise typer.BadParameter("give session paths or --all")
    personas = [x.strip() for x in persona.split(",") if x.strip()]
    if persona_group:
        from gf.sessions.personas import GROUPS, in_group

        groups = list(GROUPS) if persona_group == "all" else [persona_group]
        try:
            personas += [n for g in groups for n in in_group(g)]
        except ValueError as e:
            raise typer.BadParameter(str(e)) from None
    manifest = asyncio.run(
        run_batch(
            sessions or [],
            all_sessions=all_sessions,
            repeat=repeat,
            concurrency=concurrency,
            run_id=run_id,
            variant=variant or None,
            target=target or None,
            suite=suite or None,
            parent_run=parent or None,
            max_cost_usd=max_cost or None,
            personas=personas or None,
        )
    )
    typer.echo(
        json.dumps(
            {k: manifest[k] for k in ("run_id", "calls", "duration_s", "ended_by")}, indent=2
        )
    )
    typer.echo(f"cost: ${manifest.get('cost_usd', 0):.2f}")
    if manifest.get("partial"):
        typer.echo(
            f"PARTIAL: {manifest.get('stopped_reason')}; {len(manifest['skipped'])} calls skipped"
        )
    typer.echo(f"run folder: {manifest['folder']}")
    _exit_quietly()


@app.command("check-detector")
def check_detector(
    sessions: list[str] = typer.Argument(None, help="session YAML files (default: refund-basic)"),  # noqa: B008
    variant: str = "dishonest",
    run_id: str = "",
) -> None:
    """Prove a check fires: run sessions with a deliberately wrong agent variant and expect
    every call to fail on the variant's check (exit 1 if the detector missed)."""
    import asyncio
    import logging

    from gf.report.model import run_report
    from gf.runner.batch import run_batch
    from gf.scoring.score import score_run

    logging.basicConfig(level=logging.WARNING)
    paths = sessions or ["sessions/refund-basic.yaml"]
    manifest = asyncio.run(
        run_batch(paths, repeat=1, concurrency=min(4, len(paths)), run_id=run_id, variant=variant)
    )
    score_run(manifest["run_id"])
    det = run_report(manifest["run_id"])["detector"]
    for a in det["per_attempt"]:
        mark = "caught " if a["fired"] else ("inconcl" if a["inconclusive"] else "MISSED ")
        typer.echo(f"  {mark} {a['title'][:50]} #{a['attempt']}: {a['reason'][:120]}")
    verdict = (
        "CAUGHT IT" if det["caught"] else ("MISSED" if det["missed"] else "INCONCLUSIVE (rerun)")
    )
    typer.echo(
        f"detector {verdict}: {det['expected_check']} on {det['n_fired']}/{det['n']} calls, "
        f"{det['n_inconclusive']} inconclusive (run {manifest['run_id']})"
    )
    sys_exit = 0 if det["caught"] else 1
    import sys

    sys.stdout.flush()
    sys.stderr.flush()
    __import__("os")._exit(sys_exit)


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
def gate(
    run_id: str,
    min_pass: float = typer.Option(
        0.0, help="Fail unless the pass rate's 95% interval reaches this (0 = off)."
    ),
    max_invalid: float = typer.Option(
        0.2, help="Fail when more than this share of calls is invalid."
    ),
) -> None:
    """CI gate over a scored run: exit 1 on a zero-tolerance failure (claimed without acting,
    wrong write), on too many invalid simulations, or on a pass rate whose interval cannot
    reach --min-pass. Prints one line per reason."""
    from gf.scoring.gate import gate_run

    verdict = gate_run(run_id, min_pass=min_pass, max_invalid=max_invalid)
    for line in verdict["reasons"]:
        typer.echo(f"FAIL: {line}")
    typer.echo(
        f"gate {run_id}: {'PASS' if verdict['ok'] else 'FAIL'} · "
        f"{verdict['passed']}/{verdict['n']} valid passed (interval {verdict['ci_low']:.0%}–{verdict['ci_high']:.0%}) · "
        f"{verdict['invalid']} invalid · ${verdict['cost_usd']:.2f}"
    )
    raise typer.Exit(code=0 if verdict["ok"] else 1)


@app.command()
def personas() -> None:
    """List the persona library by group (personas/*.yaml)."""
    from gf.sessions.personas import groups

    for group, items in groups().items():
        typer.echo(f"{group} ({len(items)})")
        for p in items:
            c = p.conditions
            line = ", ".join(
                x
                for x in (
                    c.noise or "",
                    "phone line" if c.phone_line else "",
                    f"loss {c.packet_loss:.0%}" if c.packet_loss else "",
                    "poor mic" if c.low_quality_mic else "",
                    f"interrupts {c.interruptions:.0%}" if c.interruptions else "",
                    f"patience {c.patience_s:.0f}s",
                )
                if x
            )
            typer.echo(f"  {p.name:24} {p.accent}  {p.voice:20} pace {p.pace}  {line}")
            typer.echo(f"  {'':24} {p.style}")


@app.command("demo-run")
def demo_run(run_id: str = "demo") -> None:
    """Seed runs/<run_id> from the committed real call records (fixtures/records) with
    synthesised audio, then score it, so the UI has something to show without any API keys."""
    from gf.demo import seed_demo_run

    summary = seed_demo_run(run_id)
    typer.echo(
        f"seeded and scored runs/{run_id}: {summary['overall']['passed']}/{summary['overall']['n']} "
        f"passed · open http://127.0.0.1:8090/runs/{run_id}"
    )


@app.command()
def pdf(run_id: str, out: str = "") -> None:
    """Write the run's PDF report for a business reader (default runs/<run_id>/report.pdf)."""
    from pathlib import Path

    from gf.report.pdf import write_pdf

    path = write_pdf(run_id, Path(out) if out else None)
    typer.echo(f"pdf: {path}")


@app.command()
def report(
    run_id: str,
    out: str = "",
    bundle: bool = typer.Option(
        False, "--bundle", help="Transcode audio into the folder (self-contained)."
    ),
) -> None:
    """Render runs/<run_id> as a folder of HTML pages (default runs/<run_id>/report/)."""
    from pathlib import Path

    from gf.report.static import render_run

    folder = render_run(run_id, Path(out) if out else None, bundle_audio=bundle)
    typer.echo(f"report: {folder}/index.html")


@app.command()
def ui(host: str = "127.0.0.1", port: int = 8090) -> None:
    """Serve the live UI over runs/ (same pages as `gf report`)."""
    import uvicorn

    typer.echo(f"UI: http://{host}:{port}")
    uvicorn.run("gf.ui.app:app", host=host, port=port, log_level="warning")


sessions_app = typer.Typer(
    help="Practice sessions: list/validate, generate, export.", invoke_without_command=True
)
app.add_typer(sessions_app, name="sessions")


@sessions_app.callback()
def sessions(ctx: typer.Context) -> None:
    """List sessions/ (and sessions/generated/) with ids; fails on any file that does not validate."""
    if ctx.invoked_subcommand is not None:
        return
    from pathlib import Path

    from gf.sessions.schema import Session

    bad = 0
    folder = Path("sessions")
    paths = sorted(folder.glob("*.yaml")) + sorted((folder / "generated").glob("*.yaml"))
    for p in [x for x in paths if x.name not in ("suites.yaml", "REASONS.yaml")]:
        try:
            s = Session.load(p)
            name = f"generated/{p.name}" if p.parent.name == "generated" else p.name
            typer.echo(
                f"{s.id}  {name:<44} {s.caller.persona.name:<18} "
                f"{s.caller.voice:<22} {s.title[:50]}"
            )
        except Exception as e:  # noqa: BLE001
            bad += 1
            typer.echo(f"INVALID     {p.name}: {e}")
    raise typer.Exit(code=1 if bad else 0)


@sessions_app.command("generate")
def sessions_generate(
    count: int = 10,
    focus: str = "",
    seed: int = 42,
    model: str = "gpt-4.1-mini",
) -> None:
    """Generate sessions from the agent description into sessions/generated/ (needs OPENAI_API_KEY)."""
    from gf.sessions.generate import generate

    rep = generate(count, focus, seed=seed, model=model)
    for w in rep["written"]:
        typer.echo(f"written  {w['id']}  {w['file']}  {w['title'][:60]}")
    for r in rep["rejected"]:
        typer.echo(f"rejected {r['title'][:50]}: {r['reason']}")
    typer.echo(f"{len(rep['written'])} written, {len(rep['rejected'])} rejected")


@sessions_app.command("export-simulate")
def sessions_export_simulate(out: str = "scenarios.yaml") -> None:
    """Write a --scenarios YAML for `lk agent simulate` from the session files."""
    from pathlib import Path

    from gf.engines.livekit_simulate import export_scenarios

    n = export_scenarios(Path(out))
    typer.echo(f"{n} scenarios -> {out}")
    typer.echo("run: lk agent simulate audio --agent-name gf-support-agent --scenarios " + out)


@app.command("spans")
def spans(record_folder: str, otlp: bool = False) -> None:
    """Write the span tree of one call record (spec T6.24): `spans.json`, or with --otlp an
    OTLP/JSON trace that opens in Jaeger or Tempo."""
    from gf.config import thresholds
    from gf.record.spans import export

    th = thresholds()
    out = export(
        record_folder, otlp=otlp, warn_s=th.latency_p95_warn_s, fail_s=th.latency_p95_fail_s
    )
    typer.echo(f"wrote {out}")


@app.command("import-simulate")
def import_simulate(export_json: str, run_id: str = "") -> None:
    """Import an `lk agent simulate export` JSON as a run (engine livekit-simulate)."""
    from pathlib import Path

    from gf.engines.livekit_simulate import import_export

    man = import_export(Path(export_json), run_id=run_id)
    typer.echo(
        f"imported {len(man['calls'])} call(s) into runs/{man['run_id']}; score with: gf score {man['run_id']}"
    )


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


@sessions_app.command("suites")
def sessions_suites() -> None:
    """List the suites in sessions/suites.yaml and the sessions each one runs."""
    from gf.config import settings
    from gf.sessions.taxonomy import load_suites, sessions_in_suite

    folder = settings().sessions_dir
    suites = load_suites(folder)
    if not suites:
        typer.echo("no suites yet: add sessions/suites.yaml or use a session's page in the UI")
        return
    for name in sorted(suites):
        members = sessions_in_suite(folder, name)
        typer.echo(f"{name} ({len(members)} runnable of {len(suites[name])} listed)")
        for x in members:
            typer.echo(f"  {x.title}  [{x.id}]")


runs_app = typer.Typer(help="Manage run folders.", no_args_is_help=True)
app.add_typer(runs_app, name="runs")

targets_app = typer.Typer(help="Agents under test (spec Part 9).", no_args_is_help=True)
app.add_typer(targets_app, name="targets")


@targets_app.command("list")
def targets_list() -> None:
    """The registered agents under test; the active one is marked."""
    from gf import targets

    act = targets.active_id()
    for t in targets.load_all():
        r = t.redacted()
        mark = "*" if t.id == act else " "
        typer.echo(
            f"{mark} {t.id:<16} {t.kind:<8} {r['server_host'] or '(no url)':<32} "
            f"agent={t.agent_name:<20} version={r['version_label']} "
            f"secrets={'yes' if r['has_secrets'] else 'MISSING'}  {t.name}"
        )


@targets_app.command("show")
def targets_show(target_id: str) -> None:
    """One target's record (never its secrets)."""
    import json as _json

    from gf import targets

    typer.echo(_json.dumps(targets.load(target_id).redacted(), indent=2))


@targets_app.command("add")
def targets_add(
    target_id: str,
    name: str = typer.Option(..., help="how the UI names this agent"),
    url: str = typer.Option(..., help="LiveKit server URL (wss://...)"),
    agent_name: str = typer.Option(..., help="agent name to dispatch"),
    api_key: str = typer.Option(..., prompt=True, hide_input=True),
    api_secret: str = typer.Option(..., prompt=True, hide_input=True),
    version: str = typer.Option("", help="a version label the owner maintains"),
    notes: str = typer.Option(""),
) -> None:
    """Register a LiveKit agent under test; secrets go to targets/secrets.yaml."""
    from gf import targets

    t = targets.Target(
        id=target_id, name=name, server_url=url, agent_name=agent_name, version=version, notes=notes
    )
    p = targets.save(t, api_key=api_key, api_secret=api_secret)
    typer.echo(f"registered {target_id} -> {p} (secrets in {p.parent / 'secrets.yaml'})")


@targets_app.command("remove")
def targets_remove(target_id: str) -> None:
    from gf import targets

    try:
        targets.remove(target_id)
    except ValueError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(1) from None
    typer.echo(f"removed {target_id}")


@targets_app.command("test")
def targets_test(target_id: str, timeout: float = 20.0) -> None:
    """Connection test: create a room in the target's project, dispatch its agent, wait for
    it to join and speak. Prints the verdict and stores it next to the target."""
    import logging

    from gf import targets
    from gf.targets_check import connection_test_sync

    logging.getLogger("livekit").setLevel(logging.WARNING)
    r = connection_test_sync(targets.load(target_id), timeout_s=timeout)
    mark = "OK " if r["ok"] else "FAIL"
    typer.echo(f"{mark} {target_id}: {r['message']}")
    if r.get("agent_identity"):
        typer.echo(
            f"     agent {r['agent_identity']} joined after {r['joined_ms']} ms"
            + (f", spoke after {r['spoke_ms']} ms" if r.get("spoke_ms") is not None else "")
        )
    if r.get("error"):
        typer.echo(f"     {r['error']}")
    raise typer.Exit(0 if r["ok"] else 1)


@targets_app.command("use")
def targets_use(target_id: str) -> None:
    """Make a target the active one (what runs and the header use by default)."""
    from gf import targets

    try:
        targets.set_active(target_id)
    except FileNotFoundError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(1) from None
    typer.echo(f"active target: {target_id}")


@runs_app.command("rename")
def runs_rename(old: str, new: str) -> None:
    """Rename a run (folder, manifest, summary, environment registry, children's parent)."""
    from gf.runs_archive import rename

    try:
        dst = rename(old, new)
    except ValueError as e:
        typer.echo(f"error: {e}")
        raise typer.Exit(1) from None
    typer.echo(f"renamed {old} -> {new} ({dst})")


@runs_app.command("archive")
def runs_archive(
    run_ids: list[str] = typer.Argument(None, help="Run ids to archive (default: --all)."),  # noqa: B008
    all_runs: bool = typer.Option(False, "--all", help="Archive every run."),
    restore: bool = typer.Option(
        False, "--restore", help="Move the given runs back out of the archive."
    ),
) -> None:
    """Move finished runs into runs/_archive/ so they leave every list and page (reversible:
    the folders are kept whole; --restore brings them back). Use it before a new baseline
    when the scorer or the session set changed and old runs can no longer be compared."""
    from gf.runs_archive import archive
    from gf.runs_archive import restore as _restore

    moved = _restore(run_ids or []) if restore else archive(run_ids or [], all_runs=all_runs)
    for rid in moved:
        typer.echo(f"{'restored' if restore else 'archived'} {rid}")
    if not moved:
        typer.echo("nothing to do")
