"""Batch runner: every session N times, up to `concurrency` calls in flight, one run folder.

runs/<run_id>/
  manifest.json                 run-level record: stamp, parameters, per-call summaries
  <session_id>/<attempt>/...    one call record folder each (see runner/call.py)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from gf.agent.config import agent_config
from gf.config import ROOT, settings
from gf.runner.call import run_call
from gf.sessions.schema import Session, load_all

log = logging.getLogger("gf.batch")


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]


async def run_batch(
    session_paths: list[str],
    *,
    all_sessions: bool = False,
    repeat: int = 3,
    concurrency: int = 4,
    run_id: str = "",
    stagger_s: float = 2.0,
    variant: str | None = None,
) -> dict:
    from gf.agent.variants import get_variant

    v = get_variant(variant)
    sessions = (
        load_all(ROOT / "sessions") if all_sessions else [Session.load(p) for p in session_paths]
    )
    run_id = run_id or new_run_id()
    folder = settings().runs_dir / run_id
    folder.mkdir(parents=True, exist_ok=True)
    cfg = agent_config()
    sessions_hash = hashlib.sha256("".join(sorted(s.id for s in sessions)).encode()).hexdigest()[
        :12
    ]
    manifest: dict = {
        "run_id": run_id,
        "folder": str(folder),
        "started_at": datetime.now(UTC).isoformat(),
        "agent_config_hash": f"{cfg.config_hash}+{v.name}" if v else cfg.config_hash,
        "agent_variant": v.name if v else None,
        "kind": "detector_check" if v else "run",
        "expected_failing_check": v.expected_failing_check if v else None,
        "sessions_hash": sessions_hash,
        "repeat": repeat,
        "concurrency": concurrency,
        "engine": "gf-caller",
        "environment": environment_info(),
        "sessions": [{"id": s.id, "title": s.title, "path": s.source_path} for s in sessions],
        "calls": [],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))

    sem = asyncio.Semaphore(concurrency)
    started = time.monotonic()
    order = 0

    async def one(session: Session, attempt: int) -> dict:
        nonlocal order
        async with sem:
            order += 1
            await asyncio.sleep(stagger_s * ((order - 1) % concurrency))  # spread cold starts
            call_id = f"{session.id}-{attempt}-{uuid.uuid4().hex[:6]}"
            record_dir = folder / session.id / str(attempt)
            log.info("call %s: %s attempt %d", call_id, session.title, attempt)
            try:
                meta = await run_call(
                    session, call_id, record_dir, attempt=attempt, variant=variant
                )
            except Exception as e:  # noqa: BLE001 - one broken call must not sink the run
                log.error("call %s crashed: %s", call_id, e)
                meta = {
                    "call_id": call_id,
                    "session_id": session.id,
                    "attempt": attempt,
                    "runner_error": str(e),
                }
            summary = {
                k: meta.get(k)
                for k in (
                    "call_id",
                    "session_id",
                    "attempt",
                    "ended_by",
                    "end_reason",
                    "goal_met",
                    "tool_calls",
                    "agent_record_complete",
                    "runner_error",
                )
            }
            summary["record_dir"] = str(record_dir)
            manifest["calls"].append(summary)
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
            return summary

    await asyncio.gather(*(one(s, a) for s in sessions for a in range(1, repeat + 1)))
    manifest["duration_s"] = round(time.monotonic() - started, 1)
    manifest["ended_at"] = datetime.now(UTC).isoformat()
    manifest["ended_by"] = dict(Counter(c.get("ended_by") or "crash" for c in manifest["calls"]))
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return manifest


def load_manifest(run_id: str) -> dict:
    return json.loads((settings().runs_dir / run_id / "manifest.json").read_text())


def list_runs() -> list[Path]:
    runs = settings().runs_dir
    if not runs.exists():
        return []

    def started(p: Path) -> str:
        try:
            man = json.loads((p / "manifest.json").read_text())
            return man.get("started_at") or ""
        except (OSError, ValueError):
            return ""

    cands = [p for p in runs.iterdir() if p.is_dir() and (p / "manifest.json").exists()]
    # newest first: by start time from the manifest, then folder mtime, then name
    return sorted(cands, key=lambda p: (started(p), p.stat().st_mtime, p.name), reverse=True)


def environment_info() -> dict:
    """Versions that affect a run, stamped into the manifest so a run is reproducible from
    its files: package versions, Python, the git commit of this checkout."""
    import platform
    import subprocess
    from importlib.metadata import PackageNotFoundError, version

    from gf import __version__

    def ver(name: str) -> str | None:
        try:
            return version(name)
        except PackageNotFoundError:
            return None

    try:
        commit = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=5,
            ).stdout.strip()
            or None
        )
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                cwd=ROOT,
                timeout=5,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        commit, dirty = None, None
    return {
        "gf": __version__,
        "git_commit": commit,
        "git_dirty": dirty,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "livekit-agents": ver("livekit-agents"),
        "livekit-plugins-deepgram": ver("livekit-plugins-deepgram"),
        "livekit-plugins-openai": ver("livekit-plugins-openai"),
        "livekit-plugins-silero": ver("livekit-plugins-silero"),
        "livekit-plugins-turn-detector": ver("livekit-plugins-turn-detector"),
        "livekit-plugins-noise-cancellation": ver("livekit-plugins-noise-cancellation"),
        "openai": ver("openai"),
        "jiwer": ver("jiwer"),
    }
