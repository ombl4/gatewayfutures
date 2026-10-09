"""Spec T5.13: number-aware hearing, stutter gate, dead-air attribution, fail bars, scripted
faults vs rejections, avoidable rejections, restrictive honesty."""

from __future__ import annotations

import json
from pathlib import Path

from gf.config import ROOT, Thresholds
from gf.record.model import AgentTurn, CallRecord, ToolCall
from gf.report.model import _heard_marks, annotate_turns
from gf.scoring.claims import check_claims
from gf.scoring.speech import canon_tokens, digits_of, normalize
from gf.scoring.tools import check_tools
from gf.scoring.ux import attribute_gaps, check_quality, check_ux, stutters_in
from gf.sessions.schema import Session

REC = ROOT / "fixtures" / "records"
SESS = ROOT / "sessions"


def by_id(checks, cid):
    return next(c for c in checks if c.id == cid)


def test_numbers_canonicalise_however_they_were_said():
    assert normalize("GW-48213") == "g w 4 8 2 1 3"
    assert normalize("G W 4 8 2 1 3") == "g w 4 8 2 1 3"
    assert normalize("GW four eight two one three") == "g w 4 8 2 1 3"
    assert normalize("gw four eight two one three") == "g w 4 8 2 1 3"
    assert digits_of("eighty nine ninety nine") == "8999"
    assert digits_of("$89.99") == "8999"
    assert digits_of("eighty-nine dollars and ninety-nine cents") == "8999"
    assert digits_of("five hundred dollars") == "500"
    assert digits_of("five hundred") == digits_of("$500")
    assert normalize("one hundred and twenty") == "1 2 0"
    assert normalize("two thousand twenty") == "2 0 2 0"
    assert normalize("nine four one one zero") == "9 4 1 1 0" == normalize("94110")
    assert normalize("zip code 94110, please") == "zip code 9 4 1 1 0 please"
    # source spans: the digits of a spoken number point back at the words that made it
    toks = canon_tokens("total eighty nine ninety nine today")
    assert [t for t, _, _ in toks] == ["total", "8", "9", "9", "9", "today"]
    assert toks[1][1:] == (1, 2) and toks[3][1:] == (3, 4)


def test_heard_highlight_does_not_mark_number_or_acronym_spellings():
    marks = _heard_marks(
        "my order G W 4 8 2 1 3 arrived broken",
        "my order GW four eight two one three arrived broken",
    )
    assert not any(m["bad"] for m in marks)
    marks = _heard_marks("the total was $89.99", "the total was eighty nine ninety nine")
    assert not any(m["bad"] for m in marks)
    marks = _heard_marks("12 Beacon Street in Boston", "12 Beacon Street in Austin")
    assert [m["w"] for m in marks if m["bad"]] == ["Austin"]


def test_stutter_gate_ignores_read_backs_but_catches_real_stutters():
    assert stutters_in("Your order GW 4 8 3 7 7 was delivered.") == []
    assert stutters_in("delivered to zip code nine four one one zero, total eighty nine") == []
    assert stutters_in("I I want to to help you") == ["I I"] or stutters_in(
        "I I want to to help you"
    )
    assert stutters_in("let me check let me check that for you")
    rec = CallRecord.load(REC / "refund-basic")
    rec = rec.model_copy(
        update={
            "agent_turns": [
                AgentTurn(n=1, text="Your order GW 4 8 3 7 7 was delivered.", t_ms=1000),
                AgentTurn(n=2, text="Let me check let me check that for you.", t_ms=5000),
            ]
        }
    )
    st = by_id(check_quality(rec, Thresholds()), "quality.stutter")
    assert not st.passed and st.evidence["turn_ns"] == [2]


def _timeline(events, dead, p95=1500):
    return {
        "events": events,
        "ux": {
            "dead_air": dead,
            "dead_air_total_ms": sum(d["gap_ms"] for d in dead),
            "latency_ms": {"p50": 1000, "p95": p95, "n": 3},
            "responses": [],
        },
    }


def test_dead_air_is_attributed_to_the_side_that_went_quiet():
    events = [
        {"kind": "agent_speech", "t_ms": 0, "end_ms": 2000},
        {"kind": "caller_speech", "t_ms": 9000, "end_ms": 10000},
        {"kind": "agent_speech", "t_ms": 16000, "end_ms": 17000},
    ]
    dead = [
        {"start_ms": 2000, "end_ms": 9000, "gap_ms": 7000},  # after the agent: caller's pause
        {"start_ms": 10000, "end_ms": 16000, "gap_ms": 6000},  # after the caller: agent's
    ]
    gaps = attribute_gaps(_timeline(events, dead))
    assert [g["after"] for g in gaps] == ["agent", "caller"]
    rec = CallRecord.load(REC / "refund-basic")
    sess = Session.load(SESS / "refund-basic.yaml")
    checks = check_ux(rec, sess, _timeline(events, dead), Thresholds())
    da = by_id(checks, "ux.dead_air")
    assert not da.passed and da.severity == "hard" and len(da.evidence["gaps"]) == 1
    assert da.evidence["gaps"][0]["gap_ms"] == 6000 and da.value == 6000
    cq = by_id(checks, "caller.went_quiet")
    assert not cq.passed and cq.severity == "soft" and cq.evidence["gaps"][0]["gap_ms"] == 7000
    # a 4 s agent-side gap flags but does not fail
    dead2 = [{"start_ms": 10000, "end_ms": 14000, "gap_ms": 4000}]
    da2 = by_id(check_ux(rec, sess, _timeline(events, dead2), Thresholds()), "ux.dead_air")
    assert not da2.passed and da2.severity == "soft"
    # a timeline that already carries `after` keeps it
    assert (
        attribute_gaps(
            _timeline([], [{"start_ms": 1, "end_ms": 5000, "gap_ms": 4999, "after": "caller"}])
        )[0]["after"]
        == "caller"
    )


def test_latency_fail_bar_is_a_hard_failure_and_warn_bar_a_flag():
    rec = CallRecord.load(REC / "refund-basic")
    sess = Session.load(SESS / "refund-basic.yaml")
    th = Thresholds()
    for p95, passed, sev in ((1500, True, "soft"), (2500, False, "soft"), (4000, False, "hard")):
        c = by_id(check_ux(rec, sess, _timeline([], [], p95=p95), th), "ux.latency_p95")
        assert (c.passed, c.severity) == (passed, sev), p95


def _with_calls(rec: CallRecord, calls: list[ToolCall]) -> CallRecord:
    return rec.model_copy(update={"tool_calls": calls})


def test_avoidable_rejection_flags_a_write_the_lookup_ruled_out():
    rec = CallRecord.load(REC / "refund-basic")
    sess = Session.load(SESS / "refund-not-delivered-shipped.yaml")
    lookup = ToolCall(
        id=1,
        tool="lookup_order",
        args={"order_id": "GW-48377", "zip": "60614"},
        response={"order_id": "GW-48377", "status": "shipped", "total": 42.5},
        status=200,
        ok=True,
        t_ms=10000,
        duration_ms=50,
    )
    refund = ToolCall(
        id=2,
        tool="issue_refund",
        args={"order_id": "GW-48377", "amount": 42.5, "reason": "x"},
        response={"error": "not_delivered", "message": "Order is shipped; refunds need delivery"},
        status=409,
        ok=False,
        t_ms=20000,
        duration_ms=30,
    )
    c = by_id(check_tools(_with_calls(rec, [lookup, refund]), sess), "tools.avoidable_rejection")
    assert not c.passed and c.severity == "soft" and c.evidence["tool_ids"] == [2]
    assert "not delivered" in c.what_happened
    # a scripted rejection is not the agent's doing
    scripted = refund.model_copy(update={"fault": "reject"})
    assert by_id(
        check_tools(_with_calls(rec, [lookup, scripted]), sess), "tools.avoidable_rejection"
    ).passed
    # no prior lookup: the agent could not have known
    assert by_id(check_tools(_with_calls(rec, [refund]), sess), "tools.avoidable_rejection").passed


def test_honesty_accepts_restrictive_phrasing():
    rec = CallRecord.load(REC / "refund-fault-escalated")
    failed = next(c for c in rec.tool_calls if not c.ok)
    rec2 = rec.model_copy(
        update={
            "agent_turns": [
                AgentTurn(
                    n=1,
                    text="Refunds can only be issued after the order is delivered. Would you like me to connect you to a team member?",
                    t_ms=failed.t_ms + 1000,
                )
            ],
            "tool_calls": [c for c in rec.tool_calls if c.tool != "escalate_to_human"],
        }
    )
    assert by_id(check_claims(rec2), "claims.honest_about_failure").passed
    rec3 = rec2.model_copy(
        update={
            "agent_turns": [AgentTurn(n=1, text="Your refund is all set.", t_ms=failed.t_ms + 1000)]
        }
    )
    assert not by_id(check_claims(rec3), "claims.honest_about_failure").passed


def test_scripted_faults_and_rejections_are_labelled_on_the_transcript():
    turns = [
        {"kind": "tool", "id": 1, "tool": "issue_refund", "t_ms": 1000, "ok": False, "status": 500,
         "fault": "error_500", "arg_problems": [], "response": {"error": "injected"}},
        {"kind": "tool", "id": 2, "tool": "issue_refund", "t_ms": 3000, "ok": True, "status": 200,
         "fault": None, "arg_problems": [], "response": {}},
        {"kind": "tool", "id": 3, "tool": "issue_refund", "t_ms": 5000, "ok": False, "status": 409,
         "fault": None, "arg_problems": [], "response": {"error": "not_delivered"}},
    ]  # fmt: skip
    checks = [
        {"id": "tools.failed_calls", "label": "x", "passed": True, "severity": "info",
         "what_happened": "issue_refund → 500", "evidence": {"tool_ids": [1, 3]}},
    ]  # fmt: skip
    annotate_turns(turns, checks, {})
    t1 = turns[0]["tests"][0]
    assert t1["tag"] == "scripted_fault" and t1["state"] == "pass" and "retried" in t1["text"]
    assert all(t["tag"] != "failed_calls" for t in turns[0]["tests"] + turns[2]["tests"])
    t3 = turns[2]["tests"][0]
    assert t3["tag"] == "rejected" and t3["state"] == "info" and "not_delivered" in t3["text"]
    assert turns[1]["tests"] == []


def test_fixture_timelines_still_score(tmp_path: Path):
    # scores.json from fixtures carries the v3 method and the new check ids
    from gf.scoring.score import score_attempt

    r = score_attempt(REC / "refund-basic", Session.load(SESS / "refund-basic.yaml"))
    ids = {c["id"] for c in r["checks"]}
    assert {"tools.avoidable_rejection", "caller.went_quiet", "ux.dead_air"} <= ids
    assert r["method_version"].startswith("score-v3")
    json.dumps(r)
