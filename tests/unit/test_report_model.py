"""Spec T6.12 / T6.13: KPI cards, issue cards and call selection are computed from summaries
only, and the per-attempt scoring fields they rely on (tool_ok, issue_t_ms) exist."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gf.report import model

ROOT = Path(__file__).resolve().parents[2]


def _summary(run_id, started, attempts, stamp="s1"):
    valid = [a for a in attempts if a["valid"]]
    passed = sum(1 for a in valid if a["passed"])
    n = len(valid)
    return {
        "run_id": run_id,
        "started_at": started,
        "stamp": stamp,
        "overall": {
            "n": n,
            "passed": passed,
            "rate": passed / n if n else None,
            "ci_low": 0.1,
            "ci_high": 0.9,
        },
        "invalid": len(attempts) - n,
        "calls": len(attempts),
        "latency_p95_ms": {"median": 1500, "max": 2000},
        "sessions": [],
        "attempts": attempts,
        "flaky_sessions": [],
        "top_failures": [],
    }


def _attempt(sid, n, *, valid=True, passed=True, tool_ok=True, wer=0.05, t=12_000, hard=None):
    return {
        "session_id": sid,
        "attempt": n,
        "valid": valid,
        "passed": passed,
        "tool_ok": tool_ok,
        "wer": wer,
        "issue_t_ms": t,
        "failure_reason": "" if passed else "issue_refund was never called",
        "soft_flags": [],
        "hard_fails": hard or ([] if passed else ["tools.required.issue_refund"]),
        "duration_ms": 60_000,
        "latency_p95_ms": 1500,
    }


def test_run_metrics_from_summary_only():
    s = _summary(
        "r1",
        "2026-10-08T10:00:00+00:00",
        [
            _attempt("a", 1),
            _attempt("a", 2, passed=False, tool_ok=False, wer=0.15),
            _attempt("b", 1, valid=False),
        ],
    )
    m = model.run_metrics(s)
    assert m["rate"] == 0.5 and m["tool_rate"] == 0.5
    assert m["wer"] == pytest.approx(0.10) and m["p95_ms"] == 1500
    # summaries scored before tool_ok existed: measured as unknown, never as 100%
    for a in s["attempts"]:
        a.pop("tool_ok")
    assert model.run_metrics(s)["tool_rate"] is None


def test_kpi_cards_series_and_delta(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    monkeypatch.setenv("RUNS_DIR", str(runs))
    for rid, day, atts, stamp in (
        ("r1", "01", [_attempt("a", 1, passed=False, tool_ok=False)], "s1"),
        ("r2", "02", [_attempt("a", 1)], "s1"),
        ("r3", "03", [_attempt("a", 1)], "other"),
    ):
        d = runs / rid
        d.mkdir(parents=True)
        started = f"2026-10-{day}T10:00:00+00:00"
        (d / "manifest.json").write_text(json.dumps({"run_id": rid, "started_at": started}))
        (d / "summary.json").write_text(json.dumps(_summary(rid, started, atts, stamp)))
    s2 = json.loads((runs / "r2" / "summary.json").read_text())
    s1 = json.loads((runs / "r1" / "summary.json").read_text())
    cards = model.kpi_cards("r2", s2, s1)
    by = {c["id"]: c for c in cards}
    assert [c["id"] for c in cards] == ["rate", "tool_rate", "wer", "p95_ms"]
    assert by["rate"]["series"] == [0.0, 1.0]  # r3 is later, not in the series
    assert by["rate"]["delta"] == 1.0 and by["rate"]["delta_class"] == "fixed"
    assert by["rate"]["delta_text"] == "+100 pts"
    assert by["p95_ms"]["delta"] == 0 and by["p95_ms"]["delta_class"] == "same"
    # no comparable previous run: no delta, flagged as such
    s3 = json.loads((runs / "r3" / "summary.json").read_text())
    c3 = {c["id"]: c for c in model.kpi_cards("r3", s3, None)}
    assert c3["rate"]["delta"] is None and c3["rate"]["comparable"] is False
    assert c3["rate"]["series"] == [0.0, 1.0, 1.0]


def test_issues_are_prioritised_and_selected():
    r = {
        "run_id": "r",
        "summary": {"attempts": [_attempt("a", 1), _attempt("a", 2, passed=False)]},
        "failing": [
            _attempt("a", 2, passed=False) | {"title": "A"},
            _attempt("b", 1, passed=False, hard=["claims.claimed_without_acting"])
            | {"title": "B", "failure_reason": "claimed a refund that never happened"},
        ],
        "flagged": [_attempt("c", 1) | {"title": "C", "soft_flags": ["ux.dead_air"]}],
        "invalid": [
            _attempt("d", 1, valid=False)
            | {"title": "D", "failure_reason": "invalid: the caller broke character"}
        ],
        "reference": None,
    }
    iss = model.issues_for(r)
    assert iss["total"] == 4
    assert [i["severity"] for i in iss["items"]] == ["critical", "high", "medium", "simulation"]
    assert iss["items"][0]["title"].startswith("Claimed a refund")
    assert iss["items"][1]["title"].startswith("issue_refund")  # tool names keep their case
    assert iss["items"][2]["title"] == "Dead air"
    assert iss["items"][3]["title"] == "The caller broke character"
    assert all(i["t_ms"] == 12_000 for i in iss["items"])
    assert model.select_call(r) == ("b", 1)
    assert model.select_call(r, "a/2") == ("a", 2)
    assert model.select_call(r, "zzz/9") == ("b", 1)  # unknown call falls back to the top issue
    r["failing"] = r["flagged"] = r["invalid"] = []
    assert model.select_call(r) == ("a", 1)


def test_score_attempt_carries_tool_ok_and_issue_time(tmp_path):
    import shutil

    from gf.scoring.score import score_attempt
    from gf.sessions.schema import Session

    folder = tmp_path / "rec"
    shutil.copytree(ROOT / "fixtures" / "records" / "refund-basic", folder)
    scores = score_attempt(folder, Session.load(ROOT / "sessions" / "refund-basic.yaml"))
    assert "tool_ok" in scores and "issue_t_ms" in scores
    assert scores["tool_ok"] is True
    if scores["soft_flags"]:
        assert scores["issue_t_ms"] is None or scores["issue_t_ms"] >= 0


def test_heard_marks_highlight_only_the_misheard_words():
    marks = model._heard_marks("my order is GW 48213, zip 94110.", "my order is GW 48218 zip 94110")
    bad = [m["w"] for m in marks if m["bad"]]
    assert bad == ["48218"]
    assert all(not m["bad"] for m in model._heard_marks("yes please", "Yes, please."))
