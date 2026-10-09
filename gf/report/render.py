"""Jinja rendering shared by the live UI and the static report."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from gf import __version__
from gf.report import model

TEMPLATES = Path(__file__).resolve().parent / "templates"


class Links:
    """URL builder. `live` produces the FastAPI routes; `static` produces flat file names
    inside one report folder, so the folder can be opened from disk or hosted anywhere."""

    def __init__(self, mode: str = "live", audio: dict[tuple[str, int], str] | None = None):
        self.mode = mode
        self.audio_map = audio or {}

    def overview(self) -> str:
        return "/" if self.mode == "live" else "overview.html"

    def agent(self) -> str:
        return "/agent" if self.mode == "live" else "agent.html"

    def backend(self) -> str:
        return "/backend" if self.mode == "live" else "backend.html"

    def caller(self) -> str:
        return "/caller" if self.mode == "live" else "caller.html"

    def scoring(self) -> str:
        return "/scoring" if self.mode == "live" else "scoring.html"

    def settings(self) -> str:
        return "/settings" if self.mode == "live" else "settings.html"

    def session_set(self, tag: str) -> str:
        return f"/sets/{tag}" if self.mode == "live" else f"set-{tag}.html"

    def environments(self) -> str:
        return "/environments" if self.mode == "live" else "environments.html"

    def providers(self) -> str:
        return "/providers" if self.mode == "live" else "providers.html"

    def sessions(self) -> str:
        return "/sessions" if self.mode == "live" else "sessions.html"

    def session(self, sid: str) -> str:
        return f"/sessions/{sid}" if self.mode == "live" else f"session-{sid}.html"

    def runs(self) -> str:
        return "/runs" if self.mode == "live" else "index.html"

    def run(self, run_id: str) -> str:
        return f"/runs/{run_id}" if self.mode == "live" else "index.html"

    def issues(self, run_id: str) -> str:
        return f"/runs/{run_id}/issues" if self.mode == "live" else "issues.html"

    def caller_quality(self, run_id: str) -> str:
        return f"/runs/{run_id}/caller" if self.mode == "live" else "caller-quality.html"

    def call(self, run_id: str, sid: str, n: int) -> str:
        return f"/runs/{run_id}/{sid}/{n}" if self.mode == "live" else f"call-{sid}-{n}.html"

    def audio(self, run_id: str, sid: str, n: int) -> str:
        if self.mode == "live":
            return f"/runs/{run_id}/{sid}/{n}/audio.wav"
        return self.audio_map.get((sid, int(n)), f"../{sid}/{n}/audio.wav")


def _fmt_dt(value: str | None) -> str:
    if not value:
        return "–"
    try:
        return datetime.fromisoformat(value).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def _fmt_s(value: float | None, digits: int = 1) -> str:
    return "–" if value is None else f"{value:.{digits}f} s"


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value:.0%}"


def environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["ms"] = model.fmt_ms
    env.filters["clock"] = model.fmt_clock
    env.filters["dt"] = _fmt_dt
    env.filters["secs"] = _fmt_s
    env.filters["pct"] = _pct
    env.filters["tojson_compact"] = lambda v: Markup(
        json.dumps(v, separators=(",", ":"), default=str).replace("</", "<\\/")
    )
    env.filters["pretty"] = lambda v: json.dumps(v, indent=2, default=str)
    env.globals["version"] = __version__
    return env


_ENV: Environment | None = None


def render(template: str, links: Links, **ctx: Any) -> str:
    global _ENV
    if _ENV is None:
        _ENV = environment()
    ctx.setdefault("shell", model.shell_info())
    ctx.setdefault("provider_state", "")
    ctx.setdefault("provider_detail", "")
    return _ENV.get_template(template).render(links=links, mode=links.mode, **ctx)
