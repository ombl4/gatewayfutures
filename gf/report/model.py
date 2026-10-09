"""Report model: plain dicts built from run folders, consumed by the Jinja templates.

Layers (docs/spec.md T6.1): L1 run summary, L2 per-session table, L3 per-call checks with
evidence, L4 timeline. Everything is derived from files on disk (manifest.json,
summary.json, scores.json, timeline.json, the call record) so the live UI and the static
report always show the same thing.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from gf.agent.config import agent_config
from gf.config import ROOT, settings, thresholds
from gf.environment import registry, tags_of
from gf.record.latency import STAGES, turn_latency
from gf.record.model import CallRecord
from gf.runner.batch import list_runs
from gf.scoring.checks import evidence_ms
from gf.scoring.stats import wilson
from gf.sessions.schema import Session, is_retired, load_all, retired_reason
from gf.sessions.taxonomy import areas_of, load_suites, session_file, suites_of

GROUPS = [
    ("validity", "Was the simulation valid?"),
    ("caller", "Did the simulated caller do its job?"),
    ("tools", "Did the agent do the right thing?"),
    ("claims", "Was the agent honest?"),
    ("speech", "Did the agent hear the caller?"),
    ("ux", "How did the call feel?"),
    ("quality", "Was the speech clean?"),
    ("livekit", "What LiveKit's judge said"),
]

TOOL_DOCS = {
    "lookup_order": {
        "args": "order_id, zip",
        "does": "Finds an order; the zip must match the order (this is the identity check).",
    },
    "issue_refund": {
        "args": "order_id, amount",
        "does": "Refunds a delivered order, once, up to the order total.",
    },
    "update_shipping_address": {
        "args": "order_id, new_address",
        "does": "Changes the address while the order is still processing.",
    },
    "escalate_to_human": {
        "args": "reason",
        "does": "Opens a ticket for a person and ends the call.",
    },
}


# ---------------------------------------------------------------- small helpers


def _json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def rate_class(rate: float | None) -> str:
    """green / amber / red per thresholds.yaml, grey when unknown."""
    if rate is None:
        return "grey"
    th = thresholds()
    if rate >= th.rate_green:
        return "green"
    if rate >= th.rate_amber:
        return "amber"
    return "red"


def fmt_ms(ms: int | float | None) -> str:
    if ms is None:
        return "–"
    return f"{ms / 1000:.1f} s"


def fmt_clock(ms: int | float | None) -> str:
    if ms is None:
        return "–"
    s = int(ms // 1000)
    return f"{s // 60}:{s % 60:02d}"


def conditions_chips(cond: dict[str, Any]) -> list[str]:
    chips = []
    if cond.get("noise"):
        chips.append(f"noise {cond['noise']}")
    if cond.get("phone_line"):
        chips.append("phone line")
    if cond.get("packet_loss"):
        chips.append(f"packet loss {cond['packet_loss']:.0%}")
    if cond.get("low_quality_mic"):
        chips.append("poor mic")
    if cond.get("interruptions"):
        chips.append(f"interrupts {cond['interruptions']:.0%}")
    return chips or ["clean line"]


def session_card(s: Session) -> dict[str, Any]:
    c = s.caller
    return {
        "id": s.id,
        "title": s.title,
        "persona": c.persona.model_dump(),
        "facts": c.facts,
        "goal": c.goal,
        "stop_when": c.stop_when,
        "opening": c.opening,
        "voice": c.voice,
        "pace": c.pace,
        "llm": c.llm.model_dump(),
        "conditions": c.conditions.model_dump(),
        "chips": conditions_chips(c.conditions.model_dump()),
        "limits": c.limits.model_dump(),
        "fixtures": s.fixtures,
        "faults": list(s.faults),
        "expected": s.expected.model_dump(),
        "engine": s.engine,
        "path": s.source_path,
        "generated": "/generated/" in (s.source_path or "").replace("\\", "/"),
        "retired": is_retired(s),
        "retired_reason": retired_reason(s),
        "areas": areas_of(s),
        "file": session_file(s),
    }


# ---------------------------------------------------------------- overview (flow)


def overview() -> dict[str, Any]:
    cfg = agent_config()
    sessions = load_all(settings().sessions_dir, include_retired=False)
    runs = [run_row(p.name) for p in list_runs()]
    accents = sorted({s.caller.persona.accent for s in sessions if s.caller.persona.accent})
    return {
        "agent": {
            "name": cfg.name,
            "persona": cfg.persona_name,
            "provider": cfg.provider,
            "stt": f"{cfg.models.stt.vendor} {cfg.models.stt.model}",
            "llm": f"{cfg.models.llm.vendor} {cfg.models.llm.model}",
            "tts": f"{cfg.models.tts.vendor} {cfg.models.tts.model}",
            "tools": cfg.tools,
            "hash": cfg.config_hash,
        },
        "sessions": {
            "count": len(sessions),
            "accents": accents,
            "goals": sorted({s.expected.outcome for s in sessions}),
        },
        "caller": {
            "engine": "gf-caller",
            "llm": sorted({s.caller.llm.model for s in sessions}),
            "conditions": sorted(
                {c for s in sessions for c in conditions_chips(s.caller.conditions.model_dump())}
            ),
        },
        "backend": {"tools": TOOL_DOCS, "fixtures": "orders_basic"},
        "runs": [r for r in runs if r["kind"] == "run"],
        "checks": [r for r in runs if r["kind"] != "run"],
        "latest": next((r for r in runs if r["kind"] == "run"), None),
    }


def environments_page() -> dict[str, Any]:
    """Every environment tag seen, its components and the runs made with it."""
    rows = []
    for e in registry():
        rows.append(
            e
            | {
                "runs": sorted(
                    e.get("runs", []), key=lambda r: r.get("started_at") or "", reverse=True
                )
            }
        )
    untagged = [
        r for r in (run_row(p.name) for p in list_runs()) if r.get("env_tag") in (None, "env-?")
    ]
    return {"environments": rows, "untagged": untagged}


def shell_info() -> dict[str, Any]:
    """What every page's left navigation shows about the agent under test."""
    cfg = agent_config()
    return {
        "name": cfg.name,
        "persona": cfg.persona_name,
        "provider": cfg.provider,
        "stt": cfg.models.stt.model,
        "llm": cfg.models.llm.model,
        "tts": cfg.models.tts.model,
        "tools": list(cfg.tools),
        "hash": cfg.config_hash,
    }


def run_metrics(summ: dict[str, Any]) -> dict[str, float | None]:
    """The four headline numbers of a run, from its summary only."""
    o = summ.get("overall") or {}
    valid = [a for a in summ.get("attempts", []) if a.get("valid")]
    tool_known = [a for a in valid if "tool_ok" in a]
    wers = [a["wer"] for a in valid if a.get("wer") is not None]
    wer_source = "recording"
    if not wers:
        wers = [
            a["livekit"]["wer"] for a in valid if (a.get("livekit") or {}).get("wer") is not None
        ]
        wer_source = "engine" if wers else "none"
    return {
        "rate": o.get("rate") if o.get("n") else None,
        "tool_rate": (sum(1 for a in tool_known if a["tool_ok"]) / len(tool_known))
        if tool_known
        else None,
        "wer": (sum(wers) / len(wers)) if wers else None,
        "wer_source": wer_source,
        "p95_ms": (summ.get("latency_p95_ms") or {}).get("median"),
    }


def kpi_cards(
    run_id: str, summ: dict[str, Any], prev: dict[str, Any] | None
) -> list[dict[str, Any]]:
    """KPI cards with a sparkline over the last real runs up to this one and the change
    against the previous comparable run (same stamp), when there is one."""
    history: list[tuple[str, dict[str, Any]]] = []
    for p in list_runs():
        man = _json(p / "manifest.json", {})
        if man.get("kind", "run") != "run":
            continue
        ps = summ if p.name == run_id else _json(p / "summary.json")
        if ps:
            history.append((ps.get("started_at") or man.get("started_at") or "", ps))
    history.sort(key=lambda x: x[0])
    upto = [h for h in history if h[0] <= (summ.get("started_at") or "\uffff")][-8:]
    series = [run_metrics(ps) for _, ps in upto]
    cur = run_metrics(summ)
    pm = run_metrics(prev) if prev else None
    prev_like = bool(prev) and prev.get("stamp") == summ.get("stamp")
    o = summ.get("overall") or {}
    th = thresholds()

    def card(key, label, fmt, sub, *, higher_is_better=True, cls="grey", note=""):
        vals = [m[key] for m in series]
        d = None
        if pm and pm.get(key) is not None and cur.get(key) is not None:
            d = cur[key] - pm[key]
            if abs(d) < (50 if key == "p95_ms" else 0.005):
                d = 0  # below display resolution: "no change", not "-0.0 s"
        return {
            "id": key,
            "label": label,
            "value": fmt(cur.get(key)),
            "sub": sub,
            "series": [v if v is not None else None for v in vals],
            "delta": d,
            "delta_text": _delta_text(key, d) if d is not None else None,
            "delta_class": (
                "same" if d == 0 else ("fixed" if (d > 0) == higher_is_better else "regressed")
            )
            if d is not None
            else "none",
            "comparable": prev is not None,
            "like_for_like": bool(prev_like),
            "prev_id": prev.get("run_id") if prev else None,
            "cls": cls,
            "note": note,
        }

    n_valid = o.get("n") or 0
    return [
        card(
            "rate",
            "Task success",
            lambda v: _pct(v),
            f"{o.get('passed', 0)} / {n_valid} valid calls · CI {_pct(o.get('ci_low'))}–{_pct(o.get('ci_high'))}"
            if n_valid
            else "no valid calls yet",
            cls=rate_class(cur["rate"]),
            note="A call passes when the backend shows the right actions and the agent told the truth.",
        ),
        card(
            "tool_rate",
            "Tool correctness",
            lambda v: _pct(v),
            "of valid calls, from the backend log"
            if cur["tool_rate"] is not None
            else "rescore this run to measure",
            cls=rate_class(cur["tool_rate"]),
            note="Judged on the order system's log, never on what the agent said.",
        ),
        card(
            "wer",
            "Speech WER",
            lambda v: _pct(v),
            {
                "recording": "lower is better · caller's words vs agent's transcript",
                "engine": "lower is better · as measured by the engine's judge",
                "none": "not measured for this engine",
            }[cur["wer_source"]],
            higher_is_better=False,
            cls="green"
            if cur["wer"] is not None and cur["wer"] <= 0.1
            else (
                "amber"
                if cur["wer"] is not None and cur["wer"] <= 0.25
                else ("red" if cur["wer"] is not None else "grey")
            ),
            note="What the caller said vs what the agent's speech recognition produced.",
        ),
        card(
            "p95_ms",
            "p95 reply latency",
            lambda v: fmt_ms(v),
            f"median session · flag over {th.latency_p95_warn_s:g} s",
            higher_is_better=False,
            cls=_lat_class(cur["p95_ms"]),
            note="Measured from the recording: caller stops speaking → agent audio starts.",
        ),
    ]


def by_area(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pass rate per area over the valid attempts of the sessions in that area."""
    from gf.sessions.taxonomy import MODIFIER_AREAS, PRIMARY_AREAS

    order = list(PRIMARY_AREAS) + ["other"] + list(MODIFIER_AREAS)
    acc: dict[str, dict[str, int]] = {}
    for r in rows:
        for a in r.get("areas", []):
            d = acc.setdefault(a, {"n": 0, "passed": 0, "sessions": 0})
            d["n"] += r.get("n") or 0
            d["passed"] += r.get("passed") or 0
            d["sessions"] += 1
    out = []
    for a in sorted(acc, key=lambda x: order.index(x) if x in order else 99):
        d = acc[a]
        rate, lo, hi = wilson(d["passed"], d["n"]) if d["n"] else (None, None, None)
        out.append(
            {
                "area": a,
                "kind": "primary" if a in PRIMARY_AREAS or a == "other" else "modifier",
                "n": d["n"],
                "passed": d["passed"],
                "sessions": d["sessions"],
                "rate": rate,
                "ci_low": lo,
                "ci_high": hi,
                "rate_class": rate_class(rate) if d["n"] else "grey",
            }
        )
    return out


def time_breakdown(summ: dict[str, Any]) -> list[dict[str, Any]]:
    """Where the time goes in a run: median over valid calls of each call's median per stage."""
    valid = [a for a in summ.get("attempts", []) if a.get("valid") and a.get("latency_breakdown")]
    out = []
    keys = [(k, label) for k, label, _d in STAGES] + [
        ("e2e_ms", "Reported end-to-end"),
        ("heard_ms", "Heard"),
        ("unaccounted_ms", "Unaccounted"),
    ]
    for k, label in keys:
        vals = sorted(
            a["latency_breakdown"][k] for a in valid if a["latency_breakdown"].get(k) is not None
        )
        out.append(
            {
                "key": k,
                "label": label,
                "p50": vals[len(vals) // 2] if vals else None,
                "n": len(vals),
            }
        )
    return out


def _pct(v: float | None) -> str:
    return "–" if v is None else f"{v * 100:.0f}%"


def _delta_text(key: str, d: float) -> str:
    if key == "p95_ms":
        return f"{'+' if d > 0 else ''}{d / 1000:.1f} s"
    return f"{'+' if d > 0 else ''}{d * 100:.0f} pts"


SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "simulation": 3}


def issues_for(r: dict[str, Any], limit: int = 6) -> dict[str, Any]:
    """Prioritised 'issues to investigate' for a run: hard failures first (honesty and wrong
    writes are critical), then flagged calls, then invalid simulations."""
    out = []
    for a in r["failing"]:
        ids = a.get("hard_fails") or []
        crit = any(i.startswith("claims.") or i == "tools.wrong_write" for i in ids)
        out.append(
            _issue(r, a, "critical" if crit else "high", a.get("failure_reason") or "failed")
        )
    for a in r["flagged"]:
        flags = [SOFT_LABELS.get(f, f) for f in a.get("soft_flags", [])]
        out.append(_issue(r, a, "medium", ", ".join(flags) or "flagged"))
    for a in r["invalid"]:
        out.append(_issue(r, a, "simulation", a.get("failure_reason") or "invalid simulation"))
    out.sort(key=lambda i: (SEVERITY_ORDER[i["severity"]], i["session_id"], i["attempt"]))
    return {"items": out[:limit], "total": len(out)}


def _issue(r, a, severity, text) -> dict[str, Any]:
    text = text.removeprefix("invalid: ")
    head = text.split(";")[0].strip()
    row = next((x for x in r.get("sessions", []) if x["session_id"] == a["session_id"]), {})
    return {
        "severity": severity,
        "title": _clip(head if "_" in head.split(" ")[0] else head[:1].upper() + head[1:], 72),
        "detail": text if text != head else "",
        "run_id": r["run_id"],
        "session_id": a["session_id"],
        "session_title": a.get("title", a["session_id"]),
        "attempt": a["attempt"],
        "t_ms": a.get("issue_t_ms"),
        "has_audio": a.get("has_audio", True),
        "duration_ms": a.get("duration_ms"),
        "retired": row.get("retired", False),
        "retired_reason": row.get("retired_reason", ""),
    }


def _clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    return cut + "…"


def select_call(r: dict[str, Any], call: str | None = None) -> tuple[str, int] | None:
    """Which call the embedded inspector shows: an explicit 'session/attempt', else the top
    issue, else the reference pass, else the first call."""
    atts = r["summary"].get("attempts", [])
    if call and "/" in call:
        sid, n = call.rsplit("/", 1)
        if any(a["session_id"] == sid and str(a["attempt"]) == n for a in atts):
            return sid, int(n)
    issues = issues_for(r, limit=1)["items"]
    if issues:
        return issues[0]["session_id"], issues[0]["attempt"]
    if r.get("reference"):
        return r["reference"]["session_id"], r["reference"]["attempt"]
    if atts:
        return atts[0]["session_id"], atts[0]["attempt"]
    return None


def variants_page() -> list[dict[str, Any]]:
    from gf.agent.variants import VARIANTS

    return [
        {
            "name": v.name,
            "title": v.title,
            "purpose": v.purpose,
            "expected_check": v.expected_failing_check,
            "removes": list(v.remove_tools),
        }
        for v in VARIANTS.values()
    ]


def scoring_page() -> dict[str, Any]:
    """Catalogue of every check, taken from a scored record (labels and 'why' live in code)."""
    src = None
    for p in list_runs():
        for sc in sorted(p.glob("*/*/scores.json")):
            src = _json(sc)
            if src and src.get("checks"):
                break
        if src and src.get("checks"):
            break
    if not (src and src.get("checks")):
        src = _json(ROOT / "fixtures" / "records" / "refund-basic" / "scores.json", {})
    seen: dict[str, dict[str, Any]] = {}
    for c in src.get("checks", []):
        key = (
            c["id"].rsplit(".", 1)[0]
            if c["id"].startswith(("tools.required", "tools.forbidden", "state."))
            else c["id"]
        )
        label = c["label"]
        if c["id"].startswith("tools.required"):
            label = "Required tool calls with the expected arguments"
        elif c["id"].startswith("tools.forbidden"):
            label = "Forbidden tools were not called"
        elif c["id"].startswith("state."):
            label = "Final state assertions from the session"
        seen.setdefault(key, c | {"label": label})
    groups = []
    for gid, title in GROUPS:
        cs = [c for c in seen.values() if c["group"] == gid]
        if cs:
            groups.append({"id": gid, "title": title, "checks": cs})
    return {"groups": groups, "thresholds": thresholds().model_dump(), "variants": variants_page()}


def agent_page() -> dict[str, Any]:
    cfg = agent_config()
    return {"cfg": cfg.model_dump(), "tools": TOOL_DOCS, "hash": cfg.config_hash}


def backend_page() -> dict[str, Any]:
    import yaml

    fx_path = ROOT / "fixtures" / "orders_basic.yaml"
    fx = yaml.safe_load(fx_path.read_text()) if fx_path.exists() else {}
    rules = [
        ("lookup_order", "The zip must match the order, otherwise 'order not found' (404)."),
        (
            "issue_refund",
            "Only delivered orders; amount up to the order total; once per order (409 otherwise).",
        ),
        ("update_shipping_address", "Only while the order is processing (409 after it ships)."),
        ("escalate_to_human", "Creates a ticket and marks the call ended."),
        (
            "all tools",
            "Strict argument formats (GW-#####, amounts to 2 dp); problems are logged as arg_problems (422).",
        ),
        (
            "faults",
            "A session can inject latency, a 500, a timeout or a rejection on the nth call to a tool.",
        ),
    ]
    return {"fixtures": fx, "tools": TOOL_DOCS, "rules": rules}


def sessions_page() -> dict[str, Any]:
    """Active sessions (offered for runs) and retired ones (kept for old runs), separately,
    with their areas and suites."""
    sessions = load_all(settings().sessions_dir)
    history = _session_history()
    suites = load_suites(settings().sessions_dir)
    cards = [
        session_card(s) | {"history": history.get(s.id, []), "suites": suites_of(s, suites)}
        for s in sessions
    ]
    active = [c for c in cards if not c["retired"]]
    areas = sorted({a for c in active for a in c["areas"]})
    return {
        "sessions": active,
        "retired": [c for c in cards if c["retired"]],
        "suites": sorted(suites),
        "areas": areas,
        "suite_counts": {su: sum(1 for c in active if su in c["suites"]) for su in suites},
        "area_counts": {a: sum(1 for c in active if a in c["areas"]) for a in areas},
    }


def session_page(session_id: str) -> dict[str, Any]:
    s = next(x for x in load_all(settings().sessions_dir) if x.id == session_id)
    yaml_text = (
        Path(s.source_path).read_text() if s.source_path and Path(s.source_path).exists() else ""
    )
    requirements = requirements_of(s)
    return {
        "requirements": requirements,
        "session": session_card(s) | {"suites": suites_of(s, load_suites(settings().sessions_dir))},
        "history": _session_history().get(s.id, []),
        "yaml": yaml_text,
        "all_suites": sorted(load_suites(settings().sessions_dir)),
    }


def _session_history() -> dict[str, list[dict[str, Any]]]:
    """Per session: outcome strip over the last runs (oldest → newest)."""
    hist: dict[str, list[dict[str, Any]]] = {}
    for p in reversed(list_runs()[:10]):
        summ = _json(p / "summary.json")
        if not summ:
            continue
        man = _json(p / "manifest.json", {})
        for row in summ["sessions"]:
            hist.setdefault(row["session_id"], []).append(
                {
                    "run_id": p.name,
                    "started_at": man.get("started_at"),
                    "kind": man.get("kind", "run"),
                    "passed": row["passed"],
                    "n": row["n"],
                    "invalid": row["invalid"],
                    "attempts": [
                        a for a in summ.get("attempts", []) if a["session_id"] == row["session_id"]
                    ],
                }
            )
    return hist


# ---------------------------------------------------------------- runs


def run_row(run_id: str) -> dict[str, Any]:
    run_dir = settings().runs_dir / run_id
    man = _json(run_dir / "manifest.json", {})
    summ = _json(run_dir / "summary.json")
    o = (summ or {}).get("overall", {})
    return {
        "run_id": run_id,
        "started_at": man.get("started_at"),
        "duration_s": man.get("duration_s"),
        "calls": len(man.get("calls", [])),
        "sessions": len(man.get("sessions", [])),
        "repeat": man.get("repeat"),
        "engine": man.get("engine"),
        "stamp": (summ or {}).get("stamp"),
        "scored": summ is not None,
        "n": o.get("n"),
        "passed": o.get("passed"),
        "rate": o.get("rate"),
        "ci_low": o.get("ci_low"),
        "ci_high": o.get("ci_high"),
        "invalid": (summ or {}).get("invalid"),
        "rate_class": rate_class(o.get("rate")) if summ else "grey",
        "p95_ms": ((summ or {}).get("latency_p95_ms") or {}).get("median"),
        "flaky": len((summ or {}).get("flaky_sessions") or []),
        "kind": man.get("kind", "run"),
        "variant": man.get("agent_variant"),
        **tags_of(man),
    }


def _stale(summary: Path, manifest: Path) -> bool:
    try:
        return summary.stat().st_mtime < manifest.stat().st_mtime
    except OSError:
        return True


def run_report(run_id: str, *, in_progress: bool = False) -> dict[str, Any]:
    """L1 + L2. Scores the run itself when summary.json is missing or older than the
    manifest (a run still in progress, or re-run from the CLI)."""
    run_dir = settings().runs_dir / run_id
    man = _json(run_dir / "manifest.json", {})
    summ = _json(run_dir / "summary.json")
    if summ is None or _stale(run_dir / "summary.json", run_dir / "manifest.json"):
        from gf.scoring.score import score_run

        summ = score_run(run_id) if man.get("calls") else _empty_summary(man)
    attempts = {(a["session_id"], a["attempt"]): a for a in summ["attempts"]}
    sessions = {s.id: s for s in load_all(settings().sessions_dir)}

    # previous real run: the baseline for deltas; "comparable" (same stamp) decides whether
    # fixed / regressed marks are shown with confidence
    prev = None
    prev_like = False
    prev_env_same = prev_set_same = None
    if man.get("kind", "run") == "run":
        for p in list_runs():
            if p.name == run_id:
                continue
            pm = _json(p / "manifest.json", {})
            if pm.get("kind", "run") != "run":
                continue  # detector checks are never a baseline
            ps = _json(p / "summary.json")
            if ps and ps.get("started_at", "") < summ.get("started_at", ""):
                prev = ps
                prev_tags, cur_tags = tags_of(pm), tags_of(man)
                known = {prev_tags["env_tag"], cur_tags["env_tag"]}.isdisjoint({None, "env-?"})
                prev_like = (
                    known
                    and prev_tags["env_tag"] == cur_tags["env_tag"]
                    and prev_tags["set_tag"] == cur_tags["set_tag"]
                ) or (bool(summ.get("stamp")) and ps.get("stamp") == summ.get("stamp"))
                prev_env_same = prev_tags["env_tag"] == cur_tags["env_tag"]
                prev_set_same = prev_tags["set_tag"] == cur_tags["set_tag"]
                break
    prev_rows = {r["session_id"]: r for r in (prev or {}).get("sessions", [])}

    rows = []
    for row in summ["sessions"]:
        sid = row["session_id"]
        sess = sessions.get(sid)
        att = sorted((a for (s, _n), a in attempts.items() if s == sid), key=lambda a: a["attempt"])
        strip = [
            {
                "attempt": a["attempt"],
                "mark": "invalid" if not a["valid"] else ("pass" if a["passed"] else "fail"),
                "reason": a.get("failure_reason", ""),
                "soft": a.get("soft_flags", []),
            }
            for a in att
        ]
        done_attempts = {a["attempt"] for a in att}
        for n in range(1, int(man.get("repeat") or 0) + 1):
            if n not in done_attempts:
                strip.append({"attempt": n, "mark": "pending", "reason": "", "soft": []})
        strip.sort(key=lambda x: x["attempt"])
        vs = "new"
        if sid in prev_rows:
            pr = prev_rows[sid]
            if pr["n"] and row["n"]:
                if pr["passed"] == pr["n"] and row["passed"] < row["n"]:
                    vs = "regressed"
                elif pr["passed"] < pr["n"] and row["passed"] == row["n"]:
                    vs = "fixed"
                else:
                    vs = "same"
        soft = Counter()
        for a in att:
            soft.update(a.get("soft_flags", []))
        rows.append(
            row
            | {
                "chips": conditions_chips(row.get("conditions", {})),
                "persona": sess.caller.persona.model_dump() if sess else {},
                "goal": sess.caller.goal if sess else "",
                "strip": strip,
                "vs": vs,
                "rate_class": rate_class(row["rate"] if row["n"] else None),
                "soft_counts": dict(soft),
                "p95_class": _lat_class(row.get("latency_p95_ms_max")),
                "n_fail": sum(1 for a in att if a["valid"] and not a["passed"]),
                "n_invalid": sum(1 for a in att if not a["valid"]),
                "attempts_detail": [
                    a | {"flags": [SOFT_LABELS.get(f, f) for f in a.get("soft_flags", [])]}
                    for a in att
                ],
                "expected_outcome": row.get("expected_outcome")
                or (sess.expected.outcome if sess else ""),
                "expected_tools": [t.tool for t in sess.expected.tool_calls.required]
                if sess
                else [],
                "facts": sess.caller.facts if sess else {},
                "areas": areas_of(sess) if sess else [],
                "retired": is_retired(sess) if sess else False,
                "retired_reason": retired_reason(sess) if sess else "",
            }
        )

    def _attempt_rows(pred):
        out = []
        for a in summ["attempts"]:
            if pred(a):
                out.append(
                    a
                    | {
                        "title": next(
                            (r["title"] for r in rows if r["session_id"] == a["session_id"]), ""
                        )
                    }
                )
        return out

    failing = _attempt_rows(lambda a: a["valid"] and not a["passed"])
    invalid = _attempt_rows(lambda a: not a["valid"])
    flagged = _attempt_rows(lambda a: a["valid"] and a["passed"] and a.get("soft_flags"))
    reference = next(
        (a for a in summ["attempts"] if a["valid"] and a["passed"] and not a.get("soft_flags")),
        None,
    ) or next((a for a in summ["attempts"] if a["valid"] and a["passed"]), None)
    o = summ["overall"]
    lat = summ.get("latency_p95_ms") or {}
    out = {
        "run_id": run_id,
        "manifest": man,
        "summary": summ,
        "stamp": summ.get("stamp"),
        "started_at": man.get("started_at"),
        "duration_s": man.get("duration_s"),
        "engine": man.get("engine"),
        "repeat": man.get("repeat"),
        "concurrency": man.get("concurrency"),
        "overall": o | {"rate_class": rate_class(o.get("rate") if o.get("n") else None)},
        "invalid_count": summ.get("invalid", 0),
        "p95_median_ms": lat.get("median"),
        "p95_max_ms": lat.get("max"),
        "p95_class": _lat_class(lat.get("median")),
        "top_failures": summ.get("top_failures", []),
        "flaky": summ.get("flaky_sessions", []),
        "sessions": rows,
        "failing": failing,
        "invalid": invalid,
        "flagged": flagged,
        "reference": reference,
        "previous": {
            "run_id": prev.get("run_id"),
            "rate": prev["overall"].get("rate"),
            "like_for_like": prev_like,
            "env_same": prev_env_same,
            "set_same": prev_set_same,
        }
        if prev
        else None,
        "regressed": [r for r in rows if r["vs"] == "regressed"],
        "fixed": [r for r in rows if r["vs"] == "fixed"],
        "soft_labels": SOFT_LABELS,
        "in_progress": in_progress,
        "planned_calls": len(man.get("sessions", [])) * int(man.get("repeat") or 0),
        "kind": man.get("kind", "run"),
        "variant": man.get("agent_variant"),
        "tags": tags_of(man),
        "env_components": man.get("env_components"),
        "detector": _detector_verdict(man, summ, rows)
        if man.get("kind") == "detector_check"
        else None,
    }
    out["simulation"] = simulation_quality(summ)
    out["by_area"] = by_area(rows)
    out["suite"] = man.get("suite")
    out["kpis"] = kpi_cards(run_id, summ, prev) if man.get("kind", "run") == "run" else []
    out["time_breakdown"] = time_breakdown(summ)
    out["issues"] = issues_for(out)
    out["valid_count"] = sum(1 for a in summ.get("attempts", []) if a.get("valid"))
    return out


def _detector_verdict(
    man: dict[str, Any], summ: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Did every valid call fail on the check the variant is meant to trigger?"""
    from gf.agent.variants import VARIANTS, applicable

    expected = man.get("expected_failing_check") or ""
    titles = {r["session_id"]: r["title"] for r in rows}
    tools_of = {r["session_id"]: r.get("expected_tools", []) for r in rows}
    variant = VARIANTS.get(man.get("agent_variant") or "")
    per = []
    for a in summ.get("attempts", []):
        fired = expected in (a.get("hard_fails") or [])
        reason = ""
        inconclusive = False
        na = variant is not None and not applicable(variant, tools_of.get(a["session_id"], []))
        if na:
            reason = (
                "not applicable: this session never asks for "
                + " or ".join(variant.remove_tools)
                + ", so a false 'done' cannot occur here"
            )
        folder = next(
            (
                c["record_dir"]
                for c in man.get("calls", [])
                if c["session_id"] == a["session_id"] and c["attempt"] == a["attempt"]
            ),
            None,
        )
        if folder:
            sc = _json(Path(folder) / "scores.json", {})
            ch = next((c for c in sc.get("checks", []) if c["id"] == expected), None)
            reason = (ch or {}).get("what_happened", "") or a.get("failure_reason", "")
            if not fired and ch and expected == "claims.claimed_without_acting":
                # no claim at all: the call never reached the point of confirming an action
                inconclusive = not (ch.get("evidence") or {}).get("claims")
                if inconclusive:
                    reason = (
                        "inconclusive: the agent never claimed an action (the call did not reach "
                        "confirmation: "
                        + (a.get("failure_reason") or a.get("ended_by") or "")
                        + "); rerun"
                    )
        per.append(
            {
                "session_id": a["session_id"],
                "attempt": a["attempt"],
                "title": titles.get(a["session_id"], a["session_id"]),
                "valid": a.get("valid", True),
                "fired": fired,
                "inconclusive": inconclusive,
                "not_applicable": na,
                "reason": reason,
            }
        )
    valid = [p for p in per if p["valid"] and not p["not_applicable"]]
    conclusive = [p for p in valid if not p["inconclusive"]]
    return {
        "variant": man.get("agent_variant"),
        "expected_check": expected,
        "n": len(valid),
        "n_fired": sum(1 for p in valid if p["fired"]),
        "n_inconclusive": sum(1 for p in valid if p["inconclusive"]),
        "n_not_applicable": sum(1 for p in per if p["not_applicable"]),
        "caught": bool(conclusive) and all(p["fired"] for p in conclusive),
        "missed": any(not p["fired"] for p in conclusive),
        "per_attempt": per,
    }


def _empty_summary(man: dict[str, Any]) -> dict[str, Any]:
    """A run with no finished call yet (just started from the UI)."""
    return {
        "run_id": man.get("run_id"),
        "stamp": None,
        "started_at": man.get("started_at"),
        "calls": 0,
        "invalid": 0,
        "overall": {
            "n": 0,
            "passed": 0,
            "rate": None,
            "ci_low": None,
            "ci_high": None,
            "flaky": False,
        },
        "latency_p95_ms": {},
        "top_failures": [],
        "flaky_sessions": [],
        "sessions": [
            {
                "session_id": s["id"],
                "title": s["title"],
                "conditions": {},
                "expected_outcome": "",
                "attempts": 0,
                "invalid": 0,
                "n": 0,
                "passed": 0,
                "rate": 0.0,
                "ci_low": 0.0,
                "ci_high": 0.0,
                "flaky": False,
                "latency_p95_ms_max": None,
                "main_failure": "",
                "soft_flags": {},
            }
            for s in man.get("sessions", [])
        ],
        "attempts": [],
    }


SOFT_LABELS = {
    "speech.entities": "a key fact was misheard",
    "speech.misheard_to_tool": "misheard value reached a tool",
    "ux.latency_p95": "slow replies",
    "ux.dead_air": "dead air",
    "ux.talk_over": "talked over the caller",
    "ux.barge_in": "slow to stop when interrupted",
    "ux.repeats": "caller had to repeat",
    "ux.would_hang_up": "a real caller would hang up",
    "ux.greeting_first": "caller spoke first",
    "tools.arg_problems": "malformed tool arguments",
    "quality.stutter": "stutter",
    "quality.tool_name_leak": "spoke a tool name",
    "quality.truncated": "cut-off sentence",
    "quality.repeated_question": "re-asked a question",
    "quality.filler_only_turns": "filler-only turns",
}


def _lat_class(p95_ms: int | None) -> str:
    if p95_ms is None:
        return "grey"
    th = thresholds()
    if p95_ms > th.latency_p95_fail_s * 1000:
        return "red"
    if p95_ms > th.latency_p95_warn_s * 1000:
        return "amber"
    return "green"


# ---------------------------------------------------------------- one call


def call_folder(run_id: str, session_id: str, attempt: int) -> Path:
    return settings().runs_dir / run_id / session_id / str(attempt)


def call_report(
    run_id: str, session_id: str, attempt: int, *, audio_href: str | None = None
) -> dict[str, Any]:
    folder = call_folder(run_id, session_id, attempt)
    return call_report_from_folder(folder, audio_href=audio_href, run_id=run_id)


def call_report_from_folder(
    folder: Path, *, audio_href: str | None = None, run_id: str | None = None
) -> dict[str, Any]:
    record = CallRecord.load(folder)
    scores = _json(folder / "scores.json")
    timeline = _json(folder / "timeline.json")
    sessions = {s.id: s for s in load_all(settings().sessions_dir)}
    session = sessions.get(record.session_id)
    if scores is None or timeline is None:
        from gf.record.timeline import write_timeline
        from gf.scoring.score import score_attempt

        if session is None:
            raise FileNotFoundError(f"session {record.session_id} not in sessions/")
        timeline = write_timeline(folder)
        scores = score_attempt(folder, session)
    checks = scores.get("checks", [])
    if not checks and session is not None:  # summary rows carry no checks; re-score for the page
        from gf.scoring.score import score_attempt

        scores = score_attempt(folder, session)
        checks = scores["checks"]

    groups = []
    for gid, title in GROUPS:
        cs = [c for c in checks if c["group"] == gid]
        if not cs:
            continue
        fails = [c for c in cs if not c["passed"] and c["severity"] == "hard"]
        soft = [c for c in cs if not c["passed"] and c["severity"] == "soft"]
        groups.append(
            {
                "id": gid,
                "title": title,
                "checks": [
                    c | {"jump_ms": evidence_ms(c), "tag": _tag(c["id"]), "brief": _brief(c)}
                    for c in cs
                ],
                "state": "fail" if fails else ("warn" if soft else "pass"),
                "n_fail": len(fails),
                "n_warn": len(soft),
            }
        )

    verdict = (
        "invalid" if not scores.get("valid", True) else ("pass" if scores.get("passed") else "fail")
    )
    ux = timeline.get("ux", {})
    turns = _merge_turns(record, timeline)
    annotate_turns(turns, checks, thresholds().model_dump())
    env = envelopes(record.audio_path) if record.audio_path else None
    meta = record.meta
    caller = meta.get("caller", {})
    cond_measured = (
        (record.caller_result.get("conditions") or {})
        if isinstance(record.caller_result, dict)
        else {}
    )
    return {
        "run_id": run_id,
        "call_id": record.call_id,
        "session_id": record.session_id,
        "attempt": record.attempt,
        "title": meta.get("session_title") or (session.title if session else record.session_id),
        "verdict": verdict,
        "failure_reason": scores.get("failure_reason", ""),
        "soft_flags": [SOFT_LABELS.get(f, f) for f in scores.get("soft_flags", [])],
        "duration_ms": ux.get("duration_ms") or int(record.audio_duration_s * 1000),
        "ended_by": record.ended_by,
        "end_reason": record.end_reason,
        "goal_met": record.caller_result.get("goal_met"),
        "gave_up": record.caller_result.get("gave_up"),
        "caller_summary": record.caller_result.get("summary"),
        "latency": ux.get("latency_ms", {}),
        "latency_class": _lat_class((ux.get("latency_ms") or {}).get("p95")),
        "agent_reported_latency_s": _agent_latency(record),
        "wer": scores.get("wer"),
        "dead_air_total_ms": ux.get("dead_air_total_ms", 0),
        "talk_over": len(ux.get("talk_over", [])),
        "barge_in": ux.get("barge_in", []),
        "greeting_first": ux.get("greeting_first"),
        "first_agent_audio_ms": ux.get("first_agent_audio_ms"),
        "groups": groups,
        "grading": grading_steps(session, groups, scores),
        "caller_score": caller_score(checks, scores),
        "turns": turns,
        "tool_calls": [t.model_dump() for t in record.tool_calls],
        "timeline": timeline,
        "envelopes": env,
        "audio_href": audio_href,
        "session": session_card(session)
        if session
        else {"id": record.session_id, "title": record.session_id},
        "caller": caller,
        "conditions_measured": cond_measured,
        "unsupported": meta.get("caller_params_unsupported", []),
        "agent_models": meta.get("agent_models", {}),
        "agent_config_hash": meta.get("agent_config_hash"),
        "room": meta.get("room"),
        "backend_state": record.backend_state,
        "agent_metrics": _agent_turn_metrics(record),
        "engine": meta.get("engine", "gf-caller"),
        "livekit": meta.get("livekit"),
        "files": sorted(p.name for p in Path(folder).iterdir()),
        "folder": str(folder),
        "thresholds": thresholds().model_dump(),
        "latency_turns": turn_latency(record, timeline),
        "neighbours": _neighbours(run_id, record.session_id, record.attempt),
        "started_at": meta.get("started_at"),
        "n_fail": sum(g["n_fail"] for g in groups),
        "n_warn": sum(g["n_warn"] for g in groups),
        "n_tools_bad": sum(1 for t in record.tool_calls if not t.ok),
    }


def _tag(check_id: str) -> str:
    """Short code tag for a check: the id without its group (`ux.latency_p95` → `latency_p95`,
    `tools.required.issue_refund` → `required:issue_refund`)."""
    parts = check_id.split(".")
    return ":".join(parts[1:]) if len(parts) > 1 else check_id


def _brief(check: dict[str, Any]) -> str:
    """The measured value in a few characters, for the tag."""
    v = check.get("value")
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, int | float):
        if "wer" in check["id"] or ("rate" in check["id"] and 0 <= v <= 1):
            return f"{v * 100:.0f}%"
        if abs(v) >= 1000:
            return f"{v / 1000:.1f} s"
        if isinstance(v, float):
            return f"{v:.2f}".rstrip("0").rstrip(".")
        return str(v)
    return str(v)[:14]


STEP_GROUPS = {
    "validity": (
        "1",
        "Simulated caller: did it do its job?",
        "The caller's score, separate from the agent's. A hard failure here makes the call invalid (it does not count for or against the agent); soft ones flag the caller.",
    ),
    "tools": (
        "2",
        "Did the agent do what the session asks?",
        "Judged on the order system's log and final state, never on what the agent said.",
    ),
    "claims": (
        "3",
        "Was the agent honest?",
        "Every claim of a completed action must match a successful backend write earlier in the call.",
    ),
}
SOFT_GROUPS = ("speech", "ux", "quality", "livekit")


def requirements_of(session: Session | None) -> list[dict[str, Any]]:
    """The session's expectation written out as requirements (what step 2 tests)."""
    if session is None:
        return []
    tc = session.expected.tool_calls
    reqs: list[dict[str, Any]] = [
        {
            "kind": "outcome",
            "text": f"Expected outcome: {session.expected.outcome.replace('_', ' ')}",
        }
    ]
    for r in tc.required:
        args = ", ".join(f"{k} = {v}" for k, v in r.args.items()) or "any arguments"
        reqs.append(
            {
                "kind": "required",
                "text": f"Must call {r.tool} with {args}",
                "check": f"tools.required.{r.tool}",
            }
        )
    if tc.order:
        reqs.append(
            {
                "kind": "order",
                "text": "In this order: " + " → ".join(tc.order),
                "check": "tools.order",
            }
        )
    for t in tc.forbidden:
        reqs.append(
            {"kind": "forbidden", "text": f"Must not call {t}", "check": f"tools.forbidden.{t}"}
        )
    for expr in session.expected.final_state:
        reqs.append({"kind": "state", "text": f"Final state: {expr}", "check": "state."})
    for text, check in (
        ("No writes the caller did not ask for", "tools.wrong_write"),
        ("No extra successful calls beyond the scenario", "tools.extra"),
        ("Well-formed arguments on every call", "tools.arg_problems"),
    ):
        reqs.append({"kind": "always", "text": text, "check": check})
    return reqs


def caller_score(checks: list[dict[str, Any]], scores: dict[str, Any]) -> dict[str, Any]:
    """The simulated caller's own score: broken (invalid), flagged, or in character."""
    items = [c for c in checks if c["group"] in ("validity", "caller")]
    hard = [c for c in items if not c["passed"] and c["severity"] == "hard"]
    soft = [c for c in items if not c["passed"] and c["severity"] in ("soft", "info")]
    if hard or not scores.get("valid", True):
        state = "broken"
        text = "simulation broke: " + "; ".join(c["what_happened"] for c in hard)[:160]
    elif soft:
        state, text = "flagged", "; ".join(c["label"] for c in soft)[:160]
    else:
        state, text = "ok", "in character, heard the agent, goal stated early, ended for a reason"
    return {"state": state, "text": text, "items": items}


def simulation_quality(summ: dict[str, Any]) -> dict[str, Any]:
    """Run-level view of the simulator: how many calls were sound, caller flags, hearing faults."""
    atts = summ.get("attempts", [])
    known = [a for a in atts if "caller_ok" in a]
    return {
        "calls": len(atts),
        "valid": sum(1 for a in atts if a.get("valid")),
        "caller_ok": sum(1 for a in known if a["caller_ok"]),
        "known": len(known),
        "hearing_faults": sum(1 for a in atts if a.get("hearing_fault")),
        "judge_disagreed": sum(
            1 for a in atts if "validity.persona_judge" in (a.get("caller_flags") or [])
        ),
        "flags": sorted({f for a in atts for f in (a.get("caller_flags") or [])}),
    }


def grading_steps(
    session: Session | None, groups: list[dict[str, Any]], scores: dict[str, Any]
) -> dict[str, Any]:
    """The verdict explained as numbered steps in the order it is decided."""
    by_id = {g["id"]: g for g in groups}
    steps = []
    for gid, (num, title, rule) in STEP_GROUPS.items():
        g = by_id.get(gid)
        checks = g["checks"] if g else []
        if gid == "validity":
            checks = checks + (by_id.get("caller") or {}).get("checks", [])
        fails = [c for c in checks if not c["passed"] and c["severity"] == "hard"]
        steps.append(
            {
                "num": num,
                "title": title,
                "rule": rule,
                "state": "fail" if fails else ("pass" if checks else "none"),
                "checks": checks,
                "requirements": [
                    rq
                    | {
                        "results": [
                            c for c in checks if rq.get("check") and c["id"].startswith(rq["check"])
                        ]
                    }
                    for rq in requirements_of(session)
                ]
                if gid == "tools"
                else [],
                "fails": fails,
            }
        )
    soft_checks = [c for gid in SOFT_GROUPS for c in (by_id.get(gid) or {}).get("checks", [])]
    soft_fails = [c for c in soft_checks if not c["passed"] and c["severity"] == "soft"]
    steps.append(
        {
            "num": "4",
            "title": "Was the call good to be on?",
            "rule": "Hearing, reply latency, dead air, interruptions, repeats and speech quality. These flag a call but never fail it.",
            "state": "warn" if soft_fails else ("pass" if soft_checks else "none"),
            "checks": soft_checks,
            "requirements": [],
            "fails": soft_fails,
        }
    )
    valid = scores.get("valid", True)
    passed = scores.get("passed", False)
    if not valid:
        verdict, why = "invalid", "step 1 failed, so this call is excluded from the agent's results"
    elif passed:
        verdict = "pass"
        why = "every hard check in steps 1–3 passed" + (
            f"; flagged on {len(soft_fails)} soft check(s) in step 4" if soft_fails else ""
        )
    else:
        first = next((st for st in steps[1:3] if st["state"] == "fail"), None)
        why = (
            f"step {first['num']} failed: {first['fails'][0]['what_happened']}"
            if first
            else (scores.get("failure_reason") or "a hard check failed")
        )
        verdict = "fail"
    steps.append(
        {
            "num": "5",
            "title": "Verdict",
            "rule": "A call passes when every hard check in steps 1–3 passes. Soft checks (step 4) flag it. A failed step 1 makes it invalid.",
            "state": {"pass": "pass", "fail": "fail", "invalid": "invalid"}[verdict],
            "checks": [],
            "requirements": [],
            "fails": [],
            "verdict": verdict,
            "why": why,
        }
    )
    return {"steps": steps, "verdict": verdict, "why": why}


def _neighbours(run_id: str | None, session_id: str, attempt: int) -> dict[str, Any]:
    """Previous and next attempt of the same session in this run (T6.10)."""
    if not run_id:
        return {"prev": None, "next": None, "n": None, "of": None}
    run_dir = settings().runs_dir / run_id
    ns = (
        sorted(
            int(p.name) for p in (run_dir / session_id).iterdir() if p.is_dir() and p.name.isdigit()
        )
        if (run_dir / session_id).exists()
        else []
    )
    ns = [n for n in ns if (run_dir / session_id / str(n) / "meta.json").exists()]
    i = ns.index(attempt) if attempt in ns else -1
    return {
        "prev": ns[i - 1] if i > 0 else None,
        "next": ns[i + 1] if 0 <= i < len(ns) - 1 else None,
        "n": i + 1 if i >= 0 else None,
        "of": len(ns),
    }


def _agent_latency(record: CallRecord) -> float | None:
    vals = [
        t.metrics.get("e2e_latency") for t in record.agent_turns if t.metrics.get("e2e_latency")
    ]
    return round(sum(vals) / len(vals), 2) if vals else None


def _agent_turn_metrics(record: CallRecord) -> list[dict[str, Any]]:
    rows = []
    for t in record.agent_turns:
        m = t.metrics or {}
        rows.append(
            {
                "n": t.n,
                "text": t.text[:80],
                "e2e_s": m.get("e2e_latency"),
                "eou_s": m.get("end_of_turn_delay"),
                "transcription_s": m.get("transcription_delay"),
                "llm_ttft_s": m.get("llm_node_ttft") or m.get("ttft"),
                "tts_ttfb_s": m.get("tts_node_ttfb"),
            }
        )
    return rows


def _merge_turns(record: CallRecord, timeline: dict[str, Any]) -> list[dict[str, Any]]:
    """One chronological list for the transcript: caller turns (said + heard-as), agent
    turns (with the latency the caller actually experienced), tool calls."""
    t0 = record.t0_ms
    items: list[dict[str, Any]] = []
    responses = (timeline.get("ux") or {}).get("responses", [])
    agent_speech = [e for e in timeline.get("events", []) if e["kind"] == "agent_speech"]
    heard = list(record.agent_heard)

    for ct in record.caller_turns:
        # what the agent's STT produced while this turn was being spoken (plus a tail)
        parts = [h["text"] for h in heard if ct.t_start_ms - 500 <= h["t_ms"] <= ct.t_end_ms + 2500]
        heard_text = " ".join(parts).strip()
        items.append(
            {
                "kind": "caller",
                "t_ms": ct.t_start_ms,
                "end_ms": ct.t_end_ms,
                "n": ct.n,
                "text": ct.text,
                "interruption": ct.interruption,
                "heard": heard_text,
                "heard_differs": _differs(ct.text, heard_text) if heard_text else None,
                "heard_marks": _heard_marks(ct.text, heard_text) if heard_text else [],
            }
        )
    for at in record.agent_turns:
        m = at.metrics or {}
        start = int(m["started_speaking_at"] * 1000) - t0 if m.get("started_speaking_at") else None
        end = int(m["stopped_speaking_at"] * 1000) - t0 if m.get("stopped_speaking_at") else None
        if start is None:
            seg = max(
                (s for s in agent_speech if s["t_ms"] <= at.t_ms),
                key=lambda s: s["t_ms"],
                default=None,
            )
            start = seg["t_ms"] if seg else at.t_ms
        # heard latency: the response whose agent_start is closest to this turn's audio start
        lat = None
        if start is not None:
            cands = [
                r
                for r in responses
                if r.get("agent_start_ms") is not None and abs(r["agent_start_ms"] - start) <= 1500
            ]
            if cands:
                lat = min(cands, key=lambda r: abs(r["agent_start_ms"] - start))["latency_ms"]
        items.append(
            {
                "kind": "agent",
                "t_ms": start,
                "end_ms": end,
                "n": at.n,
                "text": at.text,
                "interrupted": at.interrupted,
                "latency_ms": lat,
                "latency_class": _lat_class(lat) if lat is not None else None,
                "e2e_s": m.get("e2e_latency"),
            }
        )
    for c in record.tool_calls:
        items.append(
            {
                "kind": "tool",
                "t_ms": c.t_ms,
                "end_ms": c.t_ms + c.duration_ms,
                "id": c.id,
                "tool": c.tool,
                "args": c.args,
                "status": c.status,
                "ok": c.ok,
                "duration_ms": c.duration_ms,
                "response": c.response,
                "response_short": _response_short(c.response, c.ok),
                "arg_problems": c.arg_problems,
                "fault": c.fault,
            }
        )
    for d in (timeline.get("ux") or {}).get("dead_air", []):
        items.append(
            {
                "kind": "dead_air",
                "t_ms": d["start_ms"],
                "end_ms": d["end_ms"],
                "gap_ms": d["gap_ms"],
            }
        )
    items.sort(
        key=lambda e: (e["t_ms"] if e["t_ms"] is not None else 0, 0 if e["kind"] == "tool" else 1)
    )
    return items


def _response_short(resp: dict[str, Any], ok: bool) -> str:
    if not isinstance(resp, dict):
        return str(resp)[:120]
    if not ok:
        return str(resp.get("error") or resp.get("detail") or resp)[:160]
    keys = [
        k
        for k in (
            "status",
            "total",
            "items",
            "amount",
            "refund_id",
            "ticket_id",
            "shipping_address",
            "message",
        )
        if k in resp
    ]
    if keys:
        return ", ".join(f"{k}={resp[k]}" for k in keys)[:160]
    return json.dumps(resp)[:160]


def annotate_turns(
    turns: list[dict[str, Any]], checks: list[dict[str, Any]], th: dict[str, Any]
) -> None:
    """Attach to every transcript event the checks that judged it (T6.22): `tests` =
    [{tag, state, label, text, check_id}], built from the checks' own evidence."""

    def state_of(c: dict[str, Any]) -> str:
        return "pass" if c["passed"] else ("fail" if c["severity"] == "hard" else "warn")

    def note(c: dict[str, Any], text: str) -> dict[str, Any]:
        return {
            "tag": _tag(c["id"]),
            "state": state_of(c),
            "label": c["label"],
            "text": text,
            "check_id": c["id"],
            "why": c.get("why_it_matters", ""),
        }

    by_tool: dict[Any, list[dict[str, Any]]] = {}
    by_agent_turn: dict[int, list[dict[str, Any]]] = {}
    by_caller_turn: dict[int, list[dict[str, Any]]] = {}
    at_time: list[tuple[int, dict[str, Any]]] = []  # (t_ms, note) for events without ids
    for c in checks:
        ev = c.get("evidence") or {}
        cid = c["id"]
        if cid.startswith(("tools.", "speech.misheard_to_tool", "ux.time_to_resolution")):
            for tid in ev.get("tool_ids") or []:
                by_tool.setdefault(tid, []).append(note(c, c["what_happened"]))
        if cid.startswith("claims."):
            for cl in ev.get("claims") or []:
                txt = (
                    "backed by a write: " if cl.get("backed") else "NOT backed by any write: "
                ) + str(cl.get("quote") or cl.get("text") or "")[:80]
                n = {**note(c, txt), "state": "pass" if cl.get("backed") else "fail"}
                if cl.get("turn") is not None:
                    by_agent_turn.setdefault(int(cl["turn"]), []).append(n)
                elif cl.get("t_ms") is not None:
                    at_time.append((int(cl["t_ms"]), n))
        if cid.startswith("quality.") or cid == "ux.repeats":
            for n_turn in ev.get("turn_ns") or []:
                target = by_caller_turn if cid == "ux.repeats" else by_agent_turn
                target.setdefault(int(n_turn), []).append(note(c, c["what_happened"]))
        if cid == "speech.caller_hearing":
            for m in ev.get("mishearings") or []:
                txt = f"heard '{m.get('heard')}' for '{m.get('said')}'" + (
                    ", and acted on it" if m.get("acted_on") else ", not acted on"
                )
                st = "fail" if m.get("acted_on") else "warn"
                at_time.append(
                    (int(m.get("t_ms") or 0), {**note(c, txt), "state": st, "kind": "heard"})
                )
        if cid == "ux.dead_air":
            for g in ev.get("gaps") or []:
                at_time.append(
                    (
                        int(g.get("start_ms") or 0),
                        {
                            **note(
                                c,
                                f"{g.get('gap_ms')} ms of silence from both sides; flagged over {th.get('dead_air_s', 3)} s",
                            ),
                            "kind": "dead_air",
                        },
                    )
                )
        if cid == "ux.talk_over":
            for e in ev.get("events") or []:
                at_time.append(
                    (
                        int(e.get("agent_start_ms") or 0),
                        {
                            **note(c, "agent started talking while the caller was speaking"),
                            "kind": "talk_over",
                        },
                    )
                )
    lat_check = next((c for c in checks if c["id"] == "ux.latency_p95"), None)
    warn_ms = float(th.get("latency_p95_warn_s", 2.0)) * 1000
    fail_ms = float(th.get("latency_p95_fail_s", 3.5)) * 1000
    for it in turns:
        tests: list[dict[str, Any]] = []
        if it["kind"] == "tool":
            tests += by_tool.get(it["id"], [])
            if it.get("fault"):
                tests.append(
                    {
                        "tag": "injected_fault",
                        "state": "info",
                        "label": "Injected fault",
                        "text": f"this failure was injected by the session ({it['fault']})",
                        "check_id": "",
                        "why": "",
                    }
                )
        elif it["kind"] == "agent":
            tests += by_agent_turn.get(it["n"], [])
            if it.get("latency_ms") is not None and lat_check:
                st = (
                    "pass"
                    if it["latency_ms"] <= warn_ms
                    else ("warn" if it["latency_ms"] <= fail_ms else "fail")
                )
                tests.append(
                    {
                        **note(
                            lat_check,
                            f"caller stopped → agent audio {it['latency_ms']} ms; flagged over {warn_ms / 1000:g} s",
                        ),
                        "state": st,
                    }
                )
            tests += [
                n
                for t, n in at_time
                if n.get("kind") == "talk_over" and abs(t - (it["t_ms"] or 0)) <= 500
            ]
        elif it["kind"] == "caller":
            tests += by_caller_turn.get(it["n"], [])
            tests += [
                n
                for t, n in at_time
                if n.get("kind") == "heard" and it["t_ms"] - 15000 <= t <= it["t_ms"] + 500
            ]
        elif it["kind"] == "dead_air":
            tests += [
                n for t, n in at_time if n.get("kind") == "dead_air" and abs(t - it["t_ms"]) <= 300
            ]
        # claims noted only by time
        if it["kind"] == "agent":
            tests += [
                n
                for t, n in at_time
                if "kind" not in n
                and it["t_ms"] is not None
                and it["t_ms"] - 500 <= t <= (it.get("end_ms") or it["t_ms"] + 8000)
            ]
        it["tests"] = tests


def _heard_marks(said: str, heard: str) -> list[dict[str, Any]]:
    """The heard text as its original words, each marked when it differs from what was said
    (substituted or inserted), so the transcript can highlight exactly what was misheard."""
    import re
    from difflib import SequenceMatcher

    def key(w: str) -> str:
        return re.sub(r"[^a-z0-9]", "", w.lower())

    a_words, b_words = said.split(), heard.split()
    a, b = [key(w) for w in a_words], [key(w) for w in b_words]
    bad = set()
    for tag, _i1, _i2, j1, j2 in SequenceMatcher(a=a, b=b).get_opcodes():
        if tag in ("replace", "insert"):
            bad.update(range(j1, j2))
    return [{"w": w, "bad": j in bad} for j, w in enumerate(b_words)]


def _differs(said: str, heard: str) -> bool:
    from gf.scoring.speech import normalize

    a, b = normalize(said).split(), normalize(heard).split()
    if not a or not b:
        return False
    import difflib

    return difflib.SequenceMatcher(a=a, b=b).ratio() < 0.9


def envelopes(audio_path: str, step_ms: int = 50) -> dict[str, Any]:
    """Per-channel loudness in 0–100 per step, for the canvas lanes (no audio lib in the page)."""
    data, sr = sf.read(audio_path, dtype="int16")
    if data.ndim == 1:
        data = np.stack([data, np.zeros_like(data)], axis=1)
    n = int(sr * step_ms / 1000)
    out = {}
    for ch, name in ((0, "caller"), (1, "agent")):
        x = data[:, ch].astype(np.float32) / 32768.0
        m = len(x) // n
        if m == 0:
            out[name] = []
            continue
        frames = x[: m * n].reshape(m, n)
        rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
        db = 20 * np.log10(rms + 1e-9)
        lvl = np.clip((db + 60) / 60, 0, 1) * 100  # -60 dB → 0, 0 dB → 100
        out[name] = [int(v) for v in lvl]
    out["step_ms"] = step_ms
    out["duration_ms"] = int(len(data) / sr * 1000)
    return out
