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


def test_turn_latency_pairs_metrics_to_turns():
    import json

    from gf.record.latency import turn_latency
    from gf.record.model import CallRecord

    folder = ROOT / "fixtures" / "records" / "refund-basic"
    rec = CallRecord.load(folder)
    out = turn_latency(rec, json.loads((folder / "timeline.json").read_text()))
    turns = out["turns"]
    assert len(turns) == len(rec.agent_turns)
    assert turns[0]["greeting"] and turns[0]["heard_ms"] is None
    replies = [t for t in turns if not t["greeting"]]
    assert all(t["eou_ms"] and t["llm_ttft_ms"] and t["tts_ttfb_ms"] for t in replies)
    heard = [t for t in replies if t["heard_ms"] is not None]
    assert heard and all(t["unaccounted_ms"] >= 0 for t in heard)
    assert out["summary"]["heard_ms"]["n"] == len(heard)
    assert out["summary"]["llm_ttft_ms"]["p50"] > 0
    assert len(out["glossary"]) == len(out["stages"]) + 3


def test_time_breakdown_is_median_of_call_medians():
    s = {
        "attempts": [
            {"valid": True, "latency_breakdown": {"eou_ms": 500, "heard_ms": 2000}},
            {"valid": True, "latency_breakdown": {"eou_ms": 700, "heard_ms": 1000}},
            {"valid": False, "latency_breakdown": {"eou_ms": 9000, "heard_ms": 9000}},
        ]
    }
    rows = {r["key"]: r for r in model.time_breakdown(s)}
    assert rows["eou_ms"]["p50"] == 700 and rows["eou_ms"]["n"] == 2
    assert rows["heard_ms"]["p50"] == 2000 and rows["stt_ms"]["p50"] is None


def test_wer_falls_back_to_the_engine_measurement(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))  # no runs on disk, as on CI
    s = _summary("r", "2026-10-08T10:00:00+00:00", [_attempt("a", 1, wer=None)])
    s["attempts"][0]["livekit"] = {"wer": 0.12}
    m = model.run_metrics(s)
    assert m["wer"] == pytest.approx(0.12) and m["wer_source"] == "engine"
    card = {c["id"]: c for c in model.kpi_cards("r", s, None)}["wer"]
    assert "engine" in card["sub"] and card["value"] == "12%"
    s["attempts"][0]["wer"] = 0.05
    assert model.run_metrics(s)["wer_source"] == "recording"


def test_runs_are_ordered_by_start_time_not_name(tmp_path, monkeypatch):
    from gf.runner.batch import list_runs

    runs = tmp_path / "runs"
    monkeypatch.setenv("RUNS_DIR", str(runs))
    for rid, started in (
        ("zz-old", "2026-10-01T10:00:00+00:00"),
        ("aa-new", "2026-10-08T10:00:00+00:00"),
    ):
        (runs / rid).mkdir(parents=True)
        (runs / rid / "manifest.json").write_text(
            json.dumps({"run_id": rid, "started_at": started})
        )
    assert [p.name for p in list_runs()] == ["aa-new", "zz-old"]


def test_delta_is_against_the_previous_run_even_when_not_like_for_like(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    monkeypatch.setenv("RUNS_DIR", str(runs))
    for rid, day, atts, stamp in (
        ("r1", "01", [_attempt("a", 1, passed=False, tool_ok=False)], "s1"),
        ("r2", "02", [_attempt("a", 1)], "other"),
    ):
        d = runs / rid
        d.mkdir(parents=True)
        started = f"2026-10-{day}T10:00:00+00:00"
        (d / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": rid,
                    "started_at": started,
                    "calls": [{"session_id": "a", "attempt": 1, "record_dir": "x"}],
                    "sessions": [],
                    "repeat": 1,
                }
            )
        )
        (d / "summary.json").write_text(json.dumps(_summary(rid, started, atts, stamp)))
    import os

    os.utime(runs / "r2" / "summary.json", None)
    r = model.run_report("r2")
    assert r["previous"]["run_id"] == "r1" and r["previous"]["like_for_like"] is False
    card = {c["id"]: c for c in r["kpis"]}["rate"]
    assert card["delta"] == 1.0 and card["prev_id"] == "r1" and card["like_for_like"] is False


def test_environment_tag_is_stable_and_sensitive_to_the_right_things(tmp_path, monkeypatch):
    from gf import environment as E

    info = {"python": "3.12.15", "livekit-agents": "1.8.5", "openai": "2.54.0", "jiwer": "4.0.0"}
    c1 = E.components(info=info)
    c2 = E.components(info=info)
    assert E.env_tag(c1) == E.env_tag(c2) and E.env_tag(c1).startswith("env-")
    assert E.env_tag(E.components(info=info, agent_variant="dishonest")) != E.env_tag(c1)
    assert E.env_tag(E.components(info=info | {"livekit-agents": "1.9.0"})) != E.env_tag(c1)
    assert E.env_tag(E.components(info=info | {"jiwer": "9.9"})) == E.env_tag(c1)  # not tagged
    assert E.set_tag(["a", "b"]) == E.set_tag(["b", "a"]) and E.set_tag(["a"]) != E.set_tag(["b"])
    # stamp writes manifest fields, environment.json and the registry
    runs = tmp_path / "runs"
    monkeypatch.setenv("RUNS_DIR", str(runs))
    folder = runs / "r1"
    folder.mkdir(parents=True)
    man = {"started_at": "2026-10-08T10:00:00+00:00", "environment": info}
    rec = E.stamp("r1", folder, man, ["a", "b"])
    assert man["env_tag"] == rec["env_tag"] and man["set_tag"] == E.set_tag(["a", "b"])
    assert (folder / "environment.json").exists()
    reg = E.registry()
    assert len(reg) == 1 and reg[0]["runs"][0]["run_id"] == "r1"
    E.stamp("r2", folder, dict(man), ["a"])
    assert len(E.registry()) == 1 and len(E.registry()[0]["runs"]) == 2
    assert E.tags_of({"agent_config_hash": "x", "sessions": [{"id": "a"}]}) == {
        "env_tag": "env-?",
        "set_tag": E.set_tag(["a"]),
    }


def test_backfill_reads_library_versions_from_the_lockfile_history():
    from gf import environment as E

    hist = E.environment_from_history("2026-10-08T20:26:05+00:00")
    if hist is None:
        pytest.skip("no git history available (shallow checkout)")
    assert hist["livekit-agents"] and hist["git_commit"]
    assert "uv.lock" in hist["backfilled"]["from"]
    # same recorded libraries as a live manifest, so the tag can match a live run's
    assert set(E.RECORDED_LIBS) <= set(hist)
