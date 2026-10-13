"""Traffic-weighted pass rate (scoring/traffic.py) and sessions generated from seed documents
(sessions/generate.py --from-docs), without calling any LLM."""

import json
import shutil

import pytest
import yaml

from gf.config import ROOT
from gf.scoring.stats import wilson
from gf.scoring.traffic import load_traffic, weighted_rate

REC = ROOT / "fixtures" / "records"


def test_weighted_rate_reweights_areas_by_traffic(tmp_path):
    # the suite is refund-heavy and refunds pass; real traffic is mostly address changes
    rows = [
        {"area": "refund", "n": 18, "passed": 18},
        {"area": "address change", "n": 2, "passed": 0},
    ]
    shares = {"refund": 0.2, "address change": 0.8}
    w = weighted_rate(rows, shares)
    assert w["rate"] == pytest.approx(0.2)  # the plain rate would be 18/20 = 90%
    # few calls in the heavy area: the effective sample is small and the interval wide
    assert w["n_eff"] == pytest.approx(1 / (0.04 / 18 + 0.64 / 2), abs=0.01)
    assert w["ci_high"] - w["ci_low"] > 0.5
    assert w["covered_share"] == 1 and w["uncovered"] == []
    # equal shares over one stratum reproduce the plain Wilson interval
    one = weighted_rate([{"area": "refund", "n": 10, "passed": 7}], {"refund": 1.0})
    assert (one["rate"], one["ci_low"], one["ci_high"]) == wilson(7, 10)


def test_areas_without_calls_are_reported_not_guessed():
    rows = [{"area": "refund", "n": 4, "passed": 3}, {"area": "other", "n": 3, "passed": 3}]
    w = weighted_rate(rows, {"refund": 0.6, "escalation": 0.4})
    assert w["uncovered"] == ["escalation"] and w["covered_share"] == pytest.approx(0.6)
    assert w["rate"] == pytest.approx(0.75)  # speaks for the covered 60% only
    assert w["unweighted_areas"] == ["other"]
    none = weighted_rate(rows, {"escalation": 1.0})
    assert none["rate"] is None and none["n_eff"] == 0


def test_traffic_file_is_validated_and_normalised(tmp_path):
    p = tmp_path / "traffic.yaml"
    assert load_traffic(p) is None
    p.write_text("areas: {refund: 3, address change: 1}\n")
    assert load_traffic(p) == {"refund": 0.75, "address change": 0.25}
    p.write_text("areas: {refunds: 1}\n")
    with pytest.raises(ValueError, match="unknown areas"):
        load_traffic(p)
    assert load_traffic(ROOT / "traffic.example.yaml")  # the shipped example parses


def test_score_and_gate_carry_the_weighted_rate(tmp_path, monkeypatch):
    from gf.scoring import gate as gate_mod
    from gf.scoring import score as score_mod
    from gf.scoring import traffic

    runs = tmp_path / "runs"
    monkeypatch.setenv("RUNS_DIR", str(runs))
    run = runs / "r1"
    dst = run / "7c994c348001" / "1"
    shutil.copytree(REC / "refund-basic", dst)
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "r1",
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
    monkeypatch.setattr(traffic, "TRAFFIC_FILE", tmp_path / "none.yaml")
    assert score_mod.score_run("r1")["weighted"] is None
    v = gate_mod.gate_run("r1", min_pass=0.5, weighted=True)
    assert not v["ok"] and "needs traffic.yaml" in v["reasons"][0]

    tf = tmp_path / "traffic.yaml"
    tf.write_text("areas: {refund: 1, escalation: 1}\n")
    monkeypatch.setattr(traffic, "TRAFFIC_FILE", tf)
    s = score_mod.score_run("r1")
    assert s["sessions"][0]["area"] == "refund"
    w = s["weighted"]
    assert w["uncovered"] == ["escalation"] and w["covered_share"] == 0.5 and w["traffic_hash"]
    v = gate_mod.gate_run("r1", min_pass=0.99, weighted=True)
    assert v["weighted"] == w
    assert v["ok"] == (w["ci_high"] >= 0.99)


def test_seed_docs_are_read_within_a_budget(tmp_path):
    from gf.sessions.generate import _prompt, read_seed_docs

    (tmp_path / "a-ticket.txt").write_text("Customer wants a refund, blender broke. " * 10)
    (tmp_path / "b.md").write_text("x" * 500)
    (tmp_path / "photo.png").write_bytes(b"\x89PNG")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.csv").write_text("id,text\n1,where is my order\n")
    info = read_seed_docs(tmp_path, max_chars=600)
    assert [d["file"] for d in info["docs"]] == ["a-ticket.txt", "b.md"]
    assert sum(len(d["text"]) for d in info["docs"]) == 600
    assert info["truncated"] == ["b.md", "sub/c.csv"] and info["skipped"] == ["photo.png"]
    prompt = _prompt(3, "", [], info["docs"])
    assert "=== a-ticket.txt ===" in prompt and '"conversations"' in prompt
    assert "SEED MATERIAL" not in _prompt(3, "", [])
    with pytest.raises(ValueError, match="not a folder"):
        read_seed_docs(tmp_path / "missing")
    (tmp_path / "empty").mkdir()
    with pytest.raises(ValueError, match="no readable text"):
        read_seed_docs(tmp_path / "empty")


def test_seed_sessions_keep_their_source_and_never_copy_identifiers(tmp_path):
    from gf.sessions.generate import (
        count_mix,
        seed_identifiers,
        write_proposals,
        write_traffic_suggestion,
    )

    docs = [
        {
            "file": "t1.txt",
            "text": "From jane.doe@example.com, call me on +1 (415) 555-0134, order GW-99001 "
            "and our test order GW-48213 is in the fixture",
        }
    ]
    ids = seed_identifiers(docs)
    assert {"jane.doe@example.com", "GW-99001"} <= ids and "GW-48213" not in ids
    assert any("555-0134" in x for x in ids)

    base = yaml.safe_load((ROOT / "sessions" / "refund-basic.yaml").read_text())
    good = json.loads(json.dumps(base)) | {
        "title": "Seeded: blender refund",
        "seed_source": "t1.txt",
    }
    leak = json.loads(json.dumps(base)) | {"title": "Seeded: leaky"}
    leak["caller"]["goal"] = "Get a refund; email jane.doe@example.com the receipt"
    rep = write_proposals([good, leak], tmp_path / "gen", forbidden_identifiers=ids)
    assert [w["title"] for w in rep["written"]] == ["Seeded: blender refund"]
    assert "identifiers from the seed" in rep["rejected"][0]["reason"]
    from gf.sessions.schema import Session

    s = Session.load(rep["written"][0]["file"])
    assert s.expected.notes.startswith("seed: t1.txt")

    mix = count_mix([{"area": "Refund"}, {"area": "refund"}, {"area": "billing"}, "junk"])
    assert mix["refund"] == 2 and mix["other"] == 2
    out = write_traffic_suggestion(mix, tmp_path, tmp_path / "traffic.suggested.yaml")
    assert load_traffic(out) == {"refund": 0.5, "other": 0.5}
    assert write_traffic_suggestion({"refund": 0}, tmp_path, tmp_path / "x.yaml") is None
