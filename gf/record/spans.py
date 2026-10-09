"""Spans: one call as a trace (spec T6.24).

A span is a named interval with a parent. The tree is built only from what the record
already holds, so it is deterministic and free to recompute:

    call
    ├── caller turn N                       (recording, left channel)
    ├── reply N  [caller stopped → agent finished speaking]
    │   ├── end of turn / transcription / LLM first token / TTS first byte / playback
    │   │                                   (the agent's own metrics, laid end to end)
    │   ├── unaccounted                     (heard wait minus the reported stages)
    │   ├── tool call <name>                (backend log; error status when it failed)
    │   └── agent speech                    (recording, right channel)
    ├── dead air / talk-over / interruption (recording)

Status per span: `ok`, `slow` (a reply whose heard wait passed the warn threshold, or a
dead-air gap), `error` (a failed tool call). The longest stage of each reply is marked
`critical`, so the critical path of a slow reply is one glance.

`to_otlp` writes the same tree as OTLP/JSON so a trace opens in Jaeger, Tempo or any
OpenTelemetry viewer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gf.record.latency import STAGES, turn_latency
from gf.record.model import CallRecord

STAGE_KINDS = [k.removesuffix("_ms") for k, _l, _d in STAGES]  # eou, stt, llm_ttft, ...
STAGE_LABELS = {k.removesuffix("_ms"): label for k, label, _d in STAGES}
KINDS = [
    "call",
    "caller_speech",
    "reply",
    *STAGE_KINDS,
    "unaccounted",
    "tool_call",
    "agent_speech",
    "dead_air",
    "talk_over",
    "interruption",
]


def build_spans(
    record: CallRecord,
    timeline: dict[str, Any],
    *,
    warn_s: float = 2.0,
    fail_s: float = 3.5,
) -> dict[str, Any]:
    """The span tree of one call plus a summary. Times are ms from the record's t0."""
    ux = timeline.get("ux") or {}
    duration = int(ux.get("duration_ms") or record.audio_duration_s * 1000 or 0)
    lat = turn_latency(record, timeline)["turns"]
    spans: list[dict[str, Any]] = []
    counter = {"n": 0}

    def add(
        name: str,
        kind: str,
        t0: int,
        t1: int,
        parent: str | None,
        *,
        status: str = "ok",
        turn: int | None = None,
        **attrs: Any,
    ) -> dict[str, Any]:
        counter["n"] += 1
        t1 = max(t1, t0)
        span = {
            "id": f"s{counter['n']}",
            "parent": parent,
            "name": name,
            "kind": kind,
            "t0_ms": int(t0),
            "t1_ms": int(t1),
            "duration_ms": int(t1 - t0),
            "status": status,
            "turn": turn,
            "attrs": {k: v for k, v in attrs.items() if v is not None},
        }
        spans.append(span)
        return span

    root = add("call", "call", 0, duration, None, session=record.session_id, call=record.call_id)

    for ct in record.caller_turns:
        add(
            f"caller turn {ct.n}",
            "caller_speech",
            ct.t_start_ms,
            ct.t_end_ms,
            root["id"],
            turn=ct.n,
            text=ct.text[:120],
            interruption=ct.interruption or None,
        )

    replies: list[dict[str, Any]] = []  # (reply span, window start, window end) for tool calls
    for row in lat:
        start = row["t_ms"]
        end = start + (row["duration_ms"] or row.get("tts_audio_ms") or 1000)
        heard = row["heard_ms"]
        if heard is None:
            r = add(
                f"agent turn {row['n']}" + (" (greeting)" if row["greeting"] else ""),
                "reply",
                start,
                end,
                root["id"],
                turn=row["n"],
                text=row["text"],
                greeting=row["greeting"] or None,
            )
            add("agent speech", "agent_speech", start, end, r["id"], turn=row["n"])
            replies.append({"span": r, "w0": start, "w1": end})
            continue
        caller_end = start - heard
        status = "ok" if heard <= warn_s * 1000 else "slow"
        r = add(
            f"reply {row['n']}",
            "reply",
            caller_end,
            end,
            root["id"],
            status=status,
            turn=row["n"],
            heard_ms=heard,
            reported_sum_ms=row["reported_sum_ms"],
            unaccounted_ms=row["unaccounted_ms"],
            e2e_ms=row["e2e_ms"],
            over_fail=heard > fail_s * 1000 or None,
            text=row["text"],
            interrupted=row["interrupted"] or None,
            llm_tokens_in=row.get("llm_tokens_in"),
            llm_tokens_out=row.get("llm_tokens_out"),
        )
        cursor = caller_end
        longest = max(
            ((row[k + "_ms"] or 0, k) for k in STAGE_KINDS),
            default=(0, None),
        )
        for k in STAGE_KINDS:
            ms = row[k + "_ms"]
            if not ms:
                continue
            add(
                STAGE_LABELS[k],
                k,
                cursor,
                cursor + ms,
                r["id"],
                turn=row["n"],
                critical=(k == longest[1]) or None,
            )
            cursor += ms
        if row["unaccounted_ms"] and row["unaccounted_ms"] > 0:
            add(
                "unaccounted",
                "unaccounted",
                cursor,
                start,
                r["id"],
                turn=row["n"],
                note="network, audio buffering and VAD look-back the agent cannot see",
            )
        add(
            "agent speech",
            "agent_speech",
            start,
            end,
            r["id"],
            turn=row["n"],
            interrupted=row["interrupted"] or None,
        )
        replies.append({"span": r, "w0": caller_end, "w1": end})

    for tc in record.tool_calls:
        parent = root["id"]
        turn = None
        for rp in replies:
            if rp["w0"] - 500 <= tc.t_ms <= rp["w1"]:
                parent, turn = rp["span"]["id"], rp["span"]["turn"]
                break
        else:
            # no reply window holds it: the next agent turn is the one it was made for
            nxt = next((rp for rp in replies if rp["w1"] > tc.t_ms), None)
            if nxt is not None and nxt["w0"] - tc.t_ms <= 20_000:
                parent, turn = nxt["span"]["id"], nxt["span"]["turn"]
                nxt["w0"] = min(nxt["w0"], tc.t_ms)
                nxt["span"]["t0_ms"] = min(nxt["span"]["t0_ms"], tc.t_ms)
                nxt["span"]["duration_ms"] = nxt["span"]["t1_ms"] - nxt["span"]["t0_ms"]
        add(
            f"tool call {tc.tool}",
            "tool_call",
            tc.t_ms,
            tc.t_ms + tc.duration_ms,
            parent,
            status="ok" if tc.ok else "error",
            turn=turn,
            tool=tc.tool,
            http_status=tc.status,
            fault=tc.fault,
            tool_id=tc.id,
            args=json.dumps(tc.args, ensure_ascii=False)[:160],
            arg_problems=tc.arg_problems or None,
        )
        if parent != root["id"] and not tc.ok:
            next(s for s in spans if s["id"] == parent)["status"] = "error"

    for d in ux.get("dead_air") or []:
        add(
            "dead air",
            "dead_air",
            d["start_ms"],
            d["end_ms"],
            root["id"],
            status="slow",
            gap_ms=d["gap_ms"],
        )
    for t in ux.get("talk_over") or []:
        add(
            "talk-over",
            "talk_over",
            t["agent_start_ms"],
            t["agent_start_ms"] + 300,
            root["id"],
            status="slow",
        )
    for b in ux.get("barge_in") or []:
        add(
            "interruption",
            "interruption",
            b["caller_start_ms"],
            b["caller_start_ms"] + b["agent_stop_after_ms"],
            root["id"],
            status="ok" if b["agent_stop_after_ms"] <= 1000 else "slow",
            agent_stop_after_ms=b["agent_stop_after_ms"],
        )

    # the agent's last words can land after the recording stopped: the call span covers them
    root["t1_ms"] = max([root["t1_ms"]] + [s["t1_ms"] for s in spans])
    root["duration_ms"] = root["t1_ms"] - root["t0_ms"]
    duration = root["t1_ms"]
    by_id = {s["id"]: s for s in spans}
    for s in spans:
        depth, p = 0, s["parent"]
        while p:
            depth += 1
            p = by_id[p]["parent"]
        s["depth"] = depth
    # children directly after their parent, each level in time order
    ordered: list[dict[str, Any]] = []

    def walk(pid: str | None) -> None:
        for s in sorted(
            (x for x in spans if x["parent"] == pid), key=lambda x: (x["t0_ms"], x["id"])
        ):
            ordered.append(s)
            walk(s["id"])

    walk(None)
    reply_spans = [s for s in ordered if s["kind"] == "reply" and "heard_ms" in s["attrs"]]
    crit = {}
    for s in ordered:
        if s["attrs"].get("critical"):
            crit[s["kind"]] = crit.get(s["kind"], 0) + 1
    summary = {
        "n": len(ordered),
        "duration_ms": duration,
        "replies": len(reply_spans),
        "slow": sum(1 for s in ordered if s["status"] == "slow" and s["kind"] == "reply"),
        "over_fail": sum(1 for s in reply_spans if s["attrs"].get("over_fail")),
        "errors": sum(1 for s in ordered if s["status"] == "error" and s["kind"] == "tool_call"),
        "dead_air": sum(1 for s in ordered if s["kind"] == "dead_air"),
        "critical_stages": sorted(crit.items(), key=lambda kv: -kv[1]),
        "warn_ms": int(warn_s * 1000),
        "fail_ms": int(fail_s * 1000),
    }
    return {"spans": ordered, "summary": summary, "kinds": KINDS, "stage_labels": STAGE_LABELS}


def to_otlp(built: dict[str, Any], record: CallRecord) -> dict[str, Any]:
    """OTLP/JSON (ExportTraceServiceRequest) for the span tree, one trace per call."""
    base_ns = int(record.meta.get("started_ms") or 0) * 1_000_000
    trace_id = hashlib.sha256(record.call_id.encode()).hexdigest()[:32]
    ids = {
        s["id"]: hashlib.sha256(f"{record.call_id}/{s['id']}".encode()).hexdigest()[:16]
        for s in built["spans"]
    }

    def attr(k: str, v: Any) -> dict[str, Any]:
        if isinstance(v, bool):
            val = {"boolValue": v}
        elif isinstance(v, int):
            val = {"intValue": str(v)}
        elif isinstance(v, float):
            val = {"doubleValue": v}
        else:
            val = {"stringValue": json.dumps(v) if isinstance(v, list | dict) else str(v)}
        return {"key": k, "value": val}

    out_spans = []
    for s in built["spans"]:
        attrs = [attr("gf.kind", s["kind"]), attr("gf.status", s["status"])]
        if s["turn"] is not None:
            attrs.append(attr("gf.turn", s["turn"]))
        attrs += [attr(f"gf.{k}", v) for k, v in s["attrs"].items()]
        out_spans.append(
            {
                "traceId": trace_id,
                "spanId": ids[s["id"]],
                "parentSpanId": ids[s["parent"]] if s["parent"] else "",
                "name": s["name"],
                "kind": 1,
                "startTimeUnixNano": str(base_ns + s["t0_ms"] * 1_000_000),
                "endTimeUnixNano": str(base_ns + s["t1_ms"] * 1_000_000),
                "attributes": attrs,
                "status": {"code": 2 if s["status"] == "error" else 1},
            }
        )
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        attr("service.name", "gf-call-simulator"),
                        attr("gf.session_id", record.session_id),
                        attr("gf.call_id", record.call_id),
                        attr("gf.attempt", record.attempt),
                    ]
                },
                "scopeSpans": [{"scope": {"name": "gf.record.spans"}, "spans": out_spans}],
            }
        ]
    }


def export(folder: str | Path, *, otlp: bool = False, **thresholds: float) -> Path:
    """Write `spans.json` (or `spans.otlp.json`) next to the record and return the path."""
    folder = Path(folder)
    record = CallRecord.load(folder)
    timeline = (
        json.loads((folder / "timeline.json").read_text())
        if (folder / "timeline.json").exists()
        else {}
    )
    built = build_spans(record, timeline, **thresholds)
    if otlp:
        out = folder / "spans.otlp.json"
        out.write_text(json.dumps(to_otlp(built, record), indent=1))
    else:
        out = folder / "spans.json"
        out.write_text(json.dumps(built, indent=1))
    return out
