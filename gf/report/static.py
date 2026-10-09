"""`gf report <run_id>`: render one run as a folder of plain HTML files.

Default: runs/<run_id>/report/ with audio linked relatively to the record folders.
With --bundle the audio is transcoded (ffmpeg, mp3) into the folder, so the whole folder
can be committed or hosted on its own (docs/sample-report).
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from gf.config import settings
from gf.report import model
from gf.report.render import Links, render


def render_run(run_id: str, out: Path | None = None, *, bundle_audio: bool = False) -> Path:
    run_dir = settings().runs_dir / run_id
    out = out or run_dir / "report"
    out.mkdir(parents=True, exist_ok=True)
    generated = datetime.now(UTC).isoformat()
    r = model.run_report(run_id)
    calls = [(a["session_id"], int(a["attempt"])) for a in r["summary"]["attempts"]]

    audio_map: dict[tuple[str, int], str] = {}
    if bundle_audio:
        (out / "audio").mkdir(exist_ok=True)
        for sid, n in calls:
            src = run_dir / sid / str(n) / "audio.wav"
            if not src.exists():
                continue
            dst = out / "audio" / f"{sid}-{n}.mp3"
            if not dst.exists():
                _transcode(src, dst)
            audio_map[(sid, n)] = f"audio/{sid}-{n}.mp3"
    else:
        # relative path from runs/<id>/report/ to runs/<id>/<sid>/<n>/audio.wav
        for sid, n in calls:
            if (run_dir / sid / str(n) / "audio.wav").exists():
                audio_map[(sid, n)] = _relpath(run_dir / sid / str(n) / "audio.wav", out)

    links = Links("static", audio=audio_map)
    common = {"generated": generated}

    def issue_link(i: dict) -> str:
        t = i["t_ms"] if i.get("t_ms") is not None else 0
        return f"{links.call(run_id, i['session_id'], i['attempt'])}#t={t}"

    insp: dict = {"c": None, "start_ms": None}
    sel = model.select_call(r)
    if sel and (run_dir / sel[0] / str(sel[1]) / "meta.json").exists():
        insp["c"] = model.call_report(run_id, sel[0], sel[1], audio_href=audio_map.get(sel))
        insp["start_ms"] = next(
            (i["t_ms"] for i in r["issues"]["items"] if (i["session_id"], i["attempt"]) == sel),
            None,
        )
    (out / "index.html").write_text(
        render("run.html", links, r=r, issue_link=issue_link, **insp, **common)
    )
    o = model.overview()
    (out / "overview.html").write_text(
        render("overview.html", links, o=o, r=r, issue_link=issue_link, **insp, **common)
    )
    (out / "agent.html").write_text(render("agent.html", links, a=model.agent_page(), **common))
    (out / "backend.html").write_text(
        render("backend.html", links, b=model.backend_page(), **common)
    )
    (out / "caller.html").write_text(render("caller.html", links, **common))
    (out / "scoring.html").write_text(
        render("scoring.html", links, s=model.scoring_page(), **common)
    )
    (out / "environments.html").write_text(
        render("environments.html", links, e=model.environments_page(), **common)
    )
    sp = model.sessions_page()
    (out / "sessions.html").write_text(render("sessions.html", links, p=sp, **common))
    for s in sp["sessions"]:
        (out / f"session-{s['id']}.html").write_text(
            render("session.html", links, p=model.session_page(s["id"]), **common)
        )
    for sid, n in calls:
        folder = run_dir / sid / str(n)
        if not (folder / "meta.json").exists():
            continue
        c = model.call_report(run_id, sid, n, audio_href=audio_map.get((sid, n)))
        (out / f"call-{sid}-{n}.html").write_text(render("call.html", links, c=c, **common))
    return out


def _relpath(target: Path, start: Path) -> str:
    import os

    return os.path.relpath(target, start)


def _transcode(src: Path, dst: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        shutil.copy(src, dst.with_suffix(".wav"))
        return
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-i", str(src), "-ac", "2", "-b:a", "96k", str(dst)],
        check=True,
    )
