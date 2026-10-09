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
                "path": str(sess / "refund-noisy-cafe.yaml"),
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
    ):
        r = c.get(path)
        assert r.status_code == 200, (path, r.status_code)
        assert "<title>" in r.text
    assert c.get("/nope").status_code == 404
    assert "404" in c.get("/nope").text
    assert c.get("/api/nope").json()["error"]
    assert c.get("/runs/t1/zzz/1").status_code == 404


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
    assert "Checks" in call and 'data-tab="transcript"' in call
    assert 'data-tab="latency"' in call and "What each latency means" in call
    assert 'data-tab="grading"' in call
    assert 'data-tab="flow"' in call and 'id="flowgrid"' in call
    for lane in ("Caller", "Agent heard", "Agent decision", "Tools", "Agent response", "Evaluator"):
        assert f"</i>{lane}</div>" in call
    assert call.count('class="exhd') >= 2
    assert 'id="caller-pill"' in call and 'id="agent-pill"' in call
    assert 'class="tags tested"' in call and 'data-check="tools.required.issue_refund"' in call
    assert 'data-check="ux.latency_p95"' in call
    assert 'id="simulation-quality"' in r
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
    assert 'id="by-area"' in r and "refund" in r
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

    def fake_start(ids, repeat, concurrency, run_id="", variant=None, suite=None, parent_run=None):
        calls.append((sorted(ids), repeat, concurrency, parent_run))
        return "child-1"

    monkeypatch.setattr(jobs, "start", fake_start)
    r = env["client"].post("/runs/t1/rerun", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/runs/child-1"
    assert calls[-1] == (["7c994c348001", "ef113fc07616"], 2, 2, "t1")
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
    page = (out / "call-7c994c348001-1.html").read_text()
    assert 'href="index.html"' in page and "lookup_order" in page
