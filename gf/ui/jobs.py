"""Run control for the UI: start `gf run` as a child process, track it, stop it.

A job is a `runs/<run_id>/job.json` next to the manifest, so the UI survives restarts and
a run started from the CLI is picked up too (its manifest has no `duration_s` while it is
in progress). One run at a time: the agent worker and LiveKit quotas are shared.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gf.config import ROOT, settings
from gf.runner.batch import list_runs, new_run_id
from gf.sessions.schema import load_all


def _job_path(run_id: str) -> Path:
    return settings().runs_dir / run_id / "job.json"


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def job_status(run_id: str) -> dict[str, Any]:
    """{'state': 'running'|'done'|'stopped'|'failed'|'unknown', ...}."""
    run_dir = settings().runs_dir / run_id
    man = _read(run_dir / "manifest.json") or {}
    job = _read(_job_path(run_id)) or {}
    total = len(man.get("sessions", [])) * int(man.get("repeat") or 0)
    done = len(man.get("calls", []))
    if man.get("duration_s") is not None:
        state = "done"
    elif job and _alive(job.get("pid")):
        state = "running"
    elif job and job.get("stopped"):
        state = "stopped"
    elif job:
        state = "failed"  # process gone, manifest never finalised
    elif man:
        state = "running" if _recent(run_dir / "manifest.json") else "unknown"
    else:
        state = "unknown"
    return {
        "run_id": run_id,
        "state": state,
        "done": done,
        "total": total,
        "pid": job.get("pid"),
        "started_at": man.get("started_at") or job.get("started_at"),
        "log": str(run_dir / "run.log") if (run_dir / "run.log").exists() else None,
        "started_from": job.get("started_from", "cli"),
    }


def _recent(path: Path, seconds: int = 600) -> bool:
    try:
        return (datetime.now(UTC).timestamp() - path.stat().st_mtime) < seconds
    except OSError:
        return False


def current() -> dict[str, Any] | None:
    """The run in progress, if any (newest first)."""
    for p in list_runs()[:5]:
        st = job_status(p.name)
        if st["state"] == "running":
            return st
    return None


def start(
    session_ids: list[str],
    repeat: int,
    concurrency: int,
    run_id: str = "",
    variant: str | None = None,
) -> str:
    if current():
        raise RuntimeError("a run is already in progress")
    by_id = {s.id: s for s in load_all(settings().sessions_dir)}
    unknown = [s for s in session_ids if s not in by_id]
    if unknown:
        raise ValueError(f"unknown session id(s): {', '.join(unknown)}")
    if not session_ids:
        raise ValueError("pick at least one session")
    run_id = run_id or new_run_id()
    run_dir = settings().runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "gf.cli",
        "run",
        *[arg for s in session_ids for arg in ("--sessions", by_id[s].source_path)],
        "--repeat",
        str(repeat),
        "--concurrency",
        str(concurrency),
        "--run-id",
        run_id,
        *(["--variant", variant] if variant else []),
    ]
    env = os.environ | {"PYTHONWARNINGS": "ignore", "PYTHONUNBUFFERED": "1"}
    log = open(run_dir / "run.log", "ab")  # noqa: SIM115 - handed to the child
    proc = subprocess.Popen(
        cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
    )
    _job_path(run_id).write_text(
        json.dumps(
            {
                "pid": proc.pid,
                "started_at": datetime.now(UTC).isoformat(),
                "started_from": "ui",
                "sessions": session_ids,
                "repeat": repeat,
                "concurrency": concurrency,
                "variant": variant,
                "cmd": cmd,
            },
            indent=2,
        )
    )
    return run_id


def stop(run_id: str) -> bool:
    job = _read(_job_path(run_id)) or {}
    pid = job.get("pid")
    if not _alive(pid):
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        os.kill(pid, signal.SIGTERM)
    job["stopped"] = True
    job["stopped_at"] = datetime.now(UTC).isoformat()
    _job_path(run_id).write_text(json.dumps(job, indent=2))
    return True


def tail_log(run_id: str, lines: int = 40) -> str:
    p = settings().runs_dir / run_id / "run.log"
    if not p.exists():
        return ""
    data = p.read_bytes()[-20000:].decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])


def _read(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None
