"""Spans: one call as a trace (spec T6.24 / T6.35).

A span is a named interval with a parent, a type, a status and attributes. The tree is
built only from what the record already holds (recording, agent metrics, backend log), so
it is deterministic and free to recompute:

    voice_call
    ├── turn_N_user                 caller speech, through end-of-turn detection and STT
    │   ├── end_of_turn (vad)       the turn detector deciding the caller was done
    │   └── stt (<model>)           transcription delay
    ├── agent_reasoning             from the transcript to the first TTS byte request
    │   ├── llm (<model>)           time to first token, with token counts
    │   ├── tool_call <name>        from the order system's log; error when it failed
    │   │   └── backend <name>      the order system's own handling of that request
    │   └── idle                    reasoning time the agent's metrics do not explain
    ├── agent_response              from the first TTS byte to the end of the agent's speech
    │   ├── tts (<model>)           time to first byte
    │   └── playback                first byte to audio in the room
    ├── dead_air · talk_over · interruption   from the recording

Status per span: `ok`, `slow` (a reply whose heard wait passed the warn threshold marks its
reasoning span; a dead-air gap; an interruption the agent took over a second to yield to),
`error` (a failed tool call). The longest child of each user turn, reasoning and response
span is marked `critical`.

`to_otlp` writes the same tree as OTLP/JSON so a trace opens in Jaeger, Tempo or any
OpenTelemetry viewer.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from gf.record.latency import turn_latency
from gf.record.model import CallRecord

KINDS = [
    "call",
    "user_turn",
    "end_of_turn",
    "stt",
    "reasoning",
    "llm",
    "tool_call",
    "backend",
    "idle",
    "response",
    "tts",
    "playback",
    "dead_air",
    "talk_over",
    "interruption",
]
IDLE_KINDS = ("idle", "dead_air")  # hidden unless "show idle time"
STAGE_KINDS = ["end_of_turn", "stt", "llm", "tts", "playback"]
STAGE_LABELS = {
    "end_of_turn": "End-of-turn detection",
    "stt": "Transcription",
    "llm": "LLM first token",
    "tts": "TTS first byte",
    "playback": "Playback",
    "tool_call": "Tool calls",
    "idle": "Idle",
}
KIND_LABELS = {
    "call": "Voice call",
    "user_turn": "User turn",
    "end_of_turn": "End-of-turn detection",
    "stt": "Speech-to-text",
    "reasoning": "Agent reasoning",
    "llm": "LLM",
    "tool_call": "Tool call",
    "backend": "Backend",
    "idle": "Idle time",
    "response": "Agent response",
    "tts": "Text-to-speech",
    "playback": "Playback",
    "dead_air": "Dead air",
    "talk_over": "Talk-over",
    "interruption": "Interruption",
}
KIND_HELP = {
    "call": "The whole call, from the first sound to the last word.",
    "user_turn": (
        "The caller speaking, through end-of-turn detection and transcription: the agent "
        "cannot start thinking before this ends."
    ),
    "end_of_turn": "Caller stopped speaking → the turn detector decided the caller was done.",
    "stt": "End of turn → the final transcript arrived from speech recognition.",
    "reasoning": (
        "Transcript → the first text-to-speech request: the LLM's first token, any tool calls, "
        "and idle time the agent's metrics do not explain. The caller is waiting during all of it."
    ),
    "llm": "Transcript sent to the language model → its first token (time to first token).",
    "tool_call": (
        "A request to the order system, as its log recorded it. Red when the system rejected it "
        "or failed."
    ),
    "backend": (
        "The order system's own handling of that request, from its log; the authoritative "
        "record of what happened."
    ),
    "idle": (
        "Reasoning time the agent's own metrics do not explain: network, audio buffering, "
        "VAD look-back."
    ),
    "response": (
        "First text-to-speech request → the agent's last word: time to first byte, playback, "
        "then the speech itself."
    ),
    "tts": "First sentence sent to text-to-speech → first audio byte back (time to first byte).",
    "playback": "First audio byte → it is actually playing into the room.",
    "dead_air": "Neither side speaking, measured from the recording.",
    "talk_over": "The agent started speaking while the caller was still talking.",
    "interruption": (
        "The caller cut in while the agent was speaking; the span is how long the agent kept "
        "talking."
    ),
}
PAIR_WINDOW_MS = 2500  # a reply belongs to the caller turn that ended just before it


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
    models = record.meta.get("agent_models") or {}
    stt_model = (models.get("stt") or {}).get("model") or "stt"
    llm_model = (models.get("llm") or {}).get("model") or "llm"
    tts_model = (models.get("tts") or {}).get("model") or "tts"
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
        t1 = max(int(t1), int(t0))
        span = {
            "id": f"s{counter['n']}",
            "parent": parent,
            "name": name,
            "kind": kind,
            "label": KIND_LABELS.get(kind, kind),
            "help": KIND_HELP.get(kind, ""),
            "t0_ms": int(t0),
            "t1_ms": t1,
            "duration_ms": t1 - int(t0),
            "status": status,
            "turn": turn,
            "attrs": {k: v for k, v in attrs.items() if v is not None},
        }
        spans.append(span)
        return span

    root = add(
        "voice_call", "call", 0, duration, None, session=record.session_id, call=record.call_id
    )

    # caller turns, paired below with the reply that followed each of them
    user_spans: dict[int, dict[str, Any]] = {}
    for ct in record.caller_turns:
        user_spans[ct.n] = add(
            f"turn_{ct.n}_user",
            "user_turn",
            ct.t_start_ms,
            ct.t_end_ms,
            root["id"],
            turn=ct.n,
            text=ct.text[:160],
            interruption=ct.interruption or None,
        )
    used_turns: set[int] = set()

    def caller_turn_for(caller_end: int) -> dict[str, Any] | None:
        best = None
        for ct in record.caller_turns:
            if ct.n in used_turns or ct.t_end_ms > caller_end + 300:
                continue
            if caller_end - ct.t_end_ms <= PAIR_WINDOW_MS and (
                best is None or ct.t_end_ms > best.t_end_ms
            ):
                best = ct
        if best is None:
            return None
        used_turns.add(best.n)
        return user_spans[best.n]

    windows: list[dict[str, Any]] = []  # reasoning spans with the window tool calls fall in
    for row in lat:
        start = row["t_ms"]
        end = start + (row["duration_ms"] or row.get("tts_audio_ms") or 1000)
        heard = row["heard_ms"]
        text = row["text"]
        if heard is None:
            # greeting, or a turn whose wait could not be measured: the response alone
            r = add(
                "agent_response" + (" (greeting)" if row["greeting"] else ""),
                "response",
                start,
                end,
                root["id"],
                turn=row["n"],
                text=text,
                interrupted=row["interrupted"] or None,
                greeting=row["greeting"] or None,
            )
            if row["tts_ttfb_ms"]:
                t0 = max(0, start - (row["playback_ms"] or 0) - row["tts_ttfb_ms"])
                r["t0_ms"] = min(r["t0_ms"], t0)
                r["duration_ms"] = r["t1_ms"] - r["t0_ms"]
                add(
                    f"tts ({tts_model})",
                    "tts",
                    t0,
                    t0 + row["tts_ttfb_ms"],
                    r["id"],
                    turn=row["n"],
                )
                if row["playback_ms"]:
                    add(
                        "playback",
                        "playback",
                        t0 + row["tts_ttfb_ms"],
                        start,
                        r["id"],
                        turn=row["n"],
                    )
            continue
        caller_end = start - heard
        eou, stt = row["eou_ms"] or 0, row["stt_ms"] or 0
        llm, tts, play = row["llm_ttft_ms"] or 0, row["tts_ttfb_ms"] or 0, row["playback_ms"] or 0
        stt_end = caller_end + eou + stt
        tts_start = max(stt_end, start - play - tts)
        slow = heard > warn_s * 1000
        u = caller_turn_for(caller_end)
        if u is not None:
            # the caller's turn runs until the agent has its transcript
            u["t1_ms"] = max(u["t1_ms"], stt_end)
            u["duration_ms"] = u["t1_ms"] - u["t0_ms"]
            if eou:
                add(
                    "end_of_turn (vad)",
                    "end_of_turn",
                    caller_end,
                    caller_end + eou,
                    u["id"],
                    turn=row["n"],
                )
            if stt:
                add(f"stt ({stt_model})", "stt", caller_end + eou, stt_end, u["id"], turn=row["n"])
        rs = add(
            "agent_reasoning",
            "reasoning",
            stt_end,
            tts_start,
            root["id"],
            status="slow" if slow else "ok",
            turn=row["n"],
            heard_ms=heard,
            reported_ms=row["reported_sum_ms"],
            e2e_ms=row["e2e_ms"],
            over_fail=heard > fail_s * 1000 or None,
            model=llm_model,
            input_tokens=row.get("llm_tokens_in"),
            output_tokens=row.get("llm_tokens_out"),
        )
        if llm:
            add(
                f"llm ({llm_model})",
                "llm",
                stt_end,
                stt_end + llm,
                rs["id"],
                turn=row["n"],
                model=llm_model,
                input_tokens=row.get("llm_tokens_in"),
                output_tokens=row.get("llm_tokens_out"),
            )
        windows.append({"span": rs, "w0": caller_end, "w1": tts_start, "turn": row["n"]})
        rp = add(
            "agent_response",
            "response",
            tts_start,
            end,
            root["id"],
            turn=row["n"],
            text=text,
            interrupted=row["interrupted"] or None,
        )
        if tts:
            add(f"tts ({tts_model})", "tts", tts_start, tts_start + tts, rp["id"], turn=row["n"])
        if play:
            add("playback", "playback", tts_start + tts, start, rp["id"], turn=row["n"])

    # tool calls inside the reasoning they belong to (the next reply when no window holds them)
    for tc in record.tool_calls:
        parent, turn = root["id"], None
        hit = next((w for w in windows if w["w0"] - 500 <= tc.t_ms <= w["w1"] + 500), None)
        if hit is None:
            nxt = next((w for w in windows if w["w1"] > tc.t_ms), None)
            if nxt is not None and nxt["w0"] - tc.t_ms <= 20_000:
                hit = nxt
                sp = nxt["span"]
                sp["t0_ms"] = min(sp["t0_ms"], tc.t_ms)
                sp["duration_ms"] = sp["t1_ms"] - sp["t0_ms"]
        if hit is not None:
            parent, turn = hit["span"]["id"], hit["turn"]
        t = add(
            f"tool_call {tc.tool}",
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
            args=json.dumps(tc.args, ensure_ascii=False)[:200],
            response=json.dumps(tc.response, ensure_ascii=False)[:200],
            arg_problems=tc.arg_problems or None,
        )
        add(
            f"backend {tc.tool} (order system)",
            "backend",
            tc.t_ms,
            tc.t_ms + tc.duration_ms,
            t["id"],
            status="ok" if tc.ok else "error",
            turn=turn,
            http_status=tc.status,
            fault=tc.fault,
        )
    # reasoning time the agent's metrics do not explain: idle, at the end of the reasoning
    for w in windows:
        rs = w["span"]
        kids = [s for s in spans if s["parent"] == rs["id"]]
        idle = rs["duration_ms"] - sum(k["duration_ms"] for k in kids)
        if idle > 100:
            last_end = max((k["t1_ms"] for k in kids), default=rs["t0_ms"])
            add(
                "idle",
                "idle",
                min(last_end, rs["t1_ms"]),
                rs["t1_ms"],
                rs["id"],
                turn=rs["turn"],
                note="reasoning time the agent's metrics do not explain: "
                "network, buffering, VAD look-back",
            )

    for d in ux.get("dead_air") or []:
        add(
            "dead_air",
            "dead_air",
            d["start_ms"],
            d["end_ms"],
            root["id"],
            status="slow",
            gap_ms=d["gap_ms"],
        )
    for t in ux.get("talk_over") or []:
        add(
            "talk_over",
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

    # a parent always covers its children (the agent's clock and the recording can disagree by
    # a few hundred ms); the call span covers everything, even words after the recording stopped
    by_id = {s["id"]: s for s in spans}
    for _ in range(3):
        for s in spans:
            if s["parent"]:
                par = by_id[s["parent"]]
                par["t0_ms"] = min(par["t0_ms"], s["t0_ms"])
                par["t1_ms"] = max(par["t1_ms"], s["t1_ms"])
                par["duration_ms"] = par["t1_ms"] - par["t0_ms"]
    for s in spans:
        depth, p = 0, s["parent"]
        while p:
            depth += 1
            p = by_id[p]["parent"]
        s["depth"] = depth
    for s in spans:
        if s["kind"] in ("reasoning", "response", "user_turn"):
            kids = [k for k in spans if k["parent"] == s["id"] and k["kind"] != "idle"]
            if kids:
                max(kids, key=lambda k: k["duration_ms"])["attrs"]["critical"] = True
    ordered: list[dict[str, Any]] = []

    def walk(pid: str | None) -> None:
        for s in sorted(
            (x for x in spans if x["parent"] == pid), key=lambda x: (x["t0_ms"], x["id"])
        ):
            ordered.append(s)
            walk(s["id"])

    walk(None)
    reasoning = [s for s in ordered if s["kind"] == "reasoning"]
    stage_time: dict[str, int] = {}
    for s in ordered:
        if s["kind"] in STAGE_KINDS or s["kind"] == "tool_call":
            stage_time[s["kind"]] = stage_time.get(s["kind"], 0) + s["duration_ms"]
    summary = {
        "n": len(ordered),
        "duration_ms": root["t1_ms"],
        "replies": len(reasoning),
        "slow": sum(1 for s in reasoning if s["status"] == "slow"),
        "over_fail": sum(1 for s in reasoning if s["attrs"].get("over_fail")),
        "errors": sum(1 for s in ordered if s["status"] == "error" and s["kind"] == "tool_call"),
        "dead_air": sum(1 for s in ordered if s["kind"] == "dead_air"),
        "critical_stages": sorted(stage_time.items(), key=lambda kv: -kv[1]),
        "warn_ms": int(warn_s * 1000),
        "fail_ms": int(fail_s * 1000),
        "avg_reasoning_ms": (
            int(sum(s["duration_ms"] for s in reasoning) / len(reasoning)) if reasoning else None
        ),
    }
    return {
        "spans": ordered,
        "summary": summary,
        "kinds": KINDS,
        "stage_labels": STAGE_LABELS,
        "kind_labels": KIND_LABELS,
    }


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
