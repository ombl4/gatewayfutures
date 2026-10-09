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
    good = json.loads(json.dumps(base))
    good["title"] = "Generated: refund with a chatty caller"
    good["expected"]["tool_calls"]["required"][1]["args"]["reason"] = (
        "free text the agent words itself"
    )
    bad_tool = json.loads(json.dumps(base))
    bad_tool["title"] = "Generated: bad tool"
    bad_tool["expected"]["tool_calls"]["required"][1]["tool"] = "delete_everything"
    bad_fixture = dict(base, title="Generated: bad fixture", fixtures="nope")
    dup = dict(base, title="Refund for a broken blender, clean line")
    refund_shipped = json.loads(json.dumps(base))  # GW-48377 is shipped, not delivered
    refund_shipped["title"] = "Generated: refund on a shipped order"
    refund_shipped["caller"]["facts"] = {"order_id": "GW-48377", "zip": "60614", "amount": 42.5}
    refund_shipped["expected"]["tool_calls"]["required"][1]["args"] = {
        "order_id": "GW-48377",
        "amount": 42.5,
    }
    wrong_zip = json.loads(json.dumps(base))
    wrong_zip["title"] = "Generated: wrong zip in facts"
    wrong_zip["caller"]["facts"]["zip"] = "02139"
    no_address = json.loads(json.dumps(base))
    no_address["title"] = "Generated: address change without an address"
    no_address["caller"]["facts"] = {"order_id": "GW-48502", "zip": "02139"}
    no_address["expected"]["tool_calls"]["required"] = [
        {"tool": "update_shipping_address", "args": {"order_id": "GW-48502"}}
    ]
    out = tmp_path / "generated"
    rep = write_proposals(
        [good, bad_tool, bad_fixture, dup, "not an object", refund_shipped, wrong_zip, no_address],
        out,
        known_titles=[dup["title"]],
    )
    assert [w["title"] for w in rep["written"]] == [good["title"]]
    assert len(rep["rejected"]) == 7
    reasons = " ".join(r["reason"] for r in rep["rejected"])
    for needle in (
        "unknown tools",
        "fixture",
        "duplicate",
        "not delivered",
        "does not match order",
        "new_address is missing",
    ):
        assert needle in reasons, needle
    files = list(out.glob("*.yaml"))
    assert len(files) == 1 and not list(out.glob(".*.tmp.yaml"))
    from gf.sessions.schema import Session

    written = Session.load(files[0])
    assert written.title == good["title"]
    assert "reason" not in written.expected.tool_calls.required[1].args  # free text never pinned


def test_generator_rejects_expectations_that_contradict_the_agent_policy():
    from gf.sessions.generate import _check_against_agent_policy, _check_final_state

    base = {
        "faults": [{"tool": "issue_refund", "type": "error_500", "nth": 1}],
        "expected": {
            "tool_calls": {
                "required": [
                    {"tool": "lookup_order"},
                    {"tool": "issue_refund"},
                    {"tool": "issue_refund"},
                ],
                "forbidden": ["escalate_to_human"],
            },
            "final_state": [],
        },
    }
    with pytest.raises(ValueError, match="does not retry"):
        _check_against_agent_policy(base)
    base["expected"]["tool_calls"]["required"] = [
        {"tool": "lookup_order"},
        {"tool": "issue_refund"},
    ]
    with pytest.raises(ValueError, match="forbids escalate_to_human"):
        _check_against_agent_policy(base)
    base["expected"]["tool_calls"]["forbidden"] = []
    _check_against_agent_policy(base)  # honest failure + escalation allowed: fine
    denied = {
        "expected": {
            "tool_calls": {"required": [{"tool": "lookup_order"}]},
            "final_state": ["refunds[GW-48911].amount == 15.00"],
        }
    }
    with pytest.raises(ValueError, match="pre-existing refund"):
        _check_final_state(denied)


def test_retired_sessions_load_for_old_runs_but_are_not_offered(tmp_path):
    import shutil
    from pathlib import Path

    from gf.sessions.schema import is_retired, load_all

    root = Path(__file__).resolve().parents[2]
    folder = tmp_path / "sessions"
    folder.mkdir()
    shutil.copy(root / "sessions" / "refund-basic.yaml", folder / "refund-basic.yaml")
    (folder / "retired").mkdir()
    shutil.copy(root / "sessions" / "refund-noisy-cafe.yaml", folder / "retired" / "old.yaml")
    everything = load_all(folder)
    assert len(everything) == 2 and sum(is_retired(s) for s in everything) == 1
    assert len(load_all(folder, include_retired=False)) == 1


def test_scoring_finds_a_session_whose_file_moved(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path

    from gf.scoring.score import _load_session

    root = Path(__file__).resolve().parents[2]
    folder = tmp_path / "sessions"
    (folder / "retired").mkdir(parents=True)
    shutil.copy(root / "sessions" / "refund-basic.yaml", folder / "retired" / "refund-basic.yaml")
    monkeypatch.setenv("SESSIONS_DIR", str(folder))
    sess = _load_session(
        {"id": "7c994c348001", "path": str(tmp_path / "gone" / "refund-basic.yaml")}
    )
    assert sess.id == "7c994c348001"
    with pytest.raises(FileNotFoundError, match="retired"):
        _load_session({"id": "nope", "path": str(tmp_path / "gone" / "x.yaml")})
