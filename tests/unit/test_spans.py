"""Span tree of a call (spec T6.24 / T6.35): shape, nesting, tool parenting, OTLP export."""

import json
import shutil

from gf.config import ROOT
from gf.record.model import CallRecord
from gf.record.spans import KINDS, build_spans, export, to_otlp

FIX = ROOT / "fixtures" / "records"


def _build(name: str):
    folder = FIX / name
    rec = CallRecord.load(folder)
    tl = json.loads((folder / "timeline.json").read_text())
    return rec, build_spans(rec, tl)


def test_tree_has_the_reference_shape():
    """voice_call → user turns (end_of_turn, stt) · agent_reasoning (llm, tool calls, idle) ·
    agent_response (tts, playback) · dead air / interruptions; children inside their parent."""
    _rec, b = _build("refund-basic")
    spans = b["spans"]
    by_id = {s["id"]: s for s in spans}
    root = spans[0]
    assert root["name"] == "voice_call" and root["kind"] == "call" and root["parent"] is None
    assert all(s["kind"] in KINDS and s["label"] for s in spans)
    for s in spans[1:]:
        p = by_id[s["parent"]]
        assert s["depth"] == p["depth"] + 1
        assert p["t0_ms"] <= s["t0_ms"] and s["t1_ms"] <= p["t1_ms"], (s["name"], p["name"])
    kinds = {s["kind"] for s in spans}
    assert {
        "user_turn",
        "end_of_turn",
        "stt",
        "reasoning",
        "llm",
        "response",
        "tts",
        "playback",
    } <= kinds
    user = [s for s in spans if s["kind"] == "user_turn"]
    assert user and all(s["name"].startswith("turn_") and s["name"].endswith("_user") for s in user)
    paired = [u for u in user if any(k["parent"] == u["id"] and k["kind"] == "stt" for k in spans)]
    assert paired, "a caller turn followed by a reply carries its end_of_turn and stt"
    reasoning = [s for s in spans if s["kind"] == "reasoning"]
    assert reasoning and all(
        s["name"] == "agent_reasoning" and "heard_ms" in s["attrs"] for s in reasoning
    )
    assert all(any(k["parent"] == r["id"] and k["kind"] == "llm" for k in spans) for r in reasoning)
    assert all(s["attrs"].get("model") for s in spans if s["kind"] == "llm")
    assert b["summary"]["replies"] == len(reasoning)
    # children follow their parent directly, so the list reads as a tree
    for i, s in enumerate(spans[1:], 1):
        assert spans[i - 1]["depth"] >= s["depth"] - 1


def test_stages_are_marked_critical_and_slow_replies_flagged():
    _rec, b = _build("refund-basic")
    spans = b["spans"]
    for parent_kind in ("reasoning", "response"):
        for par in [s for s in spans if s["kind"] == parent_kind]:
            kids = [k for k in spans if k["parent"] == par["id"] and k["kind"] != "idle"]
            if kids:
                assert sum(1 for k in kids if k["attrs"].get("critical")) == 1
                assert max(kids, key=lambda k: k["duration_ms"])["attrs"].get("critical")
    slow = [s for s in spans if s["kind"] == "reasoning" and s["status"] == "slow"]
    assert slow and all(s["attrs"]["heard_ms"] > 2000 for s in slow)
    assert b["summary"]["slow"] == len(slow)
    assert b["summary"]["critical_stages"][0][1] > 0


def test_tool_calls_nest_in_reasoning_with_a_backend_child_and_errors_propagate():
    _rec, b = _build("refund-fault-retried")
    spans = b["spans"]
    by_id = {s["id"]: s for s in spans}
    tools = [s for s in spans if s["kind"] == "tool_call"]
    assert tools and all(by_id[t["parent"]]["kind"] == "reasoning" for t in tools)
    for t in tools:
        kids = [k for k in spans if k["parent"] == t["id"]]
        assert len(kids) == 1 and kids[0]["kind"] == "backend" and kids[0]["status"] == t["status"]
        assert t["attrs"]["tool"] and "http_status" in t["attrs"]
    failed = [t for t in tools if t["status"] == "error"]
    assert failed and failed[0]["attrs"]["http_status"] >= 400
    assert b["summary"]["errors"] == len(failed)


def test_otlp_export_round_trips_ids_and_status(tmp_path):
    rec, b = _build("refund-fault-retried")
    o = to_otlp(b, rec)
    spans = o["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(spans) == len(b["spans"])
    ids = {s["spanId"] for s in spans}
    assert all(len(s["spanId"]) == 16 and len(s["traceId"]) == 32 for s in spans)
    assert all(s["parentSpanId"] in ids for s in spans if s["parentSpanId"])
    assert any(s["status"]["code"] == 2 for s in spans)
    assert all(int(s["endTimeUnixNano"]) >= int(s["startTimeUnixNano"]) for s in spans)
    folder = tmp_path / "rec"
    shutil.copytree(FIX / "refund-basic", folder)
    assert export(folder).name == "spans.json"
    out = export(folder, otlp=True)
    assert out.name == "spans.otlp.json" and "resourceSpans" in json.loads(out.read_text())
