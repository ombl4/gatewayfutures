"""Span tree of a call (spec T6.24): nesting, durations, tool parenting, OTLP export."""

import json
import shutil

from gf.config import ROOT
from gf.record.model import CallRecord
from gf.record.spans import KINDS, STAGE_KINDS, build_spans, export, to_otlp

FIX = ROOT / "fixtures" / "records"


def _build(name: str):
    folder = FIX / name
    rec = CallRecord.load(folder)
    tl = json.loads((folder / "timeline.json").read_text())
    return rec, build_spans(rec, tl)


def test_tree_is_nested_and_ordered():
    rec, b = _build("refund-basic")
    spans = b["spans"]
    by_id = {s["id"]: s for s in spans}
    assert spans[0]["kind"] == "call" and spans[0]["parent"] is None and spans[0]["depth"] == 0
    assert all(s["kind"] in KINDS for s in spans)
    for s in spans[1:]:
        p = by_id[s["parent"]]
        assert s["depth"] == p["depth"] + 1
        assert s["t0_ms"] >= p["t0_ms"] - 1 and s["t1_ms"] <= p["t1_ms"] + 1, (s["name"], p["name"])
    # children follow their parent directly, so the list reads as a tree
    for i, s in enumerate(spans[1:], 1):
        assert spans[i - 1]["depth"] >= s["depth"] - 1
    assert b["summary"]["replies"] == sum(
        1 for s in spans if s["kind"] == "reply" and "heard_ms" in s["attrs"]
    )


def test_stages_lay_end_to_end_and_add_up_to_the_heard_wait():
    _rec, b = _build("refund-basic")
    spans = b["spans"]
    replies = [s for s in spans if s["kind"] == "reply" and "heard_ms" in s["attrs"]]
    assert replies
    for r in replies:
        kids = [s for s in spans if s["parent"] == r["id"]]
        stages = [s for s in kids if s["kind"] in STAGE_KINDS]
        assert stages[0]["t0_ms"] == r["t0_ms"]
        for a, b_ in zip(stages, stages[1:], strict=False):
            assert b_["t0_ms"] == a["t1_ms"]  # contiguous
        assert sum(1 for s in stages if s["attrs"].get("critical")) == 1
        assert max(stages, key=lambda s: s["duration_ms"])["attrs"].get("critical")
        speech = next(s for s in kids if s["kind"] == "agent_speech")
        total = sum(s["duration_ms"] for s in stages) + sum(
            s["duration_ms"] for s in kids if s["kind"] == "unaccounted"
        )
        assert abs(total - r["attrs"]["heard_ms"]) <= 1 or speech["t0_ms"] - r["t0_ms"] < total
        assert r["status"] == ("slow" if r["attrs"]["heard_ms"] > 2000 else "ok")


def test_failed_tool_call_marks_its_reply_as_error():
    _rec, b = _build("refund-fault-retried")
    spans = b["spans"]
    by_id = {s["id"]: s for s in spans}
    tools = [s for s in spans if s["kind"] == "tool_call"]
    assert tools and all(by_id[t["parent"]]["kind"] == "reply" for t in tools)
    failed = [t for t in tools if t["status"] == "error"]
    assert failed and all(by_id[t["parent"]]["status"] == "error" for t in failed)
    assert b["summary"]["errors"] == len(failed)
    assert failed[0]["attrs"]["http_status"] >= 400


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
    # export writes next to the record (on a copy here, fixtures stay untouched)
    folder = tmp_path / "rec"
    shutil.copytree(FIX / "refund-basic", folder)
    assert export(folder).name == "spans.json"
    out = export(folder, otlp=True)
    assert out.name == "spans.otlp.json" and "resourceSpans" in json.loads(out.read_text())
