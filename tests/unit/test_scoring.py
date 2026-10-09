"""Scoring on real recorded calls (fixtures/records, audio stripped, timeline kept)."""

from pathlib import Path

from gf.config import ROOT
from gf.record.model import AgentTurn, CallRecord
from gf.scoring.claims import check_claims
from gf.scoring.score import score_attempt
from gf.scoring.stats import pass_summary, wilson
from gf.scoring.tools import _eval_assertion
from gf.sessions.schema import Session

REC = ROOT / "fixtures" / "records"
SESS = ROOT / "sessions"


def by_id(result: dict, cid: str) -> dict:
    return next(c for c in result["checks"] if c["id"] == cid)


def test_basic_refund_passes_the_task_and_fails_the_experience_bar(tmp_path: Path):
    """The recorded call did the right thing, so the task verdict passes (T5.15); its 4.2 s
    p95 reply latency fails the experience verdict, and nothing else does."""
    r = score_attempt(REC / "refund-basic", Session.load(SESS / "refund-basic.yaml"))
    assert r["valid"] and r["tool_ok"] and r["passed"] and r["hard_fails"] == []
    assert r["experience_ok"] is False and r["experience_fails"] == ["ux.latency_p95"]
    assert "p95" in r["experience_reason"] and r["failure_reason"] == ""
    assert by_id(r, "tools.required.lookup_order")["passed"]
    assert by_id(r, "tools.required.issue_refund")["passed"]
    assert by_id(r, "tools.order")["passed"] and by_id(r, "tools.wrong_write")["passed"]
    assert by_id(r, "state.refunds_gw_48213_amount_89_99")["passed"]
    assert by_id(r, "claims.claimed_without_acting")["passed"]
    assert r["wer"] is not None and r["wer"] < 0.5
    assert r["latency_p95_ms"] and r["latency_p95_ms"] > 0


def test_fault_escalated_path_is_honest_and_passes():
    r = score_attempt(
        REC / "refund-fault-escalated", Session.load(SESS / "retired" / "refund-backend-fault.yaml")
    )
    assert r["valid"] and r["passed"], r["hard_fails"]
    assert by_id(r, "claims.honest_about_failure")["passed"]
    assert by_id(r, "tools.failed_calls")["value"] == 1
    assert ("escalate_to_human", 200) in [tuple(t) for t in r["tool_calls"]]


def test_fault_retried_path_passes_every_task_check():
    r = score_attempt(
        REC / "refund-fault-retried", Session.load(SESS / "retired" / "refund-backend-fault.yaml")
    )
    assert r["valid"] and r["tool_ok"] and r["passed"]
    # T5.13/T5.15: the 5.2 s silence after the caller spoke fails the experience verdict only
    assert r["experience_fails"] == ["ux.dead_air"] and r["experience_ok"] is False
    assert [tuple(t) for t in r["tool_calls"]] == [
        ("lookup_order", 200),
        ("issue_refund", 500),
        ("issue_refund", 200),
    ]


def test_noisy_call_passes_but_flags_ux():
    r = score_attempt(
        REC / "refund-noisy", Session.load(SESS / "retired" / "refund-noisy-cafe.yaml")
    )
    assert r["passed"], r["hard_fails"]
    assert by_id(r, "speech.wer")["value"] is not None


def test_claim_without_write_fails_and_negation_does_not():
    base = CallRecord.load(REC / "refund-basic")
    liar = base.model_copy(
        update={"tool_calls": [c for c in base.tool_calls if c.tool != "issue_refund"]}
    )
    c = check_claims(liar)[0]
    assert c.id == "claims.claimed_without_acting" and not c.passed
    assert c.evidence["turn_ns"], "evidence must point at the turn"
    honest = base.model_copy(
        update={
            "tool_calls": [],
            "agent_turns": [
                AgentTurn(n=1, text="I'm sorry, the refund did not go through.", t_ms=1000)
            ],
        }
    )
    assert check_claims(honest)[0].passed


def test_caller_in_character_detects_meta_talk_and_foreign_numbers():
    from gf.record.model import CallerTurn
    from gf.scoring.ux import caller_in_character

    session = Session.load(SESS / "refund-basic.yaml")
    base = CallRecord.load(REC / "refund-basic")
    assert caller_in_character(base, session) == []
    broken = base.model_copy(
        update={
            "caller_turns": [
                CallerTurn(n=1, text="As an AI I cannot do that.", t_start_ms=0, t_end_ms=1),
                CallerTurn(n=2, text="My order is G W 9 9 9 9 9.", t_start_ms=2, t_end_ms=3),
            ]
        }
    )
    problems = caller_in_character(broken, session)
    assert len(problems) == 2 and "meta-talk" in problems[0] and "99999" in problems[1]


def test_final_state_evaluator():
    state = {"refunds": {"GW-48213": {"amount": 89.99}}, "tickets": [], "ended": False}
    assert _eval_assertion("refunds[GW-48213].amount == 89.99", state) == (True, 89.99)
    assert _eval_assertion("refunds[GW-48213].amount == 10", state)[0] is False
    assert _eval_assertion("refunds[GW-99999] exists", state)[0] is False
    assert _eval_assertion("ended == false", state)[0] is True


def test_wilson_and_flaky():
    assert wilson(3, 3) == (1.0, 0.4385, 1.0)
    assert wilson(0, 3)[2] == 0.5615
    s = pass_summary([True, False, True])
    assert s["flaky"] and not s["pass_all"] and s["pass_any"] and s["rate"] == 0.6667
    assert pass_summary([])["n"] == 0


def test_required_call_message_names_an_injected_fault():
    from pathlib import Path

    from gf.scoring.tools import _injected_fault
    from gf.sessions.schema import Session

    root = Path(__file__).resolve().parents[2]
    sess = Session.load(root / "sessions" / "refund-backend-error-retry.yaml")
    assert _injected_fault(sess, "issue_refund", 1) == "error_500 on call #1 of issue_refund"
    assert _injected_fault(sess, "lookup_order", 1) is None


def test_caller_score_rules():
    from gf.record.model import CallRecord
    from gf.scoring.caller import check_caller
    from gf.sessions.schema import Session

    root = Path(__file__).resolve().parents[2]
    rec = CallRecord.load(root / "fixtures" / "records" / "refund-basic")
    sess = Session.load(root / "sessions" / "refund-basic.yaml")
    by = {c.id: c for c in check_caller(rec, sess)}
    assert (
        by["caller.goal_stated_early"].passed and by["caller.goal_stated_early"].severity == "soft"
    )
    assert by["caller.ended_legitimately"].passed
    rec2 = rec.model_copy(update={"ended_by": "max_turns", "end_reason": "turn limit reached"})
    assert not {c.id: c for c in check_caller(rec2, sess)}["caller.ended_legitimately"].passed
    quiet = [t.model_copy(update={"text": "hello there"}) for t in rec.caller_turns[:2]] + list(
        rec.caller_turns[2:]
    )
    rec3 = rec.model_copy(update={"caller_turns": quiet})
    assert not {c.id: c for c in check_caller(rec3, sess)}["caller.goal_stated_early"].passed


def test_caller_flags_are_kept_apart_from_agent_flags(tmp_path):
    import shutil

    from gf.scoring.score import score_attempt
    from gf.sessions.schema import Session

    root = Path(__file__).resolve().parents[2]
    folder = tmp_path / "rec"
    shutil.copytree(root / "fixtures" / "records" / "refund-basic", folder)
    scores = score_attempt(folder, Session.load(root / "sessions" / "refund-basic.yaml"))
    assert "caller_ok" in scores and isinstance(scores["caller_flags"], list)
    assert not any(f.startswith(("caller.", "validity.")) for f in scores["soft_flags"])
