"""Timeline: speech segments per channel from the stereo recording (the source of truth for
timing), merged with tool calls and transcripts into one ordered list, plus the UX numbers
that come straight from audio: response latency, dead air, talk-over, barge-in stop time.

All times are milliseconds from the audio origin (stream-relative sample counts).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from gf.record.model import CallRecord

FRAME_MS = 20
SPEECH_DB = -45.0
MIN_SPEECH_MS = 200
MERGE_GAP_MS = 300  # pauses shorter than this stay inside one utterance


def _rms_db(x: np.ndarray) -> float:
    if len(x) == 0:
        return -120.0
    f = x.astype(np.float32) / 32768.0
    return float(20 * np.log10(np.sqrt(np.mean(f * f)) + 1e-9))


def speech_segments(
    samples: np.ndarray, sample_rate: int, *, threshold_db: float = SPEECH_DB
) -> list[tuple[int, int]]:
    """Energy VAD → merged (start_ms, end_ms) segments."""
    frame = int(sample_rate * FRAME_MS / 1000)
    n = len(samples) // frame
    active = [_rms_db(samples[i * frame : (i + 1) * frame]) > threshold_db for i in range(n)]
    raw: list[list[int]] = []
    start = None
    for i, on in enumerate(active + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            raw.append([start * FRAME_MS, i * FRAME_MS])
            start = None
    merged: list[list[int]] = []
    for s, e in raw:
        if merged and s - merged[-1][1] <= MERGE_GAP_MS:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged if e - s >= MIN_SPEECH_MS]


def build_timeline(record: CallRecord, *, dead_air_ms: int = 3000) -> dict[str, Any]:
    if record.audio_path is None:
        return {"error": "no audio", "events": [], "ux": {}}
    data, sr = sf.read(record.audio_path, dtype="int16")
    if data.ndim == 1:
        caller_pcm, agent_pcm = data, np.zeros_like(data)
    else:
        caller_pcm, agent_pcm = data[:, 0], data[:, 1]
    caller_seg = speech_segments(caller_pcm, sr)
    agent_seg = speech_segments(agent_pcm, sr)
    total_ms = int(len(data) / sr * 1000)

    # response latency: caller segment end → next agent segment start, unless the caller
    # spoke again first (then it is dead air for that turn)
    responses = []
    for i, (_cs, ce) in enumerate(caller_seg):
        next_caller = caller_seg[i + 1][0] if i + 1 < len(caller_seg) else total_ms + 1
        nxt = next((a for a in agent_seg if a[0] >= ce), None)
        if nxt is not None and nxt[0] < next_caller:
            responses.append(
                {"caller_end_ms": ce, "agent_start_ms": nxt[0], "latency_ms": nxt[0] - ce}
            )
        else:
            responses.append({"caller_end_ms": ce, "agent_start_ms": None, "latency_ms": None})

    # dead air: nobody speaking for >= dead_air_ms, after the first speech
    busy = sorted(caller_seg + agent_seg)
    dead = []
    for (_, e1), (s2, _) in zip(busy, busy[1:], strict=False):
        if s2 - e1 >= dead_air_ms:
            dead.append({"start_ms": e1, "end_ms": s2, "gap_ms": s2 - e1})

    # talk-over: agent starts while the caller is speaking; barge-in: caller starts while the
    # agent is speaking, and how long the agent kept talking after that
    talk_over, barge_in = [], []
    for a_s, a_e in agent_seg:
        if any(c_s < a_s < c_end for c_s, c_end in caller_seg):
            talk_over.append({"agent_start_ms": a_s})
        for c_s, _c_e in caller_seg:
            if a_s < c_s < a_e:
                barge_in.append({"caller_start_ms": c_s, "agent_stop_after_ms": a_e - c_s})

    greeting_first = bool(agent_seg) and (not caller_seg or agent_seg[0][0] < caller_seg[0][0])
    first_agent = agent_seg[0][0] if agent_seg else None

    events: list[dict[str, Any]] = []
    for n, (s, e) in enumerate(caller_seg, 1):
        events.append({"t_ms": s, "end_ms": e, "kind": "caller_speech", "n": n})
    for n, (s, e) in enumerate(agent_seg, 1):
        events.append({"t_ms": s, "end_ms": e, "kind": "agent_speech", "n": n})
    for t in record.caller_turns:
        events.append(
            {
                "t_ms": t.t_start_ms,
                "end_ms": t.t_end_ms,
                "kind": "caller_said",
                "text": t.text,
                "n": t.n,
            }
        )
    for h in record.agent_heard:
        events.append({"t_ms": h["t_ms"], "kind": "agent_heard", "text": h["text"]})
    for t in record.agent_turns:
        events.append(
            {
                "t_ms": t.t_ms,
                "kind": "agent_said",
                "text": t.text,
                "n": t.n,
                "interrupted": t.interrupted,
            }
        )
    for c in record.tool_calls:
        events.append(
            {
                "t_ms": c.t_ms,
                "end_ms": c.t_ms + c.duration_ms,
                "kind": "tool_call",
                "tool": c.tool,
                "args": c.args,
                "status": c.status,
                "ok": c.ok,
                "response": c.response,
                "id": c.id,
            }
        )
    for d in dead:
        events.append(
            {
                "t_ms": d["start_ms"],
                "end_ms": d["end_ms"],
                "kind": "dead_air",
                "gap_ms": d["gap_ms"],
            }
        )
    events.sort(key=lambda e: (e["t_ms"], e["kind"]))

    lat = [r["latency_ms"] for r in responses if r["latency_ms"] is not None]
    ux = {
        "duration_ms": total_ms,
        "greeting_first": greeting_first,
        "first_agent_audio_ms": first_agent,
        "caller_segments": len(caller_seg),
        "agent_segments": len(agent_seg),
        "responses": responses,
        "latency_ms": {
            "n": len(lat),
            "p50": _pct(lat, 50),
            "p95": _pct(lat, 95),
            "max": max(lat) if lat else None,
        },
        "dead_air": dead,
        "dead_air_total_ms": sum(d["gap_ms"] for d in dead),
        "talk_over": talk_over,
        "barge_in": barge_in,
        "caller_speech_ms": sum(e - s for s, e in caller_seg),
        "agent_speech_ms": sum(e - s for s, e in agent_seg),
    }
    return {"t0_ms": record.t0_ms, "sample_rate": sr, "events": events, "ux": ux}


def _pct(values: list[int], p: float) -> int | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))]


def write_timeline(folder: str | Path) -> dict[str, Any]:
    record = CallRecord.load(folder)
    tl = build_timeline(record)
    (Path(folder) / "timeline.json").write_text(json.dumps(tl, indent=2))
    return tl
