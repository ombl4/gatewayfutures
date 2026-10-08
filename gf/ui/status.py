"""System status for the UI: is everything a run needs actually up? Cached for a minute."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from typing import Any

import httpx

from gf.config import settings
from gf.doctor import _check_env, _check_livekit

_CACHE: dict[str, Any] = {"at": 0.0, "rows": []}
TTL_S = 60


async def _collect() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    s = settings()
    # keys
    for c in _check_env():
        rows.append({"name": c.name, "ok": c.ok, "detail": c.detail})
    # backend
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get(f"{s.backend_url}/health")
        rows.append(
            {
                "name": "order system (mock backend)",
                "ok": r.status_code == 200,
                "detail": s.backend_url,
            }
        )
    except Exception as e:  # noqa: BLE001
        rows.append(
            {
                "name": "order system (mock backend)",
                "ok": False,
                "detail": f"{s.backend_url}: {type(e).__name__}",
            }
        )
    # livekit
    try:
        c = await _check_livekit()
        rows.append({"name": c.name, "ok": c.ok, "detail": c.detail})
    except Exception as e:  # noqa: BLE001
        rows.append({"name": "LiveKit", "ok": False, "detail": type(e).__name__})
    # agent worker: a local process, or unknown when it runs elsewhere (Docker)
    found = _pgrep("gf agent") or _pgrep("gf.agent.worker")
    rows.append(
        {
            "name": "agent worker",
            "ok": found,
            "detail": "running on this machine"
            if found
            else "no local process found; if it runs in Docker, check `docker compose ps agent`",
            "soft": not found,
        }
    )
    rows.append(
        {
            "name": "ffmpeg (report audio)",
            "ok": bool(shutil.which("ffmpeg")),
            "detail": shutil.which("ffmpeg") or "not found; --bundle will copy WAV instead",
        }
    )
    return rows


def _pgrep(pattern: str) -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return False
    pids = [p for p in out.stdout.split() if p and int(p) != os.getpid()]
    return bool(pids)


async def status(force: bool = False) -> dict[str, Any]:
    now = time.time()
    if force or now - _CACHE["at"] > TTL_S:
        _CACHE["rows"] = await _collect()
        _CACHE["at"] = now
    rows = _CACHE["rows"]
    hard_fail = [r for r in rows if not r["ok"] and not r.get("soft")]
    return {"rows": rows, "ready": not hard_fail, "checked_at": _CACHE["at"]}


def status_sync(force: bool = False) -> dict[str, Any]:
    return asyncio.run(status(force))


def provider_state() -> tuple[str, str]:
    """('ok' | 'bad' | '', detail) for the header pill, from the cache only (never blocks)."""
    for r in _CACHE["rows"]:
        if r["name"].lower().startswith("livekit"):
            return ("ok" if r["ok"] else "bad"), r.get("detail", "")
    return "", "status not checked yet"
