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
    assert "Every call" in r
    call = env["client"].get("/runs/t1/7c994c348001/1").text
    assert "lookup_order" in call and "issue_refund" in call
    assert "Checks" in call and 'data-tab="transcript"' in call
    assert 'data-tab="latency"' in call and "What each latency means" in call
    assert 'id="time-breakdown"' in r
    assert "Not ready" in env["client"].get("/").text


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
