"""`gf doctor`: one check per dependency, each printed green or red with the reason."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from rich.console import Console

from gf.config import settings

console = Console()


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _check_env() -> list[Check]:
    s = settings()
    required = {
        "LIVEKIT_URL": s.livekit_url,
        "LIVEKIT_API_KEY": s.livekit_api_key,
        "LIVEKIT_API_SECRET": s.livekit_api_secret,
        "DEEPGRAM_API_KEY": s.deepgram_api_key,
        "OPENAI_API_KEY": s.openai_api_key,
    }
    return [
        Check(f"env {name}", bool(value), "set" if value else "missing from .env")
        for name, value in required.items()
    ]


async def _check_livekit() -> Check:
    s = settings()
    if not (s.livekit_url and s.livekit_api_key and s.livekit_api_secret):
        return Check("LiveKit", False, "skipped, keys missing")
    try:
        from livekit import api

        lk = api.LiveKitAPI(s.livekit_url, s.livekit_api_key, s.livekit_api_secret)
        try:
            rooms = await lk.room.list_rooms(api.ListRoomsRequest())
        finally:
            await lk.aclose()
        return Check("LiveKit", True, f"{len(rooms.rooms)} active room(s) in project")
    except Exception as e:  # noqa: BLE001 - any failure is a red check with its reason
        return Check("LiveKit", False, str(e))


async def _check_deepgram() -> Check:
    key = settings().deepgram_api_key
    if not key:
        return Check("Deepgram", False, "skipped, key missing")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {key}"}
            )
        return Check("Deepgram", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        return Check("Deepgram", False, str(e))


async def _check_openai() -> Check:
    key = settings().openai_api_key
    if not key:
        return Check("OpenAI", False, "skipped, key missing")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}
            )
        return Check("OpenAI", r.status_code == 200, f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        return Check("OpenAI", False, str(e))


def _check_docker() -> Check:
    docker = shutil.which("docker") or str(Path.home() / ".docker/bin/docker")
    if not Path(docker).exists():
        return Check("Docker", False, "docker CLI not found")
    try:
        out = subprocess.run(
            [docker, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        ok = out.returncode == 0
        detail = out.stdout.strip() if ok else out.stderr.strip().splitlines()[-1]
        return Check("Docker", ok, detail)
    except Exception as e:  # noqa: BLE001
        return Check("Docker", False, str(e))


def _check_binaries() -> list[Check]:
    return [
        Check(f"binary {name}", shutil.which(name) is not None, shutil.which(name) or "not on PATH")
        for name in ("ffmpeg", "lk")
    ]


async def _collect(extra: list[Callable[[], Check]] | None = None) -> list[Check]:
    checks = _check_env()
    checks += await asyncio.gather(_check_livekit(), _check_deepgram(), _check_openai())
    checks.append(_check_docker())
    checks += _check_binaries()
    for fn in extra or []:
        checks.append(fn())
    return checks


def run_doctor() -> int:
    """Print every check; return 0 when all pass, 1 otherwise."""
    checks = asyncio.run(_collect())
    for c in checks:
        mark = "[green]ok[/green] " if c.ok else "[red]FAIL[/red]"
        console.print(f"{mark}  {c.name:<24} {c.detail}")
    failed = [c for c in checks if not c.ok]
    if failed:
        console.print(f"\n[red]{len(failed)} check(s) failed.[/red]")
        return 1
    console.print("\n[green]All checks passed.[/green]")
    return 0


if __name__ == "__main__":
    os._exit(run_doctor())
