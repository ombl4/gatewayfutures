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
    (RECORDS / "refund-noisy", "ef113fc07616", 2, "retired/refund-noisy-cafe.yaml"),
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
    # a call with no recording (as LiveKit-simulator imports have): lanes come from timestamps
    noaudio = run / "ef113fc07616" / "1"
    shutil.copytree(RECORDS / "refund-noisy", noaudio)
    calls.append(
        {"session_id": "ef113fc07616", "attempt": 1, "record_dir": str(noaudio), "call_id": "x"}
    )
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
    tdir = tmp / "targets"
    tdir.mkdir()
    shutil.copy(ROOT / "targets" / "reference.yaml", tdir / "reference.yaml")
    os.environ["TARGETS_DIR"] = str(tdir)
    os.environ.pop("GF_TARGET", None)
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
        ("Settings", "/settings", "Settings"),
    ):
        page.click(f"aside.nav a.item:has-text('{label}')")
        assert urlparse(page.url).path == path
        assert crumb in page.locator(".crumbs").inner_text()
        assert label in page.locator("aside.nav .item.active").inner_text()
    # Settings (T6.36): three tiles, each its own screen with a button back to Settings;
    # the Settings nav item stays lit on those screens
    assert page.locator("#settings-tiles .stile").count() == 3
    for tile, path, crumb in (
        ("#tile-scoring", "/scoring", "Scoring"),
        ("#tile-environments", "/environments", "Environments"),
        ("#tile-agents", "/agent", "Agents under test"),
    ):
        page.click(tile)
        assert urlparse(page.url).path == path
        assert crumb in page.locator(".crumbs").inner_text()
        assert page.locator("aside.nav .item.active").inner_text().strip() == "Settings"
        page.click("#btn-settings-back")
        assert urlparse(page.url).path == "/settings"
    # provider menu (T4.6a): opens from the pill, names the current provider, closes on an
    # outside click, and "Add a provider" reaches the Providers page
    menu = page.locator("#provider-menu .menu")
    assert not menu.is_visible()
    page.click("#provider-menu summary")
    assert menu.is_visible()
    assert "active" in menu.locator(".mi.cur").inner_text()
    assert menu.locator(".mi").count() >= 2  # every target plus "Add an agent"
    page.mouse.click(640, 600)  # anywhere outside the menu
    assert not menu.is_visible()
    page.click("#provider-menu summary")
    page.click("#btn-add-provider")
    # lands at the top of the page with the add form already open (B9)
    assert urlparse(page.url).path == "/agent" and page.locator("#add").is_visible()
    assert page.locator("#add").get_attribute("open") is not None
    assert page.locator("#add-target input[name=id]").is_visible()
    assert page.evaluate("window.scrollY") == 0
    assert page.locator("h1").bounding_box()["y"] >= 0
    assert "Agents under test" in page.locator(".crumbs").inner_text()
    page.goto(site["base"] + "/")
    # agent block rows reach the agent page anchors
    page.click("aside.nav a.row:has-text('Tools')")
    assert page.url.endswith("/agent#tools") and page.locator("#tools").is_visible()
    page.click("aside.nav a.row:has-text('Order system')")
    assert urlparse(page.url).path == "/backend"
    # run selector reflects the chosen run
    page.goto(site["base"] + "/")
    assert page.locator("#run-select").input_value() == "t1"
    page.select_option("#run-select", "t1")
    page.wait_for_url("**/?run=t1")
    assert "t1" in page.locator(".meta").first.inner_text()
    # the set tag opens the session-set page
    page.goto(site["base"] + "/runs/t1")
    page.click(".meta a.pill.grey")
    assert (
        urlparse(page.url).path.startswith("/sets/set-")
        and page.locator("#set-sessions").is_visible()
    )
    # header buttons
    page.click("#btn-new-session")
    assert urlparse(page.url).path == "/sessions/new"
    page.click("#btn-run")
    page.wait_for_url("**/runs#run")
    assert page.locator("details#run").get_attribute("open") is not None
    assert page.locator("#btn-start").is_disabled()  # status says not ready
    _no_errors(page)


# ------------------------------------------------------------------ overview workspace


def test_overview_is_the_performance_page(site, page):
    page.goto(site["base"] + "/")
    assert page.locator("#kpis .kpi").count() == 4
    assert "first run" in page.locator("#kpis").inner_text()
    assert page.locator("details.srow").count() == 0 and page.locator("#flow").count() == 0
    assert page.locator("#by-area .area").count() >= 1 and page.locator("table").count() >= 1
    assert page.locator("#issues").count() == 0  # T6.28: the area pills carry the issues
    area = page.locator(
        "#by-area .area:has(.d.f), #by-area .area:has(.d.x), #by-area .area:has(.d.w)"
    ).first
    assert not area.locator(".pop").is_visible()
    area.locator(".tile").hover()
    assert area.locator(".pop").is_visible()
    tiles = area.locator(".itile")
    assert tiles.count() >= 1
    target = tiles.first.get_attribute("data-issue")
    page.mouse.move(5, 5)
    assert not area.locator(".pop").is_visible()
    area.locator(".tile").click()  # click pins it
    assert area.locator(".pop").is_visible()
    tiles.first.click()
    page.wait_for_selector(".callbox #inspector")
    assert urlparse(page.url).path == "/runs/t1" and f"call={target}" in page.url
    assert page.locator("#inspector").get_attribute("data-call") == target
    # B10: every area, including one with no issue dots, lists calls to open
    page.goto(site["base"] + "/")
    for area in page.locator("#by-area .area").all():
        area.locator(".tile").hover()
        assert area.locator(".itile").count() >= 1, area.get_attribute("data-area")
    clean = page.locator("#by-area .area:has(.itile.ok)")
    if clean.count():  # the fixture run has clean passes only when no fail bar is crossed
        clean.first.locator(".tile").click()
        ok = clean.first.locator(".itile.ok").first
        target = ok.get_attribute("data-issue")
        ok.click()
        page.wait_for_selector(".callbox #inspector")
        assert page.locator("#inspector").get_attribute("data-call") == target
    _no_errors(page)


def test_kpis_accordion_and_issues(site, page):
    page.goto(site["base"] + "/runs/t1")
    assert page.locator("#kpis").count() == 0 and page.locator("#by-area").count() == 0  # T6.30
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
    # Details dropdown (T6.25): the inspector loads inline under the attempt, one at a time
    assert opened.locator(".att .callbtn").count() >= 1 and page.locator("#inspector").count() == 0
    second = rows.nth(1)
    if second.get_attribute("open") is None:
        second.locator("summary").click()
    b1, b2 = opened.locator(".att .callbtn").first, second.locator(".att .callbtn").first
    b1.click()
    page.wait_for_selector("details.srow .callbox #inspector")
    assert page.locator("#inspector").get_attribute("data-call") == b1.get_attribute("data-call")
    assert f"call={b1.get_attribute('data-call')}" in page.url and "on" in b1.get_attribute("class")
    assert "has-call" in page.locator(".work").get_attribute("class")
    b2.click()
    page.wait_for_function(
        "c => document.querySelector('#inspector') && document.querySelector('#inspector').dataset.call === c",
        arg=b2.get_attribute("data-call"),
    )
    assert page.locator("#inspector").count() == 1
    b2.click()  # toggles closed
    assert page.locator("#inspector").count() == 0
    # the ?call= deep link opens the attempt's dropdown at that moment
    target = opened.locator(".att .callbtn").first.get_attribute("data-call")
    page.goto(site["base"] + f"/runs/t1?call={target}#t=1000")
    page.wait_for_selector(".callbox #inspector")
    assert page.locator("#inspector").get_attribute("data-call") == target
    assert (
        page.locator(f'.callbtn[data-call="{target}"]')
        .locator("xpath=ancestor::details")
        .get_attribute("open")
        is not None
    )
    _no_errors(page)


# ------------------------------------------------------------------ inspector interactions


def test_inspector_controls(site, page):
    page.goto(site["base"] + "/runs/t1/7c994c348001/1")
    insp = page.locator("#inspector")
    assert insp.locator("#agent-pill").count() == 1 and insp.locator(".side").count() == 0
    # tabs
    page.click('#inspector [data-tab="grading"]')
    assert page.locator("#grading .card").count() == 5
    verdict = page.locator("#grading .card").nth(4).locator(".pill").first.inner_text().lower()
    header = page.locator("#agent-pill").inner_text().lower()
    assert verdict.split()[0] in header
    for tab in ("spans", "grading", "details", "transcript"):
        page.click(f'#inspector [data-tab="{tab}"]')
        assert page.locator(f'#inspector [data-pane="{tab}"]').is_visible()
        others = page.locator("#inspector .pane:not([hidden])")
        assert others.count() == 1
    # spans tab (T6.35): a trace table with tree, filters, critical path, zoom, panel, playhead
    assert page.locator('#inspector [data-tab="latency"]').count() == 0
    page.click('#inspector [data-tab="spans"]')
    rows = page.locator("#tr-rows .sp")
    total = rows.count()
    assert total > 10 and rows.first.get_attribute("data-kind") == "call"
    assert "voice_call" in rows.first.inner_text() and "OK" in rows.first.inner_text()
    assert page.locator("#tr-head, .tr-head").first.inner_text().startswith("Span name")
    assert (
        page.locator("#tr-rows .sp .bar").count() == total
        and page.locator("#tr-rows .sp .si").count() == total
    )
    assert (
        page.locator('#tr-rows .sp[data-kind="idle"]:visible').count() == 0
    )  # idle hidden by default
    tool = page.locator('#tr-rows .sp[data-kind="tool_call"]').first
    backend = page.locator('#tr-rows .sp[data-kind="backend"]').first
    assert backend.is_hidden()  # tool rows start folded
    tool.locator(".tg").click()
    assert backend.is_visible()
    page.fill("#tr-q", "tool")
    assert (
        page.locator("#tr-rows .sp:visible").count()
        == page.locator('#tr-rows .sp[data-kind="tool_call"]').count()
    )
    page.fill("#tr-q", "")
    some_status = rows.nth(1).get_attribute("data-status")
    page.select_option("#tr-status", some_status)
    assert page.locator("#tr-rows .sp:visible").count() >= 1
    assert page.locator(f'#tr-rows .sp:visible:not([data-status="{some_status}"])').count() == 0
    page.select_option("#tr-status", "")
    page.select_option("#tr-kind", "reasoning")
    assert (
        page.locator("#tr-rows .sp:visible").count()
        == page.locator(
            '#tr-rows .sp[data-kind="reasoning"], #tr-rows .sp[data-kind="llm"]'
        ).count()
    )
    page.select_option("#tr-kind", "")
    page.click("#tr-idle")
    assert page.locator('#tr-rows .sp[data-kind="idle"]:visible').count() >= 1
    page.click("#tr-idle")
    page.click("#tr-crit")
    assert "crit-on" in page.locator("#tr-table").get_attribute("class")
    page.click("#tr-crit")
    page.click("#tr-group")
    assert (
        "flat" in page.locator("#tr-table").get_attribute("class")
        and page.locator("#tr-rows .sp:visible").count() >= total - 10
    )
    page.click("#tr-group")
    lane_before = page.evaluate(
        "getComputedStyle(document.getElementById('tr-table')).getPropertyValue('--lanew')"
    )
    page.click("#tr-zoom-in")
    assert page.locator("#tr-zoom").inner_text() == "150%"
    assert (
        page.evaluate(
            "getComputedStyle(document.getElementById('tr-table')).getPropertyValue('--lanew')"
        )
        != lane_before
    )
    page.click("#tr-zoom-out")
    page.click("#tr-expand")  # tool rows start folded, so the first click expands everything
    assert (
        page.locator("#tr-rows .sp:visible").count()
        >= total
        - page.locator('#tr-rows .sp[data-kind="idle"], #tr-rows .sp[data-kind="dead_air"]').count()
    )
    # the panel: a slow reasoning span opens with its explanation; the parent link selects the parent
    slow_row = page.locator('#tr-rows .sp[data-kind="reasoning"][data-kids="1"]').first
    slow_row.locator(".c-name").click()
    panel = page.locator("#tr-panel")
    assert panel.is_visible() and "agent_reasoning" in panel.locator(".tr-ph .nm").inner_text()
    assert "Latency breakdown" in panel.inner_text() and "Related spans" in panel.inner_text()
    if slow_row.get_attribute("data-status") == "slow":
        assert panel.locator(".tr-alert.slow").is_visible()
    panel.locator('[data-pt="attributes"]').click()
    assert "heard ms" in panel.locator('[data-pp="attributes"]').inner_text()
    panel.locator('[data-pt="transcript"]').click()
    assert len(panel.locator('[data-pp="transcript"]').inner_text()) > 10
    panel.locator('[data-pt="details"]').click()
    panel.locator("[data-sel]").first.click()
    assert "voice_call" in panel.locator(".tr-ph .nm").inner_text()
    # clicking a bar seeks the audio and the playhead appears
    page.evaluate("document.getElementById('insp-audio').currentTime = 0")
    slow_row.locator(".c-lane").click()
    assert page.evaluate("document.getElementById('insp-audio').currentTime") > 0
    page.wait_for_timeout(100)
    assert (
        page.locator("#tr-play").is_visible() and ":" in page.locator("#tr-play-lbl").inner_text()
    )
    page.click("#tr-close")
    assert panel.is_hidden()
    assert (
        page.locator("#latency-glossary").count() == 0
    )  # no glossary block: hover explains a span
    assert page.locator("#tr-tip").is_hidden()
    page.locator("#tr-rows .sp .c-name").nth(1).hover()
    assert page.locator("#tr-tip").is_visible() and len(page.locator("#tr-tip").inner_text()) > 20
    page.click("#tr-expand")  # collapse all leaves the root
    assert page.locator("#tr-rows .sp:visible").count() < total
    page.click('#inspector [data-tab="transcript"]')
    # compact check tags on the transcript: ? opens one popover at a time with the explanation
    assert page.locator('#inspector [data-pane="checks"]').count() == 0  # the Checks tab is gone
    tags = insp.locator("#transcript .tag:has(.q)")
    assert tags.count() >= 4
    tags.nth(0).locator(".q").click()
    assert tags.nth(0).locator(".pop").is_visible()
    assert len(tags.nth(0).locator(".pop .why").inner_text()) > 20
    tags.nth(1).locator(".q").click()
    assert not tags.nth(0).locator(".pop").is_visible() and tags.nth(1).locator(".pop").is_visible()
    page.click("#inspector .abar .tm")
    assert page.locator("#transcript .tag.open").count() == 0
    # transcript annotations: a tool event is tagged with the requirement it met; ? explains it
    tagged = insp.locator("#transcript .toolev .tags.tested .tag").first
    tagged.locator(".q").click()
    assert tagged.locator(".pop").is_visible() and len(tagged.locator(".pop b").inner_text()) > 5
    page.click("#inspector .abar .tm")
    assert page.locator("#caller-pill").is_visible() and page.locator("#agent-pill").is_visible()
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
    # the matching tool event card under Tool calls is selected; its own tabs switch independently
    assert "sel" in page.locator(f'.tev[data-tev="{tid}"]').get_attribute("class")
    assert page.locator('#inspector [data-tab="timeline"]').count() == 0  # T6.33
    page.click('#inspector [data-tab="grading"]')  # T6.32: tool calls live under Grading
    assert page.locator("#tool-calls").is_visible()
    assert page.locator(f'.tev[data-tev="{tid}"]').is_visible()
    assert page.locator(".tev").count() == tool_cards.count()
    page.click(f'.tev[data-tev="{tid}"] [data-tab="resp"]')
    assert page.locator(f'#tev-{tid} [data-pane="resp"]').is_visible()
    assert page.locator(f'#tev-{tid} [data-pane="args"]').is_hidden()
    assert page.locator(
        '#inspector [data-pane="grading"]'
    ).is_visible()  # the outer tab is untouched
    page.click('#inspector [data-tab="transcript"]')
    # seeking: transcript turn, check jump, lanes click, play button
    t = int(insp.locator("#transcript .turn").nth(2).get_attribute("data-t"))
    insp.locator("#transcript .turn").nth(2).click()
    assert abs(page.evaluate("document.getElementById('insp-audio').currentTime") - t / 1000) < 0.3
    page.click('#inspector [data-tab="grading"]')
    jump = insp.locator('[data-pane="grading"] a.jump').first
    jump_ms = int(jump.get_attribute("data-seek"))
    jump.click()
    page.click('#inspector [data-tab="transcript"]')
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
    page.wait_for_selector("#insp-play .i-pause:visible")
    assert page.locator("#insp-play .i-play").is_hidden()
    page.click("#insp-play")
    assert page.evaluate("document.getElementById('insp-audio').paused")
    page.wait_for_selector("#insp-play .i-play:visible")
    assert page.locator("#insp-play .i-pause").is_hidden()
    assert page.locator("#insp-cur").inner_text() != "0:00"
    # prev/next attempt links exist when there is a neighbour (fixture has one attempt per session)
    assert insp.locator(".nav").inner_text()
    _no_errors(page)


def test_inspector_without_recording_still_draws_lanes(site, page):
    page.goto(site["base"] + "/runs/t1/ef113fc07616/1")
    assert page.locator("#insp-audio").count() == 0
    assert page.locator("#insp-play").is_disabled()
    assert page.locator("#insp-play svg:visible").count() == 1  # one icon, not both
    painted = page.evaluate(
        """() => { const c = document.getElementById('insp-lanes'); const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++; return n; }"""
    )
    assert painted > 200  # speech blocks drawn from the timeline
    assert "no recording" in page.locator("#inspector .legend").first.inner_text()
    _no_errors(page)


def test_add_session_to_regression_suite_from_the_ui(site, page):
    page.goto(site["base"] + "/sessions/7c994c348001")
    page.fill("#suites input.input[name=suite]", "regression")
    page.click("#btn-add-suite")
    page.wait_for_url("**/sessions/7c994c348001")
    assert "regression" in page.locator("#suites").inner_text()
    page.goto(site["base"] + "/sessions")
    page.click('#filters [data-filter="suite:regression"]')
    visible = page.locator("#sessions details.srow:not([hidden])")
    ids = [visible.nth(i).get_attribute("data-session") for i in range(visible.count())]
    assert (
        "7c994c348001" in ids and visible.count() < page.locator("#sessions details.srow").count()
    )
    assert page.locator("#sessions-title").inner_text() == "Suite regression"
    total = page.locator("#sessions details.srow").count()
    assert page.locator("#sessions-count").inner_text() == f"({visible.count()} of {total})"
    page.click('#filters [data-filter=""]')
    assert page.locator("#sessions-title").inner_text() == "All sessions"
    page.goto(site["base"] + "/runs")
    assert page.locator('select[name=pick] option[value="suite:regression"]').count() == 1
    page.select_option("select[name=pick]", "suite:regression")
    shown = page.locator("#sessionpick label:visible")
    assert 1 <= shown.count() < page.locator("#sessionpick label").count()
    page.select_option("select[name=pick]", "pick")
    assert (
        page.locator("#sessionpick label:not([hidden])").count()
        == page.locator("#sessionpick label").count()
    )
    assert page.locator("#sessionpick input").first.is_enabled()
    _no_errors(page)


def test_overview_opens_at_the_top(site, page):
    page.goto(site["base"] + "/runs/t1")
    page.click("aside.nav a.item:has-text('Overview')")
    page.wait_for_selector("#kpis")
    page.wait_for_timeout(300)
    assert page.evaluate("window.scrollY") == 0
    page.goto(site["base"] + "/")
    page.wait_for_timeout(300)
    assert page.evaluate("window.scrollY") == 0


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
    assert page.locator("#issues").count() == 0 and page.locator("#by-area").count() == 0
    # Runs list (T6.30): the latest run is expanded to its sessions; Details loads the inspector
    page.goto(site["base"] + "/runs")
    # negative controls (T6.31): the jump button lands on the form at the bottom
    assert page.locator("#selftest").count() == 1
    page.click("#btn-selftest")
    page.wait_for_timeout(200)
    assert page.locator("#selftest-form select[name=variant]").is_visible()
    assert page.url.endswith("#selftest")
    box = page.evaluate("document.getElementById('selftest').getBoundingClientRect().toJSON()")
    assert (
        0 <= box["top"] < page.viewport_size["height"]
    )  # scrolled into view (a short page cannot scroll it to the top)
    row = page.locator("tr.runrow").first
    assert row.get_attribute("data-open") == "1"
    page.wait_for_selector("tr.runbody:not([hidden]) details.srow")
    assert "open" in row.get_attribute("class")
    srow = page.locator("tr.runbody details.srow").first
    if srow.get_attribute("open") is None:
        srow.locator("summary").click()
    srow.locator(".att .callbtn").first.click()
    page.wait_for_selector("tr.runbody .callbox #inspector")
    row.locator(".chev-btn").click()
    assert page.locator("tr.runbody").first.is_hidden()
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
    seen, bad = _crawl(page, site["base"], ["/", "/runs/t1", "/sessions", "/settings", "/agent"])
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
    assert page.locator("#kpis").count() == 0 and page.locator("details.srow").count() == 2
    assert page.locator("#btn-run").count() == 0  # live-only controls are absent
    page.goto((site["static"] / "overview.html").as_uri())
    area = page.locator(
        "#by-area .area:has(.d.f), #by-area .area:has(.d.x), #by-area .area:has(.d.w)"
    ).first
    area.locator(".tile").click()
    area.locator(".itile").first.click()  # static tiles link to the call page
    assert page.url.startswith("file://") and "call-" in page.url
    page.wait_for_selector("#inspector")
    page.click('#inspector [data-tab="grading"]')
    assert page.locator('#inspector [data-pane="grading"]').is_visible()
    page.click('#inspector [data-tab="transcript"]')
    page.locator("#transcript .turn").nth(1).click()
    assert page.evaluate("document.getElementById('insp-audio').currentTime") > 0
    page.goto((site["static"] / "overview.html").as_uri())
    assert page.locator("#inspector").count() == 0  # static pages link to call pages instead
    assert page.locator("#kpis .kpi").count() == 4 and page.locator("details.srow").count() == 0
    page.click("aside.nav a.item:has-text('Practice sessions')")
    assert page.url.endswith("sessions.html")
    _no_errors(page)


def test_register_an_agent_under_test_from_the_ui(site, page):
    """T9.4: the add form registers a target, it appears in the list and the start form, Use
    makes it the header's agent, Remove takes it away; secrets never show."""
    page.goto(site["base"] + "/agent?add=1")
    assert page.locator("#targets tr.target-row").count() == 1
    page.fill("#add-target input[name=id]", "acme")
    page.fill("#add-target input[name=name]", "Acme support line")
    page.fill("#add-target input[name=url]", "wss://acme.livekit.cloud")
    page.fill("#add-target input[name=agent_name]", "acme-agent")
    page.fill("#add-target input[name=api_key]", "APIacmekey")
    page.fill("#add-target input[name=api_secret]", "verysecretvalue")
    page.fill("#add-target input[name=version]", "v3")
    page.click("#btn-add-target")
    page.wait_for_url("**/agent#acme")
    assert page.locator("#targets tr.target-row").count() == 2
    row = page.locator("#acme")
    assert "Acme support line" in row.inner_text() and "acme.livekit.cloud" in row.inner_text()
    assert "APIacmekey" not in page.content() and "verysecretvalue" not in page.content()
    row.locator("button:has-text('Use')").click()
    page.wait_for_url("**/agent#acme")
    assert "active" in page.locator("#acme").inner_text()
    assert "Acme support line" in page.locator("#provider-menu summary").inner_text()
    page.goto(site["base"] + "/runs")
    assert page.locator("#target-select").input_value() == "acme"
    page.goto(site["base"] + "/agent")
    page.locator("#acme button:has-text('Remove')").click()
    page.wait_for_url("**/agent")
    assert page.locator("#targets tr.target-row").count() == 1
    assert "Gateway Goods" in page.locator("#provider-menu summary").inner_text()
    _no_errors(page)
