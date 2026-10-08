"""Spec T8.1/T8.2 (LiveKit simulate import/export) and T4.5 (generated sessions are validated
before they are written)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / "fixtures" / "livekit" / "export-audio.json"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    runs.mkdir()
    sess = tmp_path / "sessions"
    shutil.copytree(ROOT / "sessions", sess)
    monkeypatch.setenv("RUNS_DIR", str(runs))
    monkeypatch.setenv("SESSIONS_DIR", str(sess))
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:1")  # nothing listening: log stays empty
    return {"runs": runs, "sessions": sess}


def test_import_livekit_export_builds_a_scorable_run(dirs):
    from gf.engines.livekit_simulate import import_export
    from gf.scoring.score import score_run

    man = import_export(EXPORT, run_id="lk-test")
    assert man["engine"] == "livekit-simulate" and len(man["calls"]) == 2
    folders = [Path(c["record_dir"]) for c in man["calls"]]
    for f in folders:
        for name in ("meta.json", "caller.json", "timeline.json", "backend_log.json", "audio.json"):
            assert (f / name).exists(), name
        meta = json.loads((f / "meta.json").read_text())
        assert meta["engine"] == "livekit-simulate"
        assert meta["livekit"]["status"] in ("STATUS_FAILED", "STATUS_COMPLETED", "STATUS_PASSED")
        assert "wer" in meta["livekit"]["metrics"]["stt"]
        tl = json.loads((f / "timeline.json").read_text())
        assert tl["ux"]["latency_ms"]["n"] > 0  # rebuilt from message timestamps
        caller = json.loads((f / "caller.json").read_text())
        assert caller["reference_is_agent_transcript"] is True and caller["turns_said"]
    summary = score_run("lk-test")
    assert summary["calls"] == 2 and summary["invalid"] == 0
    for a in summary["attempts"]:
        assert a["engine"] == "livekit-simulate"
        assert a["livekit"]["status"]
        checks = {
            c["id"]: c
            for c in json.loads((Path(man["calls"][0]["record_dir"]) / "scores.json").read_text())[
                "checks"
            ]
        }
        assert "not measured" in checks["speech.wer"]["what_happened"]


def test_import_maps_scenario_labels_to_existing_sessions(dirs):
    from gf.engines.livekit_simulate import import_export

    data = json.loads(EXPORT.read_text())
    # rename the first job/scenario to an existing session title
    title = "Refund for a broken blender, clean line"
    data["run"]["jobs"][0]["label"] = title
    data["run"]["scenario_group"]["scenarios"][0]["label"] = title
    p = dirs["runs"] / "export.json"
    p.write_text(json.dumps(data))
    man = import_export(p, run_id="lk-map")
    assert any(s["title"] == title and s["id"] == "7c994c348001" for s in man["sessions"])


def test_export_scenarios_from_sessions(dirs, tmp_path):
    from gf.engines.livekit_simulate import export_scenarios

    out = tmp_path / "scenarios.yaml"
    n = export_scenarios(out)
    scen = yaml.safe_load(out.read_text())
    assert n == len(scen) >= 11
    for sc in scen:
        assert set(sc) == {"label", "instructions", "agent_expectations"}
        assert sc["label"] and sc["instructions"] and sc["agent_expectations"]
    refund = next(s for s in scen if s["label"] == "Refund for a broken blender, clean line")
    assert "G W four eight two one three" in refund["instructions"]
    assert "issue_refund" in refund["agent_expectations"]


def test_generated_proposals_are_validated_before_writing(dirs, tmp_path):
    from gf.sessions.generate import write_proposals

    base = yaml.safe_load((ROOT / "sessions" / "refund-basic.yaml").read_text())
    good = dict(base, title="Generated: refund with a chatty caller")
    bad_tool = json.loads(json.dumps(base))
    bad_tool["title"] = "Generated: bad tool"
    bad_tool["expected"]["tool_calls"]["required"][1]["tool"] = "delete_everything"
    bad_fixture = dict(base, title="Generated: bad fixture", fixtures="nope")
    dup = dict(base, title="Refund for a broken blender, clean line")
    out = tmp_path / "generated"
    rep = write_proposals(
        [good, bad_tool, bad_fixture, dup, "not an object"], out, known_titles=[dup["title"]]
    )
    assert [w["title"] for w in rep["written"]] == [good["title"]]
    assert len(rep["rejected"]) == 4
    reasons = " ".join(r["reason"] for r in rep["rejected"])
    assert "unknown tools" in reasons and "fixture" in reasons and "duplicate" in reasons
    files = list(out.glob("*.yaml"))
    assert len(files) == 1 and not list(out.glob(".*.tmp.yaml"))
    from gf.sessions.schema import Session

    assert Session.load(files[0]).title == good["title"]
