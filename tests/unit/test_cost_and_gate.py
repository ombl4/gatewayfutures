"""Cost from a real record's usage events, the run budget, and the CI gate."""

import json
import shutil

from gf.config import ROOT
from gf.record.model import CallRecord
from gf.scoring.cost import call_cost
from gf.scoring.gate import gate_run

REC = ROOT / "fixtures" / "records"


def test_cost_comes_from_the_usage_events_and_is_priced():
    c = call_cost(CallRecord.load(REC / "refund-basic"))
    a = c["agent"]
    assert a["source"] == "agent_session_report.usage"
    assert a["llm_in"] == 14168 and a["llm_cached"] == 6400 and a["llm_out"] == 372
    assert a["tts_chars"] == 500 and 80 < a["stt_s"] < 81
    assert c["caller"]["llm_in"] > 0 and c["caller"]["tts_chars"] > 0
    assert 0 < c["usd"] < 0.10 and c["unpriced"] == []  # a one-minute call costs cents


def test_gate_fails_on_zero_tolerance_and_partial_runs(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    monkeypatch.setenv("RUNS_DIR", str(runs))
    run = runs / "r1"
    dst = run / "7c994c348001" / "1"
    shutil.copytree(REC / "refund-basic", dst)
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
                "started_at": "2026-10-08T10:00:00+00:00",
                "repeat": 1,
                "sessions": [
                    {
                        "id": "7c994c348001",
                        "title": "x",
                        "path": str(ROOT / "sessions/refund-basic.yaml"),
                    }
                ],
                "calls": [{"session_id": "7c994c348001", "attempt": 1, "record_dir": str(dst)}],
            }
        )
    )
    v = gate_run("r1")
    assert v["ok"] and v["n"] == 1 and v["cost_usd"] > 0
    # the same run marked partial by the budget, with a zero-tolerance fail injected
    summ = json.loads((run / "summary.json").read_text())
    summ["attempts"][0]["hard_fails"] = ["claims.claimed_without_acting"]
    (run / "summary.json").write_text(json.dumps(summ))
    man = json.loads((run / "manifest.json").read_text())
    man["partial"], man["stopped_reason"] = True, "cost budget: $0.05 spent, limit $0.01"
    (run / "manifest.json").write_text(json.dumps(man))
    v = gate_run("r1")
    assert not v["ok"] and len(v["reasons"]) == 2
    assert "claimed_without_acting" in v["reasons"][0] and "partial" in v["reasons"][1]
