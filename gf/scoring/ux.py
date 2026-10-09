"""User-experience checks from the audio timeline, plus transcript quality gates and validity."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from gf.config import Thresholds
from gf.record.model import CallRecord
from gf.scoring.checks import Check
from gf.scoring.speech import NUMBER_WORDS
from gf.sessions.schema import Session

REPROMPT = "are you still there"
STUTTER = re.compile(r"\b(\w+(?:\s+\w+){0,3})\s+\1\b", re.I)
FILLERS = re.compile(
    r"^(one moment|just a moment|let me check|bear with me|hold on|please hold)[.!, ]*$", re.I
)


def stutters_in(text: str) -> list[str]:
    """Repeated short phrases, ignoring repeated digits, number words and single letters:
    "GW 4 8 3 7 7" and "nine four one one zero" are read-backs, not stutters."""
    out = []
    for m in STUTTER.finditer(text):
        toks = m.group(1).lower().split()
        if all(t.isdigit() or t in NUMBER_WORDS or len(t) == 1 for t in toks):
            continue
        out.append(m.group(0))
    return out


def attribute_gaps(timeline: dict[str, Any]) -> list[dict[str, Any]]:
    """Each dead-air gap with `after`: who spoke last before it ("caller" → the agent went
    quiet; "agent" → the simulated caller did). Uses the timeline's speech events, so old
    timelines get it too."""
    ux = timeline.get("ux") or {}
    gaps = [dict(g) for g in ux.get("dead_air") or []]
    ends = sorted(
        (int(e["end_ms"]), "caller" if e["kind"] == "caller_speech" else "agent")
        for e in timeline.get("events") or []
        if e.get("kind") in ("caller_speech", "agent_speech") and e.get("end_ms") is not None
    )
    for g in gaps:
        if g.get("after"):
            continue
        last = None
        for end, who in ends:
            if end <= g["start_ms"] + 1:
                last = who
        g["after"] = last
    return gaps


def check_ux(
    record: CallRecord, session: Session, timeline: dict[str, Any], th: Thresholds
) -> list[Check]:
    ux = timeline.get("ux") or {}
    checks: list[Check] = []
    lat = ux.get("latency_ms") or {}
    p95 = lat.get("p95")
    checks.append(
        Check(
            id="ux.latency_p95",
            group="ux",
            label=(
                f"Response latency p95 (caller stops → agent audio) under "
                f"{th.latency_p95_warn_s:.1f} s (fails the call over {th.latency_p95_fail_s:.1f} s)"
            ),
            passed=p95 is None or p95 <= th.latency_p95_warn_s * 1000,
            severity="hard" if p95 is not None and p95 > th.latency_p95_fail_s * 1000 else "soft",
            what_happened=f"p50 {lat.get('p50')} ms, p95 {p95} ms over {lat.get('n', 0)} turns"
            if p95 is not None
            else "no answered turns",
            why_it_matters="Over about two seconds callers start talking over the agent or hang up.",
            evidence={"responses": ux.get("responses", []), "t_ms": _slowest(ux)},
            value=p95,
        )
    )
    gaps = attribute_gaps(timeline)
    dead = [g for g in gaps if g.get("after") != "agent"]  # silence after the caller spoke
    caller_gaps = [g for g in gaps if g.get("after") == "agent"]  # the caller's own pause
    longest = max((d["gap_ms"] for d in dead), default=0)
    checks.append(
        Check(
            id="ux.dead_air",
            group="ux",
            label=(
                f"No silence after the caller spoke longer than {th.dead_air_gap_s:.0f} s "
                f"(fails the call at {th.dead_air_gap_fail_s:.0f} s)"
            ),
            passed=not dead,
            severity="hard" if longest >= th.dead_air_gap_fail_s * 1000 else "soft",
            what_happened=f"{len(dead)} gap(s) after the caller spoke, longest {longest} ms"
            if dead
            else "none",
            why_it_matters="Dead air is when a real caller says 'hello?' or hangs up.",
            evidence={"t_ms": dead[0]["start_ms"] if dead else None, "gaps": dead},
            value=sum(d["gap_ms"] for d in dead),
        )
    )
    checks.append(
        Check(
            id="caller.went_quiet",
            group="caller",
            label=f"The simulated caller answered within {th.dead_air_gap_s:.0f} s of the agent",
            passed=not caller_gaps,
            severity="soft",
            what_happened=(
                f"{len(caller_gaps)} pause(s) after the agent spoke, longest "
                f"{max(g['gap_ms'] for g in caller_gaps)} ms"
            )
            if caller_gaps
            else "no long pauses",
            why_it_matters=(
                "A pause after the agent's question is the simulator's, not the agent's, so it "
                "is kept out of the agent's dead-air score."
            ),
            evidence={
                "t_ms": caller_gaps[0]["start_ms"] if caller_gaps else None,
                "gaps": caller_gaps,
            },
            value=sum(g["gap_ms"] for g in caller_gaps),
        )
    )
    talk = ux.get("talk_over") or []
    checks.append(
        Check(
            id="ux.talk_over",
            group="ux",
            label=f"Agent did not start talking over the caller more than {th.talk_over_max} times",
            passed=len(talk) <= th.talk_over_max,
            severity="soft",
            what_happened=f"{len(talk)} time(s)",
            why_it_matters="Talking over the caller is the fastest way to sound like a machine.",
            evidence={"t_ms": talk[0]["agent_start_ms"] if talk else None, "events": talk},
            value=len(talk),
        )
    )
    barge = ux.get("barge_in") or []
    slow_stop = [b for b in barge if b["agent_stop_after_ms"] > th.barge_in_stop_s * 1000]
    checks.append(
        Check(
            id="ux.barge_in",
            group="ux",
            label=f"When interrupted, the agent stopped within {th.barge_in_stop_s:.1f} s",
            passed=not slow_stop,
            severity="soft",
            what_happened=f"{len(barge)} interruption(s), {len(slow_stop)} slow stop(s)"
            if barge
            else "the caller never interrupted",
            why_it_matters="An agent that keeps talking after being interrupted feels like it is not listening.",
            evidence={
                "t_ms": slow_stop[0]["caller_start_ms"] if slow_stop else None,
                "events": barge,
            },
            value=max((b["agent_stop_after_ms"] for b in barge), default=None),
        )
    )
    reprompts = sum(1 for t in record.caller_turns if REPROMPT in t.text.lower())
    repeats = (
        int(record.caller_result.get("repeats", 0)) + reprompts + _near_duplicate_turns(record)
    )
    checks.append(
        Check(
            id="ux.repeats",
            group="ux",
            label=f"Caller had to repeat themselves fewer than {th.repeats_max} times",
            passed=repeats < th.repeats_max,
            severity="soft",
            what_happened=f"{repeats} repeat(s) incl. {reprompts} 'are you still there?'",
            why_it_matters="Every repeat is a moment the agent did not understand or did not respond.",
            evidence={"turn_ns": [t.n for t in record.caller_turns if REPROMPT in t.text.lower()]},
            value=repeats,
        )
    )
    first_write = next(
        (
            c
            for c in record.tool_calls
            if c.ok and c.tool in ("issue_refund", "update_shipping_address", "escalate_to_human")
        ),
        None,
    )
    checks.append(
        Check(
            id="ux.time_to_resolution",
            group="ux",
            label="Time from call start to the write that resolved the request",
            passed=True,
            severity="info",
            what_happened=f"{first_write.t_ms / 1000:.1f} s ({first_write.tool})"
            if first_write
            else "no resolving write",
            evidence={
                "t_ms": first_write.t_ms if first_write else None,
                "tool_ids": [first_write.id] if first_write else [],
            },
            value=first_write.t_ms if first_write else None,
        )
    )
    patience_ms = session.caller.conditions.patience_s * 1000
    would_hang_up = sum(d["gap_ms"] for d in dead) > patience_ms or repeats >= 3
    checks.append(
        Check(
            id="ux.would_hang_up",
            group="ux",
            label="A real caller would have stayed on the line",
            passed=not would_hang_up,
            severity="soft",
            what_happened=(
                f"dead air after the caller {sum(d['gap_ms'] for d in dead)} ms vs patience {int(patience_ms)} ms; {repeats} repeats"
            ),
            why_it_matters="Rule of thumb: dead air beyond the caller's patience, or three repeats, loses the call.",
            evidence={},
            value=would_hang_up,
        )
    )
    checks.append(
        Check(
            id="ux.greeting_first",
            group="ux",
            label="Agent spoke first",
            passed=bool(ux.get("greeting_first")),
            severity="soft",
            what_happened=f"first agent audio at {ux.get('first_agent_audio_ms')} ms",
            why_it_matters="A caller who has to speak into silence assumes nobody answered.",
            evidence={"t_ms": ux.get("first_agent_audio_ms")},
        )
    )
    # agent-side breakdown (self-reported), information only
    e2e = [t.metrics.get("e2e_latency") for t in record.agent_turns if t.metrics.get("e2e_latency")]
    checks.append(
        Check(
            id="ux.agent_reported_latency",
            group="ux",
            label="Agent-reported end-to-end latency (for comparison with what the caller heard)",
            passed=True,
            severity="info",
            what_happened=f"mean {sum(e2e) / len(e2e):.2f} s over {len(e2e)} turns"
            if e2e
            else "not reported",
            value=round(sum(e2e) / len(e2e), 3) if e2e else None,
        )
    )
    return checks


def _slowest(ux: dict[str, Any]) -> int | None:
    rows = [r for r in (ux.get("responses") or []) if r.get("latency_ms") is not None]
    return max(rows, key=lambda r: r["latency_ms"])["caller_end_ms"] if rows else None


def _near_duplicate_turns(record: CallRecord) -> int:
    texts = [t.text.lower() for t in record.caller_turns if REPROMPT not in t.text.lower()]
    n = 0
    for a, b in zip(texts, texts[1:], strict=False):
        if SequenceMatcher(None, a, b).ratio() >= 0.85:
            n += 1
    return n


def check_quality(record: CallRecord, th: Thresholds) -> list[Check]:
    """Cheap deterministic transcript gates over the agent's own turns."""
    checks: list[Check] = []
    turns = record.agent_turns
    tools_called = {c.tool for c in record.tool_calls}
    stutters = [t.n for t in turns if stutters_in(t.text)]
    leaks = [
        t.n
        for t in turns
        if any(re.search(rf"\b{re.escape(name)}\b", t.text) for name in tools_called)
    ]
    last_n = turns[-1].n if turns else None
    truncated = [
        t.n
        for t in turns
        if len(t.text) >= 20
        and not re.search(r"[.!?]['\"]?\s*$", t.text.strip())
        and t.n != last_n
        and not t.interrupted
    ]
    fillers = [t.n for t in turns if FILLERS.match(t.text.strip())]
    consecutive_fillers = any(a + 1 == b for a, b in zip(fillers, fillers[1:], strict=False))
    questions = [(t.n, t.t_ms, t.text) for t in turns if "?" in t.text]
    repeated_q = []
    for (_n1, t1, q1), (n2, t2, q2) in zip(questions, questions[1:], strict=False):
        if (
            t2 - t1 <= th.repeated_question_window_s * 1000
            and SequenceMatcher(None, q1.lower(), q2.lower()).ratio()
            >= th.repeated_question_similarity
        ):
            repeated_q.append(n2)
    for cid, label, bad, why in (
        (
            "quality.stutter",
            "No stuttered phrases",
            stutters,
            "Repeated phrases sound like a glitch.",
        ),
        (
            "quality.tool_name_leak",
            "No tool names spoken aloud",
            leaks,
            "Internal names leaking into speech break the illusion.",
        ),
        (
            "quality.truncated",
            "No cut-off sentences",
            truncated,
            "A sentence without an ending usually means the reply was cut.",
        ),
        (
            "quality.repeated_question",
            "Did not re-ask the same question within 15 s",
            repeated_q,
            "Re-asking means the answer was not heard or not kept.",
        ),
    ):
        checks.append(
            Check(
                id=cid,
                group="quality",
                label=label,
                passed=not bad,
                severity="soft",
                what_happened=f"turn(s) {bad}" if bad else "clean",
                why_it_matters=why,
                evidence={
                    "turn_ns": bad,
                    "t_ms": next((t.t_ms for t in turns if t.n == bad[0]), None) if bad else None,
                },
                value=len(bad),
            )
        )
    checks.append(
        Check(
            id="quality.filler_only_turns",
            group="quality",
            label="No two filler-only turns in a row",
            passed=not consecutive_fillers,
            severity="soft",
            what_happened=f"filler turns {fillers}" if fillers else "none",
            evidence={"turn_ns": fillers},
            value=len(fillers),
        )
    )
    return checks


META_TALK = re.compile(
    r"\b(as an ai|language model|simulation|simulated|i am an ai|test scenario|my instructions)\b",
    re.I,
)


def _numeric_runs(phrase: str) -> list[str]:
    """Digits of each run of consecutive numeric tokens, runs kept apart by a space."""
    from gf.scoring.speech import WORD2DIGIT

    out, cur = [], []
    for tok in re.findall(r"[a-z]+|\d+", phrase):
        d = tok if tok.isdigit() else WORD2DIGIT.get(tok)
        if d is None:
            if cur:
                out.append("".join(cur))
                cur = []
            continue
        cur.append(d)
    if cur:
        out.append("".join(cur))
    return [r + " " for r in out]


def caller_in_character(record: CallRecord, session: Session) -> list[str]:
    """Deterministic character check: only the session's facts, no meta-talk."""
    from gf.scoring.speech import digits_of

    problems = []
    allowed = {digits_of(str(v)) for v in session.caller.facts.values() if digits_of(str(v))}
    for t in record.caller_turns:
        if META_TALK.search(t.text):
            problems.append(f"turn {t.n}: meta-talk ({META_TALK.search(t.text).group(0)!r})")
        # Digit groups are read per numeric phrase ("4 8 2 1 3"), never across unrelated
        # numbers ("$89.99 ... 48213" must not become "899948213").
        for phrase in re.split(r"[^\w\s-]+", t.text.lower()):
            for run in re.findall(r"\d{5}", "".join(_numeric_runs(phrase))):
                if not any(run in a for a in allowed):
                    problems.append(f"turn {t.n}: spoke a 5-digit number not in its facts ({run})")
    return problems


def check_validity(record: CallRecord, session: Session | None = None) -> list[Check]:
    """Invalid = the simulation broke, so the call must not count against the agent."""
    reasons = []
    external = record.meta.get("engine") == "livekit-simulate"
    if session is not None and not external:
        # LiveKit's simulator does not export the caller's own words (only what the agent
        # heard), and its judge covers character; the digit rule would misfire on STT errors.
        reasons += caller_in_character(record, session)
    if record.meta.get("runner_error"):
        reasons.append(f"runner error: {record.meta['runner_error']}")
    if not record.caller_turns:
        reasons.append("the simulated caller never spoke")
    if not record.agent_events:
        reasons.append("no agent record was written")
    if record.caller_result.get("broke_character"):
        reasons.append("the caller broke character")
    if record.ended_by == "mutual_silence" and not record.agent_turns:
        reasons.append("mutual silence before any agent speech")
    return [
        Check(
            id="validity.simulation",
            group="validity",
            label="The simulation itself ran correctly",
            passed=not reasons,
            severity="hard",
            what_happened="; ".join(reasons) or "valid",
            why_it_matters="A broken simulation says nothing about the agent, so it is excluded from rates.",
            evidence={"ended_by": record.ended_by, "end_reason": record.end_reason},
        )
    ]
