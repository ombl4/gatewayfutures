"""Spec T6.3–T6.7: the live UI over a run folder built from fixture records."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
RECORDS = ROOT / "fixtures" / "records"
SESSIONS = ROOT / "sessions"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A runs/ dir with one run of two fixture records and a copy of sessions/."""
    runs = tmp_path / "runs"
    sess = tmp_path / "sessions"
    shutil.copytree(SESSIONS, sess)
    monkeypatch.setenv("RUNS_DIR", str(runs))
    monkeypatch.setenv("SESSIONS_DIR", str(sess))
    tdir = tmp_path / "targets"
    tdir.mkdir()
    shutil.copy(ROOT / "targets" / "reference.yaml", tdir / "reference.yaml")
    monkeypatch.setenv("TARGETS_DIR", str(tdir))
    monkeypatch.delenv("GF_TARGET", raising=False)
    run = runs / "t1"
    calls = []
    for src, sid, n in (
        (RECORDS / "refund-basic", "7c994c348001", 1),
        (RECORDS / "refund-noisy", "ef113fc07616", 2),
    ):
        dst = run / sid / str(n)
        shutil.copytree(src, dst)
        calls.append(
            {"session_id": sid, "attempt": n, "record_dir": str(dst), "call_id": f"{sid}-{n}"}
        )
    manifest = {
        "run_id": "t1",
        "folder": str(run),
        "started_at": "2026-10-08T10:00:00+00:00",
        "agent_config_hash": "a7c6a425319d",
        "sessions_hash": "abc",
        "repeat": 2,
        "concurrency": 2,
        "engine": "gf-caller",
        "duration_s": 120.0,
        "sessions": [
            {
                "id": "7c994c348001",
                "title": "Refund for a broken blender, clean line",
                "path": str(sess / "refund-basic.yaml"),
            },
            {
                "id": "ef113fc07616",
                "title": "Refund, noisy cafe",
                "path": str(sess / "retired" / "refund-noisy-cafe.yaml"),
            },
        ],
        "calls": calls,
    }
    (run / "manifest.json").write_text(json.dumps(manifest))

    from gf.ui import status

    async def canned(force=False):
        return {
            "rows": [{"name": "order system (mock backend)", "ok": False, "detail": "down"}],
            "ready": False,
            "checked_at": 0,
        }

    monkeypatch.setattr(status, "status", canned)
    from gf.ui.app import app

    return {
        "runs": runs,
        "sessions": sess,
        "client": TestClient(app, raise_server_exceptions=False),
    }


def test_pages_render(env):
    c = env["client"]
    for path in (
        "/",
        "/agent",
        "/backend",
        "/caller",
        "/scoring",
        "/sessions",
        "/sessions/7c994c348001",
        "/sessions/new",
        "/sessions/new?copy=7c994c348001",
        "/runs",
        "/runs/t1",
        "/runs/t1/7c994c348001/1",
        "/runs/t1/ef113fc07616/2",
        "/providers",
    ):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert "<title>" in r.text
    assert c.get("/nope").status_code == 404
    assert "404" in c.get("/nope").text
    assert c.get("/api/nope").json()["error"]
    assert c.get("/runs/t1/zzz/1").status_code == 404
    call = c.get("/runs/t1/7c994c348001/1").text
    assert 'data-pane="spans"' in call and 'id="tr-data"' in call  # T6.24/T6.35
    # T6.25: the run pages carry no inspector; each attempt's Details loads it from here
    assert 'id="inspector"' not in c.get("/runs/t1").text
    assert (
        'class="srow"' not in c.get("/").text and 'class="srow"' in c.get("/runs/t1").text
    )  # T6.27
    assert 'class="btn sm callbtn"' in c.get("/runs/t1").text
    frag = c.get("/runs/t1/7c994c348001/1/inspector?t=1200")
    assert frag.status_code == 200 and 'id="inspector"' in frag.text and "<title>" not in frag.text
    assert c.get("/runs/t1/zzz/1/inspector").status_code == 404


def test_run_page_content(env):
    r = env["client"].get("/runs/t1").text
    assert "Refund for a broken blender, clean line" in r
    assert "dot pending" in r  # repeat 2, one attempt per session on disk
    assert '<details class="srow"' in r and 'class="srow" data-session' in r
    assert (
        " open>" not in r.split('id="sessions"')[1].split("</div>\n</div>")[0]
    )  # all rows start closed
    assert "Every call" in r
    call = env["client"].get("/runs/t1/7c994c348001/1").text
    assert "lookup_order" in call and "issue_refund" in call
    assert (
        "Grading" in call and 'data-pane="checks"' not in call and 'data-tab="transcript"' in call
    )
    assert 'data-tab="latency"' not in call and "What each span means" not in call  # T6.35
    assert 'data-tip="The caller speaking' in call  # hover explanations per span
    assert 'id="tr-table"' in call and "voice_call" in call and "agent_reasoning" in call
    assert 'data-tab="spans"' in call and 'id="tr-tip"' in call
    assert 'data-tab="grading"' in call
    assert 'data-pane="tools"' not in call and 'id="tool-calls"' in call  # T6.32
    assert 'id="caller-pill"' in call and 'id="agent-pill"' in call
    assert 'class="tags tested"' in call and 'data-check="tools.required.issue_refund"' in call
    assert 'data-check="ux.latency_p95"' in call
    for step in (
        "Step 1 · Simulated caller: did it do its job?",
        "Step 2 · Did the agent do what the session asks?",
        "Step 3 · Was the agent honest?",
        "Step 4 · Was the call good to be on?",
        "Step 5 · Verdict",
    ):
        assert step in call
    assert "Must call issue_refund with" in call
    assert 'id="passing"' in env["client"].get("/sessions/7c994c348001").text
    assert call.count('class="tag ') >= 10 and 'data-check="ux.latency_p95"' in call
    assert 'aria-label="explain latency_p95"' in call
    assert 'id="time-breakdown"' in r
    assert "refund" in r
    runs_page = env["client"].get("/runs").text
    assert 'value="suite:regression"' in runs_page and 'name="pick"' in runs_page
    sessions = env["client"].get("/sessions").text
    assert 'data-filter="area:refund"' in sessions
    assert 'id="sessions-count"' in sessions and 'data-label="Suite regression"' in sessions
    sp = env["client"].get("/sessions/7c994c348001").text
    assert 'id="btn-add-suite"' in sp
    r2 = env["client"].post(
        "/sessions/7c994c348001/suite",
        data={"suite": "Regression!", "action": "add"},
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert "regression" in (env["sessions"] / "suites.yaml").read_text()
    assert ">regression<" in env["client"].get("/sessions/7c994c348001").text
    env["client"].post(
        "/sessions/7c994c348001/suite", data={"suite": "regression", "action": "remove"}
    )
    assert env["client"].post("/sessions/zzz/suite", data={"suite": "x"}).status_code == 404
    from gf.environment import set_tag

    tag = set_tag(["7c994c348001", "ef113fc07616"])
    setp = env["client"].get(f"/sets/{tag}").text
    assert (
        'id="set-sessions"' in setp
        and "Refund for a broken blender, clean line" in setp
        and "t1" in setp
    )
    assert env["client"].get("/sets/set-00000000").status_code == 404
    assert f'href="/sets/{tag}"' in r
    envs = env["client"].get("/environments").text
    assert "Environments" in envs and "t1" in envs  # untagged fixture run is listed as such
    assert "set-" in r  # the run page carries the session-set tag even for untagged runs
    assert env["client"].get("/api/environments").status_code == 200
    # the provider menu (T4.6a): current provider named, designed-for ones listed, add link
    home_html = env["client"].get("/").text
    assert 'id="provider-menu"' in home_html and 'id="btn-add-provider"' in home_html
    assert "Gateway Goods support line" in home_html and "Add an agent" in home_html
    prov = env["client"].get("/providers").text
    assert "design-note.md" in prov and "Day 1" not in prov  # no week plan on the page
    assert env["client"].get("/api/providers").json()["current"]["key"] == "livekit"
    home = env["client"].get("/?run=t1").text
    assert 'id="run-select"' in home and 'value="t1" selected' in home
    assert env["client"].get("/?run=nope").status_code == 200  # unknown run falls back to latest
    assert "Not ready" in env["client"].get("/runs").text


def test_api(env):
    c = env["client"]
    assert c.get("/health").json() == {"ok": True}
    assert "version" in c.get("/version").json()
    assert [r["run_id"] for r in c.get("/api/runs").json()["runs"]] == ["t1"]
    run = c.get("/api/runs/t1").json()
    assert run["overall"]["n"] == 2 and run["job"]["state"] == "done"
    call = c.get("/api/runs/t1/7c994c348001/1").json()
    assert call["verdict"] in ("pass", "fail", "invalid")
    assert {g["id"] for g in call["groups"]} >= {"tools", "claims", "speech", "ux"}
    assert "envelopes" not in call
    assert len(c.get("/api/sessions").json()["sessions"]) >= 11
    assert c.get("/api/agent").json()["cfg"]["tools"]
    assert c.get("/api/status").json()["ready"] is False


def test_stale_summary_is_rescored(env):
    from gf.report.model import run_report

    run = env["runs"] / "t1"
    run_report("t1")
    summary = run / "summary.json"
    assert summary.exists()
    first = summary.stat().st_mtime
    os.utime(run / "manifest.json", (first + 10, first + 10))
    run_report("t1")
    assert summary.stat().st_mtime > first


def test_start_refused_while_running(env):
    from gf.ui import jobs

    run = env["runs"] / "t2"
    run.mkdir()
    (run / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": "t2",
                "sessions": [{"id": "x", "title": "x", "path": "x"}],
                "repeat": 2,
                "calls": [],
            }
        )
    )
    (run / "job.json").write_text(json.dumps({"pid": os.getpid(), "started_from": "ui"}))
    assert jobs.current()["run_id"] == "t2"
    r = env["client"].post("/runs/start", data={"all": "1", "repeat": "1"})
    assert r.status_code == 400 and "already in progress" in r.text
    assert "Run in progress" in env["client"].get("/").text
    assert "Run in progress" in env["client"].get("/runs").text


def test_starting_run_renders_instead_of_404(env):
    run = env["runs"] / "t4"
    run.mkdir()
    (run / "job.json").write_text(json.dumps({"pid": os.getpid(), "started_from": "ui"}))
    r = env["client"].get("/runs/t4")
    assert r.status_code == 200 and "Starting" in r.text
    (run / "job.json").write_text(json.dumps({"pid": 2**22 + 12345, "started_from": "ui"}))
    r = env["client"].get("/runs/t4")
    assert r.status_code == 200 and "Did not start" in r.text


def test_check_run_refuses_sessions_the_variant_cannot_prove(env):
    from gf.sessions.schema import load_all

    esc = next(
        s
        for s in load_all(env["sessions"])
        if all(t.tool != "issue_refund" for t in s.expected.tool_calls.required)
        and all(t.tool != "update_shipping_address" for t in s.expected.tool_calls.required)
    )
    r = env["client"].post("/checks/run", data={"session": esc.id, "variant": "dishonest"})
    assert r.status_code == 400 and "cannot be proven" in r.text
    assert not any(p.name.startswith("check-") for p in env["runs"].iterdir())


def test_rerun_starts_a_child_run_without_touching_the_parent(env, monkeypatch):
    from gf.ui import jobs

    calls = []

    def fake_start(
        ids, repeat, concurrency, run_id="", variant=None, suite=None, parent_run=None, **kw
    ):
        calls.append((sorted(ids), repeat, concurrency, parent_run))
        return "child-1"

    monkeypatch.setattr(jobs, "start", fake_start)
    r = env["client"].post("/runs/t1/rerun", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/runs/child-1"
    assert calls[-1] == (["7c994c348001"], 2, 2, "t1")  # the retired session is skipped
    r = env["client"].post(
        "/runs/t1/rerun", data={"session": "7c994c348001"}, follow_redirects=False
    )
    assert calls[-1][0] == ["7c994c348001"]
    assert (env["runs"] / "t1" / "manifest.json").exists()
    page = env["client"].get("/runs/t1").text
    assert 'id="btn-rerun"' in page and "rerun-session" in page


def test_dead_job_reads_as_failed_and_stop_is_safe(env):
    from gf.ui import jobs

    run = env["runs"] / "t3"
    run.mkdir()
    (run / "manifest.json").write_text(
        json.dumps({"run_id": "t3", "sessions": [], "repeat": 1, "calls": []})
    )
    (run / "job.json").write_text(json.dumps({"pid": 2**22 + 12345}))
    assert jobs.job_status("t3")["state"] == "failed"
    assert jobs.stop("t3") is False
    assert jobs.current() is None


def test_session_form_creates_immutable_file(env):
    c = env["client"]
    text = (
        (env["sessions"] / "refund-basic.yaml")
        .read_text()
        .replace("Refund for a broken blender, clean line", "Form test, refund on a clean line")
    )
    r = c.post(
        "/sessions/new", data={"name": "Form Test!", "yaml_text": text}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/sessions/")
    assert (env["sessions"] / "form-test.yaml").exists()
    sid = r.headers["location"].rsplit("/", 1)[-1]
    assert "Form test, refund on a clean line" in c.get(f"/sessions/{sid}").text
    dup = c.post("/sessions/new", data={"name": "form-test", "yaml_text": text})
    assert dup.status_code == 400 and "already exists" in dup.text
    bad = c.post(
        "/sessions/new", data={"name": "bad", "yaml_text": "title: x\ncaller: {goal: y}\n"}
    )
    assert bad.status_code == 400 and "Does not validate" in bad.text
    assert not (env["sessions"] / "bad.yaml").exists()
    unknown_tool = text.replace("tool: issue_refund", "tool: delete_everything")
    assert (
        c.post("/sessions/new", data={"name": "bad2", "yaml_text": unknown_tool}).status_code == 400
    )


def test_token_auth(env, monkeypatch):
    monkeypatch.setenv("GF_UI_TOKEN", "s3cret")
    c = env["client"]
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.get("/api/runs").status_code == 401
    assert c.get("/health").status_code == 200
    assert c.get("/api/runs", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert c.post("/login", data={"token": "wrong", "next": "/"}).status_code == 401
    ok = c.post("/login", data={"token": "s3cret", "next": "/runs"}, follow_redirects=False)
    assert ok.status_code == 303 and "gf_token" in ok.headers.get("set-cookie", "")
    assert c.get("/runs").status_code == 200  # cookie kept by the client


def test_static_report_renders(env, tmp_path):
    from gf.report.static import render_run

    out = render_run("t1", tmp_path / "out")
    assert (out / "index.html").exists()
    assert (out / "call-7c994c348001-1.html").exists()
    assert "Refund for a broken blender, clean line" in (out / "index.html").read_text()
    assert (out / "sessions.html").exists() and (out / "scoring.html").exists()
    assert "Voice agent providers" in (out / "providers.html").read_text()
    assert 'href="agent.html?add=1"' in (out / "index.html").read_text()
    page = (out / "call-7c994c348001-1.html").read_text()
    assert 'href="index.html"' in page and "lookup_order" in page
    assert 'data-pane="spans"' in page


def test_area_pills_carry_their_sessions_issues(env):
    """T6.28: an area's tiles are exactly the failing, invalid and flagged attempts of the
    sessions in that area, each pointing at a call."""
    from gf.report import model

    r = model.run_report("t1")
    areas = {a["area"]: a for a in r["by_area"]}
    assert areas
    for a in areas.values():
        in_area = {s["session_id"] for s in r["sessions"] if a["area"] in s.get("areas", [])}
        for i in a["issues"]:
            assert i["session_id"] in in_area and i["severity"] in (
                "critical",
                "high",
                "simulation",
            )
            assert "attempt" in i and "t_ms" in i
        for f in a["flagged"]:
            assert f["session_id"] in in_area and f["flags"]
        assert a["n_fail"] + a["n_invalid"] == sum(
            len(i.get("attempts") or [1]) for i in a["issues"]
        )
    seen = {
        (i["session_id"], n)
        for a in areas.values()
        for i in a["issues"]
        for n in (i.get("attempts") or [i["attempt"]])
    }
    for x in r["failing"] + r["invalid"]:
        assert (x["session_id"], x["attempt"]) in seen
    html = env["client"].get("/").text
    assert 'class="area"' in html and "itile" in html and 'id="issues"' not in html


def test_rename_run_rewrites_every_reference(env):
    """T7.5: the folder moves and the id changes in the manifest, summary, registry and a
    child's parent_run; the renamed run still renders."""
    import json

    from gf.config import settings
    from gf.runs_archive import next_run_id, rename

    root = settings().runs_dir
    reg = root / "_environments"
    reg.mkdir(exist_ok=True)
    (reg / "env-deadbeef.json").write_text(
        json.dumps({"env_tag": "env-deadbeef", "runs": [{"run_id": "t1", "set_tag": "set-1"}]})
    )
    from gf.scoring.score import score_run

    score_run("t1")  # a real summary.json, so the renamed run renders
    child = root / "child"
    child.mkdir()
    (child / "manifest.json").write_text(
        json.dumps({"run_id": "child", "parent_run": "t1", "sessions": [], "calls": []})
    )
    dst = rename("t1", "base-001")
    assert dst.name == "base-001" and not (root / "t1").exists()
    man = json.loads((dst / "manifest.json").read_text())
    assert man["run_id"] == "base-001" and "/t1" not in man["folder"]
    assert all("/t1/" not in c["record_dir"] for c in man["calls"])
    assert json.loads((dst / "summary.json").read_text())["run_id"] == "base-001"
    assert json.loads((reg / "env-deadbeef.json").read_text())["runs"][0]["run_id"] == "base-001"
    assert json.loads((child / "manifest.json").read_text())["parent_run"] == "base-001"
    assert env["client"].get("/runs/base-001").status_code == 200
    assert env["client"].get("/runs/t1").status_code == 404
    assert next_run_id("base-001") == "base-002"
    (root / "base-002").mkdir()
    assert next_run_id("base-001") == "base-003" and next_run_id("t1") == ""
    import pytest

    with pytest.raises(ValueError):
        rename("base-001", "base-002")
    with pytest.raises(ValueError):
        rename("nope", "x")


def test_issue_breakdown_rows(env):
    """T6.29: one row per kind of problem with count, share and seriousness; rendered on the
    overview and the run page with the caller-quality card."""
    from gf.report import model

    r = model.run_report("t1")
    b = r["issue_breakdown"]
    assert b["total"] == 2 and b["rows"]
    keys = {row["key"] for row in b["rows"]}
    assert any(k.startswith("flag:") for k in keys)  # the fixture calls pass with flags
    for row in b["rows"]:
        assert (
            row["count"] >= 1
            and 0 < row["share"] <= 1
            and row["status"] in ("high", "medium", "low")
        )
        assert len(row["series"]) == b["runs"] >= 1
    statuses = [row["status"] for row in b["rows"]]
    assert statuses == sorted(statuses, key=["high", "medium", "low"].index)
    html = env["client"].get("/").text
    assert "Performance by area" in html and "Issue breakdown" in html
    assert 'id="simulation-quality"' in html and 'class="area"' in html
    run_html = env["client"].get("/runs/t1").text  # T6.30: the run page keeps none of them
    assert "Performance by area" not in run_html and 'id="kpis"' not in run_html
    runs_html = env["client"].get("/runs").text
    assert 'data-open="1"' in runs_html and 'data-src="/runs/t1/sessions"' in runs_html
    frag = env["client"].get("/runs/t1/sessions")
    assert frag.status_code == 200 and 'class="srow"' in frag.text and "<title>" not in frag.text
    assert env["client"].get("/runs/zzz/sessions").status_code == 404


def test_negative_controls_live_on_the_runs_page(env):
    """T6.31: the detector-check form sits at the bottom of Runs with a jump button; the
    Scoring page only points there."""
    c = env["client"]
    runs = c.get("/runs").text
    assert (
        'id="selftest"' in runs and 'id="btn-selftest"' in runs and 'action="/checks/run"' in runs
    )
    assert "Negative controls" in runs
    scoring = c.get("/scoring").text
    assert 'action="/checks/run"' not in scoring and "#selftest" in scoring


def test_agents_under_test_page(env, monkeypatch):
    """T9.4: list, add (secrets never rendered), test (stubbed connector), activate, remove;
    the header and the start form follow the active target."""
    from gf import targets, targets_check

    c = env["client"]
    page_html = c.get("/agent").text
    assert "Agents under test" in page_html and 'id="targets"' in page_html
    assert "Gateway Goods support line" in page_html and 'id="add-target"' in page_html
    r = c.post(
        "/agents",
        data={
            "id": "acme",
            "name": "Acme support",
            "url": "wss://acme.livekit.cloud",
            "agent_name": "acme-agent",
            "api_key": "APIacme",
            "api_secret": "s3cretvalue",
            "version": "v7",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/agent#acme"
    listing = c.get("/agent").text
    assert "Acme support" in listing and "acme.livekit.cloud" in listing and "v7" in listing
    assert "APIacme" not in listing and "s3cretvalue" not in listing
    assert targets.load("acme").credentials()[1] == "APIacme"
    dup = {"id": "acme", "name": "x", "url": "wss://x", "agent_name": "a"}
    assert c.post("/agents", data=dup).status_code == 400
    assert c.post("/agents", data=dup | {"id": "Bad Id"}).status_code == 400

    async def fake_test(t, *, timeout_s=20.0):
        res = {
            "target_id": t.id,
            "ok": True,
            "reason": "ok",
            "message": targets_check.REASONS["ok"],
            "agent_identity": "agent-xyz",
            "joined_ms": 900,
            "spoke_ms": 1800,
            "error": None,
            "checked_at": "2026-10-09T12:00:00+00:00",
        }
        targets_check.store(t.id, res)
        return res

    monkeypatch.setattr(targets_check, "connection_test", fake_test)
    assert c.post("/agents/acme/test", follow_redirects=False).status_code == 303
    after = c.get("/agent?tested=acme").text
    assert 'id="test-result"' in after and "Connected · Acme support" in after
    assert "agent-xyz" in after
    assert c.post("/agents/nope/test", follow_redirects=False).status_code == 404
    assert c.post("/agents/acme/activate", follow_redirects=False).status_code == 303
    assert targets.active_id() == "acme"
    home = c.get("/").text
    assert 'id="provider-menu"' in home and "Acme support" in home
    runs_page = c.get("/runs").text
    assert 'id="target-select"' in runs_page and 'value="acme" selected' in runs_page
    assert c.post("/agents/reference/remove").status_code == 400
    assert c.post("/agents/acme/remove", follow_redirects=False).status_code == 303
    assert not targets.exists("acme") and targets.active_id() == "reference"
