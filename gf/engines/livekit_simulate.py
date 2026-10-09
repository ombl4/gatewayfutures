"""Second engine (spec T8.1/T8.2): LiveKit Cloud's own simulator, `lk agent simulate`.

Import: `lk agent simulate export <run-id>` JSON → a run folder with one record per job, so the
same scoring and pages work. The simulator keeps the audio in LiveKit Cloud, so there is no
stereo recording here: the timeline is rebuilt from the agent's per-message timestamps, and
checks that need the recording are reported as not measured. LiveKit's own judge verdict and
metrics (WER, entity recall, heard latency) are stored per call and shown next to our checks.

Export: the session files → a `--scenarios` YAML (label, instructions, agent_expectations), so the
same sessions can be run on both engines.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from gf.agent.config import agent_config
from gf.config import settings
from gf.runner.batch import environment_info
from gf.sessions.schema import Session, load_all

ENGINE = "livekit-simulate"


# ---------------------------------------------------------------- export (sessions → scenarios)


def scenario_for(s: Session) -> dict[str, str]:
    c = s.caller
    facts = ", ".join(f"{k} = {v}" for k, v in c.facts.items())
    spoken = {k: _spoken(str(v)) for k, v in c.facts.items() if k in ("order_id", "zip")}
    spoken_txt = "; ".join(f'say {k} as "{v}"' for k, v in spoken.items())
    instructions = (
        f"You are {c.persona.name}, {c.persona.style} ({c.persona.accent}). "
        f"Facts you may use: {facts}. {spoken_txt}. "
        f"Goal: {c.goal} Stop when {c.stop_when}. "
        "Never invent order numbers, zips or amounts that are not in your facts. "
        "Speak in short sentences like a real phone caller."
    )
    req = " then ".join(
        f"{r.tool}({', '.join(f'{k}={v}' for k, v in r.args.items())})"
        for r in s.expected.tool_calls.required
    )
    forb = ", ".join(s.expected.tool_calls.forbidden)
    expectations = f"Outcome: {s.expected.outcome}."
    if req:
        expectations += f" The agent calls {req}."
    if forb:
        expectations += f" It must not call {forb}."
    if s.expected.notes:
        expectations += f" {s.expected.notes}"
    return {"label": s.title, "instructions": instructions, "agent_expectations": expectations}


def export_scenarios(out: Path) -> int:
    sessions = load_all(settings().sessions_dir)
    out.write_text(
        yaml.safe_dump([scenario_for(s) for s in sessions], sort_keys=False, allow_unicode=True)
    )
    return len(sessions)


def _spoken(value: str) -> str:
    words = {
        "0": "zero",
        "1": "one",
        "2": "two",
        "3": "three",
        "4": "four",
        "5": "five",
        "6": "six",
        "7": "seven",
        "8": "eight",
        "9": "nine",
    }
    out = []
    for ch in value:
        if ch.isdigit():
            out.append(words[ch])
        elif ch.isalpha():
            out.append(ch.upper())
    return " ".join(out)


# ---------------------------------------------------------------- import (export JSON → run)


def import_export(
    path: Path, *, run_id: str = "", backend_url: str | None = None
) -> dict[str, Any]:
    data = json.loads(path.read_text())
    run = data["run"]
    jobs = run.get("jobs", [])
    chat = (data.get("summary") or {}).get("chat_history", {})
    s = settings()
    run_id = run_id or f"lk-{run['id']}"
    run_dir = s.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg = agent_config()
    sessions = {x.title: x for x in load_all(s.sessions_dir)}

    # scenario label → session (existing by title, else synthesised from the scenario)
    by_label: dict[str, Session] = {}
    for sc in (run.get("scenario_group") or {}).get("scenarios", []):
        label = sc.get("label", "")
        by_label[label] = sessions.get(label) or _synth_session(label, sc, run_dir)
    attempt_no: dict[str, int] = {}
    calls = []
    for job in jobs:
        label = job.get("label", "")
        sess = by_label.get(label) or _synth_session(label, job, run_dir)
        by_label[label] = sess
        attempt_no[sess.id] = attempt_no.get(sess.id, 0) + 1
        n = attempt_no[sess.id]
        room = job.get("room_name") or job["id"]
        folder = run_dir / sess.id / str(n)
        folder.mkdir(parents=True, exist_ok=True)
        # the worker wrote its own events under runs/_adhoc/<room> (call id = room name)
        items = (chat.get(job["id"]) or {}).get("items", [])
        adhoc = s.runs_dir / "_adhoc" / room
        for name in ("agent_events.jsonl", "agent_session_report.json"):
            if (adhoc / name).exists() and not (folder / name).exists():
                shutil.copy(adhoc / name, folder / name)
        if not (folder / "agent_events.jsonl").exists():
            # no local worker record: rebuild the agent's events from the exported chat context
            (folder / "agent_events.jsonl").write_text(_agent_events(items, room))
        t0 = _first_ts(items) or _iso_ms(job.get("started_at"))
        _write_record(folder, job, items, t0, sess, n, cfg, backend_url or s.backend_url)
        calls.append(
            {
                "call_id": room,
                "session_id": sess.id,
                "attempt": n,
                "ended_by": "livekit-simulate",
                "end_reason": job.get("status"),
                "goal_met": (job.get("metrics") or {}).get("task_completion") == 1,
                "tool_calls": [],
                "agent_record_complete": (folder / "agent_events.jsonl").exists(),
                "runner_error": None,
                "record_dir": str(folder),
                "livekit_status": job.get("status"),
            }
        )
    manifest = {
        "run_id": run_id,
        "folder": str(run_dir),
        "started_at": run.get("created_at"),
        "agent_config_hash": cfg.config_hash,
        "sessions_hash": "lk-" + run["id"],
        "repeat": max(attempt_no.values(), default=1),
        "concurrency": None,
        "engine": ENGINE,
        "environment": environment_info(),
        "livekit_run": {
            k: run.get(k)
            for k in (
                "id",
                "status",
                "agent_name",
                "created_at",
                "ended_at",
                "job_count",
                "passed_count",
                "failed_count",
            )
        },
        "livekit_summary": {
            k: (data.get("summary") or {}).get(k)
            for k in ("passed", "failed", "going_well", "to_improve", "issues")
        },
        "sessions": [
            {"id": x.id, "title": x.title, "path": x.source_path} for x in by_label.values()
        ],
        "calls": calls,
        "duration_s": _duration(run),
    }
    from gf.environment import stamp

    stamp(
        run_id,
        run_dir,
        manifest,
        [x["id"] for x in manifest.get("sessions", [])],
        engine=ENGINE,
    )

    (run_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return manifest


def _write_record(
    folder: Path,
    job: dict,
    items: list[dict],
    t0: int,
    sess: Session,
    n: int,
    cfg,
    backend_url: str,
) -> None:
    room = job.get("room_name") or job["id"]
    msgs = [i["message"] for i in items if "message" in i]
    user_msgs = [m for m in msgs if m.get("role") == "USER"]
    asst_msgs = [m for m in msgs if m.get("role") == "ASSISTANT"]

    def span(m):
        met = m.get("metrics") or {}
        a, b = _iso_ms(met.get("started_speaking_at")), _iso_ms(met.get("stopped_speaking_at"))
        c = _iso_ms(m.get("created_at"))
        return (a or c or t0), (b or a or c or t0)

    # caller.json: in this engine the caller's words are only known as the agent heard them
    turns_said = []
    for m in user_msgs:
        a, b = span(m)
        turns_said.append({"ts_ms": a, "end_ms": b, "text": _text(m), "source": "agent_transcript"})
    turns_heard = [{"ts_ms": span(m)[0], "text": _text(m)} for m in asst_msgs]
    metrics = job.get("metrics") or {}
    caller = {
        "ended_by": "livekit-simulate",
        "end_reason": job.get("status", ""),
        "goal_met": metrics.get("task_completion") == 1,
        "gave_up": False,
        "summary": (job.get("error") or "")[:300],
        "turns_heard": turns_heard,
        "turns_said": turns_said,
        "heard_latency": [],
        "broke_character": False,
        "repeats": 0,
        "started_ms": t0,
        "ended_ms": _iso_ms(job.get("ended_at")) or t0,
        "reference_is_agent_transcript": True,
    }
    (folder / "caller.json").write_text(json.dumps(caller, indent=2))

    # backend log/state from the mock backend, if it still has this call id
    log, state = [], {}
    try:
        with httpx.Client(base_url=backend_url, timeout=5) as client:
            r = client.get(f"/calls/{room}/log")
            if r.status_code == 200:
                log = r.json() if isinstance(r.json(), list) else r.json().get("log", [])
            r = client.get(f"/calls/{room}/state")
            if r.status_code == 200:
                state = r.json()
    except httpx.HTTPError:
        pass
    (folder / "backend_log.json").write_text(json.dumps(log, indent=2))
    (folder / "backend_state.json").write_text(json.dumps(state, indent=2))

    # audio.json without audio: t0 for the record loader; no WAV
    duration_ms = max([span(m)[1] for m in msgs] + [t0]) - t0
    (folder / "audio.json").write_text(
        json.dumps(
            {
                "path": None,
                "sample_rate": None,
                "duration_s": round(duration_ms / 1000, 2),
                "t0_epoch_ms": t0,
                "channels": None,
            },
            indent=2,
        )
    )

    # timeline from the agent's own per-message timestamps (no recording in this engine)
    caller_seg = [(span(m)[0] - t0, span(m)[1] - t0) for m in user_msgs]
    agent_seg = [(span(m)[0] - t0, span(m)[1] - t0) for m in asst_msgs]
    (folder / "timeline.json").write_text(
        json.dumps(_timeline(t0, caller_seg, agent_seg, duration_ms, msgs, log), indent=2)
    )

    meta = {
        "call_id": room,
        "session_id": sess.id,
        "session_title": sess.title,
        "attempt": n,
        "engine": ENGINE,
        "room": room,
        "agent_config_hash": cfg.config_hash,
        "agent_models": cfg.models.model_dump(),
        "caller": sess.caller.model_dump(),
        "caller_params_unsupported": ["conditions.*", "voice", "pace", "llm"],
        "fixtures": sess.fixtures,
        "faults": [],
        "started_ms": t0,
        "ended_by": "livekit-simulate",
        "end_reason": job.get("status", ""),
        "agent_record_complete": (folder / "agent_events.jsonl").exists(),
        "livekit": {
            "job_id": job["id"],
            "status": job.get("status"),
            # the export marks a job the judge accepted as COMPLETED (or PASSED), else FAILED
            "passed": job.get("status") in ("STATUS_PASSED", "STATUS_COMPLETED"),
            "judge_reasoning": job.get("error") or "",
            "label": job.get("label"),
            "instructions": job.get("instructions"),
            "agent_expectations": job.get("agent_expectations"),
            "metrics": metrics,
            "usage": job.get("usage"),
        },
    }
    (folder / "meta.json").write_text(json.dumps(meta, indent=2, default=str))


def _agent_events(items: list[dict], call_id: str) -> str:
    """agent_events.jsonl lines (what the worker would have written) from the chat items."""
    lines = []
    for it in items:
        m = it.get("message")
        if not m:
            continue
        met = m.get("metrics") or {}
        role = "assistant" if m.get("role") == "ASSISTANT" else "user"
        ts = _iso_ms(met.get("started_speaking_at")) or _iso_ms(m.get("created_at")) or 0
        metrics = {
            k: v for k, v in met.items() if k not in ("started_speaking_at", "stopped_speaking_at")
        }
        for k in ("started_speaking_at", "stopped_speaking_at"):
            ms = _iso_ms(met.get(k))
            if ms:
                metrics[k] = ms / 1000
        if role == "user":
            lines.append(
                json.dumps(
                    {
                        "ts_ms": ts,
                        "kind": "user_transcript_final",
                        "call_id": call_id,
                        "text": _text(m),
                    }
                )
            )
        lines.append(
            json.dumps(
                {
                    "ts_ms": ts,
                    "kind": "message",
                    "call_id": call_id,
                    "role": role,
                    "text": _text(m),
                    "interrupted": bool(m.get("interrupted")),
                    "metrics": metrics,
                    "source": "livekit-export",
                }
            )
        )
    return "\n".join(lines) + ("\n" if lines else "")


def _timeline(
    t0: int, caller_seg, agent_seg, total_ms: int, msgs: list[dict], log: list[dict]
) -> dict[str, Any]:
    responses = []
    for i, (_cs, ce) in enumerate(caller_seg):
        next_caller = caller_seg[i + 1][0] if i + 1 < len(caller_seg) else total_ms + 1
        nxt = next((a for a in agent_seg if a[0] >= ce), None)
        if nxt is not None and nxt[0] < next_caller:
            responses.append(
                {"caller_end_ms": ce, "agent_start_ms": nxt[0], "latency_ms": nxt[0] - ce}
            )
        else:
            responses.append({"caller_end_ms": ce, "agent_start_ms": None, "latency_ms": None})
    busy = sorted(caller_seg + agent_seg)
    dead = [
        {"start_ms": e1, "end_ms": s2, "gap_ms": s2 - e1}
        for (_, e1), (s2, _) in zip(busy, busy[1:], strict=False)
        if s2 - e1 >= 3000
    ]
    lat = sorted(r["latency_ms"] for r in responses if r["latency_ms"] is not None)

    def pct(p):
        return lat[min(len(lat) - 1, max(0, round(p / 100 * (len(lat) - 1))))] if lat else None

    events: list[dict[str, Any]] = []
    for n, (s, e) in enumerate(caller_seg, 1):
        events.append({"t_ms": s, "end_ms": e, "kind": "caller_speech", "n": n})
    for n, (s, e) in enumerate(agent_seg, 1):
        events.append({"t_ms": s, "end_ms": e, "kind": "agent_speech", "n": n})
    for c in log:
        events.append(
            {
                "t_ms": int(c["ts_ms"]) - t0,
                "end_ms": int(c["ts_ms"]) - t0 + int(c.get("duration_ms", 0)),
                "kind": "tool_call",
                "tool": c["tool"],
                "args": c["args"],
                "status": c["status"],
                "ok": c["ok"],
                "response": c["response"],
                "id": c["id"],
            }
        )
    events.sort(key=lambda e: (e["t_ms"], e["kind"]))
    greeting_first = bool(agent_seg) and (not caller_seg or agent_seg[0][0] < caller_seg[0][0])
    return {
        "t0_ms": t0,
        "sample_rate": None,
        "source": "agent message timestamps (no recording in this engine)",
        "events": events,
        "ux": {
            "duration_ms": total_ms,
            "greeting_first": greeting_first,
            "first_agent_audio_ms": agent_seg[0][0] if agent_seg else None,
            "caller_segments": len(caller_seg),
            "agent_segments": len(agent_seg),
            "responses": responses,
            "latency_ms": {
                "n": len(lat),
                "p50": pct(50),
                "p95": pct(95),
                "max": lat[-1] if lat else None,
            },
            "dead_air": dead,
            "dead_air_total_ms": sum(d["gap_ms"] for d in dead),
            "talk_over": [],
            "barge_in": [],
            "caller_speech_ms": sum(e - s for s, e in caller_seg),
            "agent_speech_ms": sum(e - s for s, e in agent_seg),
            "not_measured": ["talk_over", "barge_in"],
        },
    }


def _synth_session(label: str, sc: dict, run_dir: Path) -> Session:
    """A session stand-in for a scenario that has no matching session file: keeps the label,
    the caller instructions as the goal, and no tool expectations (LiveKit's verdict is the
    signal). Written next to the run so the record stays loadable."""
    folder = run_dir / "_scenarios"
    folder.mkdir(parents=True, exist_ok=True)
    data = {
        "title": label or "LiveKit scenario",
        "caller": {
            "persona": {
                "name": "LiveKit simulated caller",
                "style": "as instructed",
                "accent": "en-US",
            },
            "facts": {},
            "goal": sc.get("instructions", "")[:800] or label,
            "stop_when": "the scenario ends",
        },
        "expected": {"outcome": "livekit_judge", "notes": sc.get("agent_expectations", "")},
        "engine": ENGINE,
    }
    path = folder / f"{_slug(label)}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    return Session.load(path)


def _slug(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "scenario"


def _text(m: dict) -> str:
    return " ".join(c.get("text", "") for c in m.get("content", []) if isinstance(c, dict)).strip()


def _iso_ms(value: str | None) -> int | None:
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        # trim nanoseconds to microseconds for fromisoformat
        if "." in v:
            head, tail = v.split(".", 1)
            frac, tz = tail[:-6], tail[-6:]
            v = f"{head}.{frac[:6]}{tz}"
        return int(datetime.fromisoformat(v).timestamp() * 1000)
    except ValueError:
        return None


def _first_ts(items: list[dict]) -> int | None:
    for i in items:
        m = i.get("message") or {}
        met = m.get("metrics") or {}
        t = _iso_ms(met.get("started_speaking_at")) or _iso_ms(m.get("created_at"))
        if t:
            return t
    return None


def _duration(run: dict) -> float | None:
    a, b = _iso_ms(run.get("created_at")), _iso_ms(run.get("ended_at"))
    return round((b - a) / 1000, 1) if a and b else None
