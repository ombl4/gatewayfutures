"""Spec T6.16: every interactive element of the dashboard works in a real browser.

Runs the live app on a free port over a run folder built from the fixture records (with a
synthesised stereo recording so replay buttons exist), drives it with Playwright/Chromium and
checks navigation, header buttons, KPI cards, the session accordion, issue cards, inspector
tabs, the heard switch, replay, seeking from transcript / tool event / lanes / check, console
errors and dead internal links. The static report folder is checked the same way for the
elements that exist there. `make test-ui`.
"""

from __future__ import annotations

import json
import shutil
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import numpy as np
import pytest
import soundfile as sf

pytestmark = pytest.mark.ui
pw = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
RECORDS = ROOT / "fixtures" / "records"
SESSIONS = ROOT / "sessions"
CALLS = (
    (RECORDS / "refund-basic", "7c994c348001", 1, "refund-basic.yaml"),
    (RECORDS / "refund-noisy", "ef113fc07616", 2, "refund-noisy-cafe.yaml"),
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _synth_audio(folder: Path) -> float:
    """A stereo WAV as long as the record: caller tone bursts left, agent tone bursts right."""
    sr = 16_000
    audio = json.loads((folder / "audio.json").read_text())
    secs = float(audio.get("duration_s") or 60.0)
    t = np.arange(int(sr * secs)) / sr
    left = np.where(((t // 2) % 2) == 0, np.sin(2 * np.pi * 220 * t), 0.0) * 0.3
    right = np.where(((t // 2) % 2) == 1, np.sin(2 * np.pi * 330 * t), 0.0) * 0.3
    sf.write(folder / "audio.wav", np.stack([left, right], axis=1), sr, subtype="PCM_16")
    return secs


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui")
    runs, sess = tmp / "runs", tmp / "sessions"
    shutil.copytree(SESSIONS, sess)
    run = runs / "t1"
    calls = []
    for src, sid, n, _ in CALLS:
        dst = run / sid / str(n)
        shutil.copytree(src, dst)
        _synth_audio(dst)
        calls.append({"session_id": sid, "attempt": n, "record_dir": str(dst), "call_id": sid})
    (run / "manifest.json").write_text(
        json.dumps(
            {
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
                    {"id": sid, "title": sid, "path": str(sess / name)} for _, sid, _, name in CALLS
                ],
                "calls": calls,
            }
        )
    )
    import os

    os.environ["RUNS_DIR"] = str(runs)
    os.environ["SESSIONS_DIR"] = str(sess)
    os.environ.pop("GF_UI_TOKEN", None)

    import uvicorn

    from gf.ui import status
    from gf.ui.app import app

    async def canned(force=False):
        return {
            "rows": [{"name": "LiveKit", "ok": True, "detail": "wss://test"}],
            "ready": False,
            "checked_at": time.time(),
        }

    status.status = canned
    status._CACHE["rows"] = [{"name": "LiveKit", "ok": True, "detail": "wss://test"}]
    status._CACHE["at"] = time.time()
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    # static report of the same run, for the file:// checks
    from gf.report.static import render_run

    static = render_run("t1", tmp / "report")
    yield {"base": base, "runs": runs, "static": static}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    pg.errors = errors  # type: ignore[attr-defined]
    yield pg
    ctx.close()


def _no_errors(pg):
    bad = [e for e in pg.errors if "favicon" not in e]
    assert not bad, bad


# ------------------------------------------------------------------ shell and navigation


def test_navigation_and_header(site, page):
    page.goto(site["base"] + "/")
    assert page.locator("aside.nav .item.active").inner_text().strip() == "Overview"
    assert "connected" in page.locator(".status-pill").inner_text()
    for label, path, crumb in (
        ("Practice sessions", "/sessions", "Practice sessions"),
        ("Runs", "/runs", "Runs"),
        ("Scoring", "/scoring", "Scoring"),
        ("Agent settings", "/agent", "Agent settings"),
    ):
        page.click(f"aside.nav a.item:has-text('{label}')")
        assert urlparse(page.url).path == path
        assert crumb in page.locator(".crumbs").inner_text()
        assert label in page.locator("aside.nav .item.active").inner_text()
    # agent block rows reach the agent page anchors
    page.click("aside.nav a.row:has-text('Tools')")
    assert page.url.endswith("/agent#tools") and page.locator("#tools").is_visible()
    page.click("aside.nav a.row:has-text('Order system')")
    assert urlparse(page.url).path == "/backend"
    # header buttons
    page.click("#btn-new-session")
    assert urlparse(page.url).path == "/sessions/new"
    page.click("#btn-run")
    assert (
        urlparse(page.url).path == "/"
        and page.locator("details#run").get_attribute("open") is not None
    )
    assert page.locator("#btn-start").is_disabled()  # status says not ready
    _no_errors(page)


# ------------------------------------------------------------------ overview workspace


def test_kpis_accordion_and_issues(site, page):
    page.goto(site["base"] + "/")
    assert page.locator("#kpis .kpi").count() == 4
    assert "no comparable run" in page.locator("#kpis").inner_text()
    rows = page.locator("details.srow")
    assert rows.count() == 2
    # first row with a failure is open by default; toggling works
    first = rows.first
    was_open = first.get_attribute("open") is not None
    first.locator("summary").click()
    assert (first.get_attribute("open") is not None) != was_open
    first.locator("summary").click()
    assert (first.get_attribute("open") is not None) == was_open
    if first.get_attribute("open") is None:
        first.locator("summary").click()
    opened = first
    assert opened.locator(".cols4 .h").count() == 4  # persona / goal / conditions / expected
    assert opened.locator(".att button.play").count() >= 1
    assert opened.locator(".att a:has-text('Open call')").count() >= 1
    # issue cards carry a timestamp and open the inspector at that moment
    cards = page.locator("#issues a.issue")
    assert cards.count() >= 1
    target = cards.first.get_attribute("data-issue")
    page.click("#issues a.issue >> nth=0")
    page.wait_for_selector("#inspector")
    assert page.locator("#inspector").get_attribute("data-call") == target
    assert f"call={target}" in page.url
    _no_errors(page)


# ------------------------------------------------------------------ inspector interactions


def test_inspector_controls(site, page):
    page.goto(site["base"] + "/runs/t1/7c994c348001/1")
    insp = page.locator("#inspector")
    assert insp.locator(".root").count() == 1
    # tabs
    for tab in ("tools", "timeline", "checks", "details", "transcript"):
        page.click(f'#inspector [data-tab="{tab}"]')
        assert page.locator(f'#inspector [data-pane="{tab}"]').is_visible()
        others = page.locator("#inspector .pane:not([hidden])")
        assert others.count() == 1
    # heard switch
    heard = insp.locator("#transcript .heard").first
    assert heard.is_visible()
    page.click("#insp-heard")
    assert not heard.is_visible()
    page.click("#insp-heard")
    assert heard.is_visible()
    # tool event selection from the inline card
    tool_cards = insp.locator(".toolev")
    assert tool_cards.count() >= 2
    second = tool_cards.nth(1)
    second.click()
    tid = second.get_attribute("data-tool")
    assert page.locator(f'.tev[data-tev="{tid}"]').is_visible()
    assert page.locator(".tev:not([hidden])").count() == 1
    page.click(f'.tev[data-tev="{tid}"] [data-tab="resp"]')
    assert page.locator(f'#tev-{tid} [data-pane="resp"]').is_visible()
    # seeking: transcript turn, check jump, lanes click, play button
    t = int(insp.locator("#transcript .turn").nth(2).get_attribute("data-t"))
    insp.locator("#transcript .turn").nth(2).click()
    assert abs(page.evaluate("document.getElementById('insp-audio').currentTime") - t / 1000) < 0.3
    jump = insp.locator("a.jump:visible").first
    jump_ms = int(jump.get_attribute("data-seek"))
    jump.click()
    assert (
        abs(page.evaluate("document.getElementById('insp-audio').currentTime") - jump_ms / 1000)
        < 0.3
    )
    box = insp.locator("#insp-lanes").bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2)
    cur = page.evaluate("document.getElementById('insp-audio').currentTime")
    total = page.evaluate("document.getElementById('insp-audio').duration")
    assert total * 0.4 < cur < total * 0.6  # clicked in the middle of the lanes
    page.evaluate("document.getElementById('insp-audio').pause()")
    page.click("#insp-play")
    time.sleep(0.6)
    assert page.evaluate("!document.getElementById('insp-audio').paused")
    page.click("#insp-play")
    assert page.evaluate("document.getElementById('insp-audio').paused")
    assert page.locator("#insp-cur").inner_text() != "0:00"
    # prev/next attempt links exist when there is a neighbour (fixture has one attempt per session)
    assert insp.locator(".nav").inner_text()
    _no_errors(page)


def test_hash_time_opens_at_the_moment(site, page):
    page.goto(site["base"] + "/runs/t1/7c994c348001/1#t=5000")
    page.wait_for_selector("#inspector")
    assert abs(page.evaluate("document.getElementById('insp-audio').currentTime") - 5.0) < 0.3
    _no_errors(page)


def test_replay_buttons_drive_the_shared_player(site, page):
    page.goto(site["base"] + "/runs/t1")
    btn = page.locator("table button.play").first
    btn.click()
    assert page.locator("#player").is_visible()
    src = page.evaluate("document.querySelector('#player audio').src")
    assert src.endswith("/audio.wav")
    time.sleep(0.7)
    assert btn.evaluate("b => b.classList.contains('on')")
    assert page.locator("#player audio").evaluate("a => a.currentTime") > 0
    btn.click()  # pause
    assert page.locator("#player audio").evaluate("a => a.paused")
    page.click("#player button")  # close
    assert not page.locator("#player").is_visible()
    _no_errors(page)


def test_run_page_actions_present(site, page):
    page.goto(site["base"] + "/runs/t1")
    assert page.locator("#btn-rescore").is_visible() and page.locator("#btn-prove").is_visible()
    assert page.locator("#every-call").is_visible()
    page.click("#issues a.issue >> nth=0")
    assert "call=" in page.url and page.locator("#inspector").is_visible()
    _no_errors(page)


# ------------------------------------------------------------------ links and layout


def _crawl(page, base, start_paths):
    seen, bad = set(), []
    queue = list(start_paths)
    while queue:
        path = queue.pop()
        if path in seen or len(seen) > 60:
            continue
        seen.add(path)
        resp = page.goto(urljoin(base, path))
        if resp is None or resp.status != 200:
            bad.append(
                (path, resp.status if resp else None, page.locator("body").inner_text()[:160])
            )
            continue
        for href in page.eval_on_selector_all(
            "a[href]", "as => as.map(a => a.getAttribute('href'))"
        ):
            if not href or href.startswith(("#", "http", "mailto")):
                continue
            p = urlparse(urljoin(page.url, href)).path
            if p.startswith("/api/docs") or p.endswith(".wav"):
                continue
            if p not in seen:
                queue.append(p)
    return seen, bad


def test_no_dead_links_and_no_console_errors(site, page):
    seen, bad = _crawl(page, site["base"], ["/", "/runs/t1", "/sessions", "/scoring", "/agent"])
    assert not bad, bad
    assert len(seen) > 10
    _no_errors(page)


@pytest.mark.parametrize("width", [1400, 400])
def test_no_horizontal_overflow(site, browser, width):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    pg = ctx.new_page()
    for path in ("/", "/runs/t1", "/runs/t1/7c994c348001/1", "/sessions", "/agent", "/scoring"):
        pg.goto(site["base"] + path)
        sw, iw = pg.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        assert sw <= iw + 1, (path, width, sw, iw)
    ctx.close()


# ------------------------------------------------------------------ static report from disk


def test_static_report_works_from_disk(site, page):
    index = site["static"] / "index.html"
    page.goto(index.as_uri())
    assert page.locator("#kpis .kpi").count() == 4
    assert page.locator("details.srow").count() == 2
    assert page.locator("#btn-run").count() == 0  # live-only controls are absent
    page.click("#issues a.issue >> nth=0")
    assert page.url.startswith("file://") and "call-" in page.url
    page.wait_for_selector("#inspector")
    page.click('#inspector [data-tab="checks"]')
    assert page.locator('#inspector [data-pane="checks"]').is_visible()
    page.click('#inspector [data-tab="transcript"]')
    page.locator("#transcript .turn").nth(1).click()
    assert page.evaluate("document.getElementById('insp-audio').currentTime") > 0
    page.goto((site["static"] / "overview.html").as_uri())
    assert page.locator("#inspector").count() == 1
    page.click("aside.nav a.item:has-text('Practice sessions')")
    assert page.url.endswith("sessions.html")
    _no_errors(page)
