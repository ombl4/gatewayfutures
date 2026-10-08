"""`gf score <run_id>`: score every attempt (scores.json) and the run (summary.json).
Re-scoring never re-runs a call; it only reads the record folder."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from gf.config import ROOT, settings, thresholds, thresholds_hash
from gf.record.model import CallRecord
from gf.record.timeline import build_timeline
from gf.scoring import METHOD_VERSION
from gf.scoring.checks import Check, hard_fails
from gf.scoring.claims import check_claims
from gf.scoring.persona import judge_persona
from gf.scoring.speech import check_speech
from gf.scoring.stats import pass_summary
from gf.scoring.tools import check_tools
from gf.scoring.ux import check_quality, check_ux, check_validity
from gf.sessions.schema import Session


def method_version() -> str:
    """Scoring code version plus the thresholds file hash."""
    return f"{METHOD_VERSION}+th-{thresholds_hash()}"


def score_attempt(folder: str | Path, session: Session) -> dict[str, Any]:
    folder = Path(folder)
    record = CallRecord.load(folder)
    th = thresholds()
    tl_path = folder / "timeline.json"
    if record.audio_path:
        timeline = build_timeline(record, dead_air_ms=int(th.dead_air_gap_s * 1000))
        tl_path.write_text(json.dumps(timeline, indent=2))
    elif tl_path.exists():
        timeline = json.loads(tl_path.read_text())
    else:
        timeline = {"events": [], "ux": {}}

    checks: list[Check] = []
    checks += check_validity(record, session)
    checks.append(judge_persona(record, session))
    checks += check_tools(record, session)
    checks += check_claims(record)
    checks += check_speech(record, session)
    checks += check_ux(record, session, timeline, th)
    checks += check_livekit_judge(record, session)
    checks += check_quality(record, th)

    valid = all(c.passed for c in checks if c.group == "validity")
    fails = [c for c in hard_fails(checks) if c.group != "validity"]
    soft = [c for c in checks if not c.passed and c.severity == "soft"]
    ux = timeline.get("ux") or {}
    result = {
        "call_id": record.call_id,
        "session_id": record.session_id,
        "attempt": record.attempt,
        "valid": valid,
        "passed": valid and not fails,
        "hard_fails": [c.id for c in fails],
        "soft_flags": [c.id for c in soft],
        "failure_reason": fails[0].what_happened
        if fails
        else (
            "invalid: " + next(c.what_happened for c in checks if c.group == "validity")
            if not valid
            else ""
        ),
        "ended_by": record.ended_by,
        "goal_met": record.caller_result.get("goal_met"),
        "duration_ms": ux.get("duration_ms"),
        "latency_p50_ms": (ux.get("latency_ms") or {}).get("p50"),
        "latency_p95_ms": (ux.get("latency_ms") or {}).get("p95"),
        "wer": next((c.value for c in checks if c.id == "speech.wer"), None),
        "engine": record.meta.get("engine", "gf-caller"),
        "livekit": _livekit_brief(record.meta.get("livekit")),
        "dead_air_total_ms": ux.get("dead_air_total_ms"),
        "tool_calls": [(c.tool, c.status) for c in record.tool_calls],
        "checks": [c.model_dump() for c in checks],
        "method_version": method_version(),
    }
    (folder / "scores.json").write_text(json.dumps(result, indent=2, default=str))
    return result


def score_run(run_id: str) -> dict[str, Any]:
    run_dir = settings().runs_dir / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text())
    sessions = {
        s["id"]: Session.load(ROOT / s["path"] if not Path(s["path"]).is_absolute() else s["path"])
        for s in manifest["sessions"]
    }
    attempts: list[dict[str, Any]] = []
    for call in manifest["calls"]:
        sid = call["session_id"]
        folder = Path(call["record_dir"])
        if sid not in sessions or not (folder / "meta.json").exists():
            attempts.append(
                {
                    "call_id": call.get("call_id"),
                    "session_id": sid,
                    "attempt": call.get("attempt"),
                    "valid": False,
                    "passed": False,
                    "failure_reason": "no record",
                    "hard_fails": [],
                    "soft_flags": [],
                    "checks": [],
                }
            )
            continue
        attempts.append(score_attempt(folder, sessions[sid]))

    per_session = []
    for sid, session in sessions.items():
        rows = [a for a in attempts if a["session_id"] == sid]
        valid_rows = [a for a in rows if a["valid"]]
        ps = pass_summary([a["passed"] for a in valid_rows])
        lat = [a["latency_p95_ms"] for a in valid_rows if a.get("latency_p95_ms") is not None]
        reasons = Counter(a["failure_reason"] for a in valid_rows if not a["passed"])
        per_session.append(
            {
                "session_id": sid,
                "title": session.title,
                "conditions": session.caller.conditions.model_dump(),
                "expected_outcome": session.expected.outcome,
                "attempts": len(rows),
                "invalid": len(rows) - len(valid_rows),
                **ps,
                "latency_p95_ms_max": max(lat) if lat else None,
                "main_failure": reasons.most_common(1)[0][0] if reasons else "",
                "soft_flags": dict(Counter(f for a in valid_rows for f in a["soft_flags"])),
            }
        )
    valid_all = [a for a in attempts if a["valid"]]
    overall = pass_summary([a["passed"] for a in valid_all])
    all_lat = [a["latency_p95_ms"] for a in valid_all if a.get("latency_p95_ms") is not None]
    reasons = Counter(a["failure_reason"] for a in valid_all if not a["passed"])
    summary = {
        "run_id": run_id,
        "agent_config_hash": manifest.get("agent_config_hash"),
        "sessions_hash": manifest.get("sessions_hash"),
        "method_version": method_version(),
        "stamp": f"{manifest.get('agent_config_hash')}+{manifest.get('sessions_hash')}+{method_version()}",
        "engine": manifest.get("engine"),
        "started_at": manifest.get("started_at"),
        "duration_s": manifest.get("duration_s"),
        "repeat": manifest.get("repeat"),
        "calls": len(attempts),
        "invalid": len(attempts) - len(valid_all),
        "overall": overall,
        "latency_p95_ms": {
            "median": sorted(all_lat)[len(all_lat) // 2] if all_lat else None,
            "max": max(all_lat) if all_lat else None,
        },
        "top_failures": reasons.most_common(3),
        "flaky_sessions": [s["session_id"] for s in per_session if s["flaky"]],
        "sessions": per_session,
        "attempts": [{k: v for k, v in a.items() if k != "checks"} for a in attempts],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def _livekit_brief(lk: dict | None) -> dict | None:
    """The LiveKit simulator's own verdict and headline metrics, when the call came from it."""
    if not lk:
        return None
    m = lk.get("metrics") or {}
    return {
        "passed": bool(lk.get("passed")),
        "status": lk.get("status"),
        "wer": (m.get("stt") or {}).get("wer"),
        "entity_recognition": (m.get("stt") or {}).get("entity_recognition"),
        "heard_p95_ms": (m.get("conversation") or {}).get("heard_e2e_latency_p95_ms"),
        "overall_score": m.get("overall_score"),
    }


def check_livekit_judge(record, session) -> list[Check]:
    """The LiveKit simulator's own verdict, when the call came from it. Hard for a stand-in
    session (no expectations of our own), informational when our checks apply too."""
    lk = record.meta.get("livekit")
    if not lk:
        return []
    judge_only = session.expected.outcome == "livekit_judge"
    return [
        Check(
            id="livekit.verdict",
            group="livekit",
            label="LiveKit simulator judge",
            passed=bool(lk.get("passed")),
            severity="hard" if judge_only else "info",
            what_happened=(lk.get("judge_reasoning") or "").strip()[:400]
            or ("passed" if lk.get("passed") else str(lk.get("status"))),
            why_it_matters="An independent judge on the same call; where it disagrees with our checks is where to listen.",
            evidence={
                "status": lk.get("status"),
                "overall_score": (lk.get("metrics") or {}).get("overall_score"),
            },
            value=(lk.get("metrics") or {}).get("overall_score"),
        )
    ]
