"""`gf ui`: the live web app (spec T6.3–T6.7). Same templates as `gf report`, rendered from
runs/ and sessions/ on every request, plus run control, system status, session creation,
a JSON API and optional token auth (GF_UI_TOKEN) for hosted deployments."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from gf import __version__
from gf.config import settings
from gf.report import model
from gf.report.render import Links, render
from gf.sessions.schema import Session
from gf.ui import jobs, status

log = logging.getLogger("gf.ui")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Fill the status cache in the background so the header pill is right on the first page."""
    asyncio.get_running_loop().create_task(status.status())
    yield


app = FastAPI(
    title="Call simulator",
    version=__version__,
    docs_url="/api/docs",
    redoc_url=None,
    lifespan=_lifespan,
)
app.add_middleware(GZipMiddleware, minimum_size=2048)
LINKS = Links("live")
COOKIE = "gf_token"


def token() -> str:
    return os.environ.get("GF_UI_TOKEN", "")


def page(template: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
    state, detail = status.provider_state()
    ctx.setdefault("provider_state", state)
    ctx.setdefault("provider_detail", detail)
    return HTMLResponse(render(template, LINKS, **ctx), status_code=status_code)


def _inspector_ctx(run_id: str, call: str = "") -> dict[str, Any]:
    """The run report plus the call the embedded inspector shows (T6.12/T6.14)."""
    st = jobs.job_status(run_id)
    r = model.run_report(run_id, in_progress=st["state"] == "running")
    ctx: dict[str, Any] = {"r": r, "c": None, "start_ms": None, "job": st}
    sel = model.select_call(r, call or None)
    if sel:
        sid, n = sel
        folder = model.call_folder(run_id, sid, n)
        if (folder / "meta.json").exists():
            href = LINKS.audio(run_id, sid, n) if (folder / "audio.wav").exists() else None
            ctx["c"] = model.call_report(run_id, sid, n, audio_href=href)
            ctx["start_ms"] = next(
                (
                    i["t_ms"]
                    for i in r["issues"]["items"]
                    if (i["session_id"], i["attempt"]) == (sid, n)
                ),
                None,
            )
    return ctx


def _issue_link(base: str):
    def link(i: dict[str, Any]) -> str:
        t = i["t_ms"] if i.get("t_ms") is not None else 0
        sep = "" if base.endswith("&") else "?"
        return f"{base}{sep}call={i['session_id']}/{i['attempt']}#t={t}"

    return link


# ---------------------------------------------------------------- auth + errors


@app.middleware("http")
async def _auth(request: Request, call_next):
    tok = token()
    if tok and not request.url.path.startswith(("/login", "/health", "/version")):
        presented = (
            request.cookies.get(COOKIE)
            or request.query_params.get("token")
            or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        )
        if presented != tok:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "unauthorised"}, status_code=401)
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
def login_form(next: str = "/"):
    return page("login.html", next=next, error="")


@app.post("/login")
def login(token_value: str = Form(..., alias="token"), next: str = Form("/")):
    if token_value != token():
        return page("login.html", status_code=401, next=next, error="That token is not right.")
    r = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    r.set_cookie(COOKIE, token_value, httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return r


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    return page("error.html", status_code=exc.status_code, code=exc.status_code, detail=exc.detail)


@app.exception_handler(Exception)
async def _any_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    return page("error.html", status_code=500, code=500, detail=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- pages


@app.get("/", response_class=HTMLResponse)
async def overview(call: str = "", run: str = ""):
    o = model.overview()
    ctx: dict[str, Any] = {"r": None, "c": None, "start_ms": None}
    known = {x["run_id"] for x in o["runs"]} | {x["run_id"] for x in o["checks"]}
    chosen = run if run in known else (o["latest"]["run_id"] if o["latest"] else "")
    if chosen:
        ctx = _inspector_ctx(chosen, call)
        ctx.pop("job", None)
    return page(
        "overview.html",
        o=o,
        st=await status.status(),
        job=jobs.current(),
        sessions=model.sessions_page()["sessions"],
        suites=model.sessions_page()["suites"],
        issue_link=_issue_link(f"/?run={chosen}&") if chosen else _issue_link("/"),
        **ctx,
    )


@app.get("/agent", response_class=HTMLResponse)
def agent():
    return page("agent.html", a=model.agent_page())


@app.get("/backend", response_class=HTMLResponse)
def backend():
    return page("backend.html", b=model.backend_page())


@app.get("/caller", response_class=HTMLResponse)
def caller():
    return page("caller.html")


@app.get("/scoring", response_class=HTMLResponse)
def scoring():
    return page(
        "scoring.html",
        s=model.scoring_page(),
        sessions=model.sessions_page()["sessions"],
        checks=model.overview()["checks"],
        job=jobs.current(),
    )


@app.get("/environments", response_class=HTMLResponse)
def environments():
    return page("environments.html", e=model.environments_page())


@app.get("/sessions", response_class=HTMLResponse)
def sessions():
    return page("sessions.html", p=model.sessions_page())


@app.get("/sessions/new", response_class=HTMLResponse)
def session_new(copy: str = ""):
    text = TEMPLATE_YAML
    if copy:
        try:
            text = model.session_page(copy)["yaml"] or text
        except StopIteration:
            pass
    return page("session_new.html", yaml_text=text, name="", error="")


@app.post("/sessions/new")
def session_create(name: str = Form(...), yaml_text: str = Form(...)):
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in name.strip().lower()).strip(
        "-"
    )
    if not slug:
        return page(
            "session_new.html",
            status_code=400,
            yaml_text=yaml_text,
            name=name,
            error="Give the file a name (letters, digits, dashes).",
        )
    target = settings().sessions_dir / f"{slug}.yaml"
    if target.exists():
        return page(
            "session_new.html",
            status_code=400,
            yaml_text=yaml_text,
            name=name,
            error=f"{target.name} already exists. Sessions are immutable: pick a new name.",
        )
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
        tmp.write(yaml_text)
    try:
        sess = Session.load(tmp.name)
    except Exception as e:  # noqa: BLE001 - the reason is shown to the user
        return page(
            "session_new.html",
            status_code=400,
            yaml_text=yaml_text,
            name=name,
            error=f"Does not validate: {e}",
        )
    finally:
        Path(tmp.name).unlink(missing_ok=True)
    target.write_text(yaml_text if yaml_text.endswith("\n") else yaml_text + "\n")
    return RedirectResponse(f"/sessions/{sess.id}", status_code=303)


@app.post("/sessions/generate")
def sessions_generate(count: int = Form(6), focus: str = Form("")):
    from gf.sessions.generate import generate

    rep = generate(max(1, min(20, count)), focus.strip())
    return page("sessions.html", p=model.sessions_page(), generated=rep)


@app.get("/sessions/{session_id}", response_class=HTMLResponse)
def session(session_id: str):
    try:
        return page("session.html", p=model.session_page(session_id))
    except StopIteration:
        raise HTTPException(404, "unknown session") from None


@app.post("/sessions/{session_id}/suite")
def session_suite(session_id: str, suite: str = Form(...), action: str = Form("add")):
    """Add a session to a suite or remove it (writes sessions/suites.yaml, never the session)."""
    from gf.sessions.taxonomy import add_to_suite, remove_from_suite, session_file

    try:
        sess = next(x for x in model.load_all(settings().sessions_dir) if x.id == session_id)
    except StopIteration:
        raise HTTPException(404, "unknown session") from None
    name = "".join(ch for ch in suite.strip().lower() if ch.isalnum() or ch in "-_")
    if not name:
        raise HTTPException(400, "give the suite a name (letters, digits, dashes)")
    if action == "remove":
        remove_from_suite(settings().sessions_dir, name, session_file(sess))
    else:
        add_to_suite(settings().sessions_dir, name, session_file(sess))
    return RedirectResponse(f"/sessions/{session_id}", status_code=303)


@app.get("/runs", response_class=HTMLResponse)
async def runs():
    sp = model.sessions_page()
    return page(
        "runs.html",
        o=model.overview(),
        job=jobs.current(),
        st=await status.status(),
        sessions=sp["sessions"],
        suites=sp["suites"],
    )


@app.post("/runs/start")
async def run_start(request: Request):
    form = await request.form()
    ids = [str(v) for v in form.getlist("session")]
    pick = str(form.get("pick", "all" if form.get("all") else "pick"))
    suite = ""
    if pick == "all":
        ids = [s["id"] for s in model.sessions_page()["sessions"]]
    elif pick.startswith("suite:"):
        from gf.sessions.taxonomy import sessions_in_suite

        suite = pick.split(":", 1)[1]
        try:
            ids = [x.id for x in sessions_in_suite(settings().sessions_dir, suite)]
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
    try:
        run_id = jobs.start(
            ids,
            int(form.get("repeat", 3)),
            int(form.get("concurrency", 4)),
            str(form.get("run_id", "")).strip(),
            suite=suite or None,
        )
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/checks/run")
async def check_run(request: Request):
    """Detector self-test: run the chosen session(s) with a deliberately wrong agent variant."""
    from datetime import UTC, datetime

    form = await request.form()
    ids = [str(v) for v in form.getlist("session")] or [str(form.get("session_id", ""))]
    ids = [i for i in ids if i]
    variant = str(form.get("variant", "dishonest"))
    from gf.agent.variants import applicable, get_variant
    from gf.sessions.schema import load_all

    try:
        v = get_variant(variant)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    by_id = {x.id: x for x in load_all(settings().sessions_dir)}
    ids = [
        i
        for i in ids
        if i in by_id and applicable(v, [t.tool for t in by_id[i].expected.tool_calls.required])
    ]
    if not ids:
        raise HTTPException(
            400,
            f"none of the chosen sessions use a tool the '{variant}' variant takes away "
            f"({', '.join(v.remove_tools)}), so the check cannot be proven on them",
        )
    run_id = f"check-{variant}-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    try:
        run_id = jobs.start(ids, 1, min(4, max(1, len(ids))), run_id, variant=variant)
    except (RuntimeError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/stop")
def run_stop(run_id: str):
    _require_run(run_id)
    jobs.stop(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.post("/runs/{run_id}/rescore")
def run_rescore(run_id: str):
    _require_run(run_id)
    from gf.scoring.score import score_run

    score_run(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run(run_id: str, call: str = ""):
    run_dir = settings().runs_dir / run_id
    if not (run_dir / "manifest.json").exists() and (run_dir / "job.json").exists():
        st = jobs.job_status(run_id)
        return page("run_starting.html", run_id=run_id, job=st, log_tail=jobs.tail_log(run_id))
    _require_run(run_id)
    ctx = _inspector_ctx(run_id, call)
    st = ctx["job"]
    return page(
        "run.html",
        log_tail=jobs.tail_log(run_id) if st["state"] != "done" else "",
        issue_link=_issue_link(f"/runs/{run_id}"),
        **ctx,
    )


@app.get("/runs/{run_id}/{session_id}/{attempt}", response_class=HTMLResponse)
def call(run_id: str, session_id: str, attempt: int):
    folder = _require_call(run_id, session_id, attempt)
    href = LINKS.audio(run_id, session_id, attempt) if (folder / "audio.wav").exists() else None
    return page("call.html", c=model.call_report(run_id, session_id, attempt, audio_href=href))


@app.get("/runs/{run_id}/{session_id}/{attempt}/audio.wav")
def audio(run_id: str, session_id: str, attempt: int):
    path = _require_call(run_id, session_id, attempt) / "audio.wav"
    if not path.exists():
        raise HTTPException(404, "no audio")
    return FileResponse(path, media_type="audio/wav")


# ---------------------------------------------------------------- JSON API (T6.7)


@app.get("/api/status")
async def api_status(refresh: bool = False):
    return await status.status(force=refresh)


@app.get("/api/runs")
def api_runs():
    return {"runs": model.overview()["runs"], "current": jobs.current()}


@app.get("/api/runs/{run_id}")
def api_run(run_id: str):
    _require_run(run_id)
    r = model.run_report(run_id)
    r.pop("manifest", None)
    return r | {"job": jobs.job_status(run_id)}


@app.get("/api/runs/{run_id}/{session_id}/{attempt}")
def api_call(run_id: str, session_id: str, attempt: int):
    _require_call(run_id, session_id, attempt)
    c = model.call_report(run_id, session_id, attempt)
    c.pop("envelopes", None)
    return c


@app.get("/api/environments")
def api_environments():
    return model.environments_page()


@app.get("/api/sessions")
def api_sessions():
    return model.sessions_page()


@app.get("/api/agent")
def api_agent():
    return model.agent_page()


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/version")
def version():
    return {"version": __version__}


# ---------------------------------------------------------------- helpers


def _require_run(run_id: str) -> None:
    if not (settings().runs_dir / run_id / "manifest.json").exists():
        raise HTTPException(404, f"run {run_id} not found")


def _require_call(run_id: str, session_id: str, attempt: int) -> Path:
    folder = model.call_folder(run_id, session_id, attempt)
    if not (folder / "meta.json").exists():
        raise HTTPException(404, "call not found (not finished yet?)")
    return folder


TEMPLATE_YAML = """title: Refund for a broken blender, calm caller
caller:
  persona: {name: Maria Lopez, style: polite, accent: en-US}
  facts: {order_id: GW-48213, zip: "94110", amount: 89.99, item: countertop blender}
  goal: Get a full refund of $89.99 for order GW-48213, which arrived broken.
  stop_when: the agent confirms the refund was issued
  voice: aura-2-asteria-en
  pace: 1.0
  llm: {model: gpt-4.1-mini, temperature: 0.0, seed: 301}
  conditions: {noise: null, phone_line: false, packet_loss: 0.0, patience_s: 20}
  limits: {max_turns: 12, max_duration_s: 150, mutual_silence_reprompt_s: 4, mutual_silence_abort_s: 8}
fixtures: orders_basic
faults: []
expected:
  outcome: refunded
  tool_calls:
    required:
      - {tool: lookup_order, args: {order_id: GW-48213, zip: "94110"}}
      - {tool: issue_refund, args: {order_id: GW-48213, amount: 89.99}}
    order: [lookup_order, issue_refund]
    forbidden: [escalate_to_human, update_shipping_address]
  final_state:
    - refunds[GW-48213].amount == 89.99
"""
