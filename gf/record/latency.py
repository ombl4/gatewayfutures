"""Per-turn latency breakdown (spec T6.17).

Two clocks, kept apart on purpose:

* **heard** — measured from the stereo recording: the caller stops speaking → the agent's audio
  starts. This is the only latency a caller experiences.
* **reported** — the agent pipeline's own metrics for the same turn: end-of-turn detection,
  transcription delay, LLM time to first token, TTS time to first byte, playback latency, and
  its own end-to-end figure. The difference between heard and the sum of the stages is shown
  as *unaccounted* (network, audio buffering, VAD look-back, clock skew).
"""

from __future__ import annotations

from typing import Any

from gf.record.model import CallRecord

STAGES: list[tuple[str, str, str]] = [
    # key, label, plain-language definition
    (
        "eou_ms",
        "End-of-turn detection",
        "Caller stopped speaking → the turn detector decided the caller was done. Before this, the agent cannot start thinking.",
    ),
    (
        "stt_ms",
        "Transcription",
        "End of turn → the final transcript arrived from speech recognition.",
    ),
    (
        "llm_ttft_ms",
        "LLM first token",
        "Transcript sent to the language model → first token of the reply (time to first token, TTFT).",
    ),
    (
        "tts_ttfb_ms",
        "TTS first byte",
        "First sentence sent to text-to-speech → first audio byte back (time to first byte, TTFB).",
    ),
    (
        "playback_ms",
        "Playback",
        "First audio byte → it is actually played into the room.",
    ),
]

GLOSSARY = [(label, definition) for _k, label, definition in STAGES] + [
    (
        "Reported end-to-end",
        "The agent's own sum for the turn (end of turn → audio playing), from its clock.",
    ),
    (
        "Heard",
        "Measured from the recording: caller's last word → agent's first sound. What a real caller waits.",
    ),
    (
        "Unaccounted",
        "Heard minus the reported stages: network, audio buffering and VAD look-back that the agent cannot see.",
    ),
]

MATCH_WINDOW_MS = 15_000


def turn_latency(record: CallRecord, timeline: dict[str, Any]) -> dict[str, Any]:
    """Per agent turn: heard latency (recording) and the reported stages (agent metrics)."""
    t0 = record.t0_ms
    groups = _metric_groups(record)
    responses = (timeline.get("ux") or {}).get("responses", [])
    used: set[int] = set()
    turns = []
    for at in record.agent_turns:
        m = at.metrics or {}
        start = (
            int(m["started_speaking_at"] * 1000) - t0 if m.get("started_speaking_at") else at.t_ms
        )
        end = int(m["stopped_speaking_at"] * 1000) - t0 if m.get("stopped_speaking_at") else None
        heard = _nearest_response(responses, start)
        grp = _group_for(groups, start, used)
        row: dict[str, Any] = {
            "n": at.n,
            "text": at.text[:90],
            "t_ms": start,
            "duration_ms": (end - start) if end is not None else None,
            "interrupted": at.interrupted,
            "heard_ms": heard,
            "eou_ms": _ms(grp.get("end_of_utterance_delay")) if grp else None,
            "stt_ms": _ms(grp.get("transcription_delay")) if grp else None,
            "llm_ttft_ms": _ms(m.get("llm_node_ttft")) or (_ms(grp.get("ttft")) if grp else None),
            "tts_ttfb_ms": _ms(m.get("tts_node_ttfb")) or (_ms(grp.get("ttfb")) if grp else None),
            "playback_ms": _ms(m.get("playback_latency")),
            "e2e_ms": _ms(m.get("e2e_latency")),
            "llm_tokens_in": grp.get("prompt_tokens") if grp else None,
            "llm_tokens_out": grp.get("completion_tokens") if grp else None,
            "tts_audio_ms": _ms(grp.get("audio_duration")) if grp else None,
        }
        stages = [row[k] for k, _l, _d in STAGES if row[k] is not None]
        row["reported_sum_ms"] = sum(stages) if stages else None
        row["unaccounted_ms"] = (
            heard - row["reported_sum_ms"]
            if heard is not None and row["reported_sum_ms"] is not None
            else None
        )
        row["greeting"] = heard is None and at.n == 1
        turns.append(row)
    keys = [k for k, _l, _d in STAGES] + ["e2e_ms", "heard_ms", "unaccounted_ms"]
    summary = {k: _p50_p95([t[k] for t in turns if t.get(k) is not None]) for k in keys}
    return {"turns": turns, "summary": summary, "stages": STAGES, "glossary": GLOSSARY}


def breakdown_medians(record: CallRecord, timeline: dict[str, Any]) -> dict[str, int | None]:
    """Per-call medians per stage, stored in scores.json so runs can aggregate them."""
    tl = turn_latency(record, timeline)
    return {k: v["p50"] for k, v in tl["summary"].items()}


def _metric_groups(record: CallRecord) -> list[dict[str, Any]]:
    """Agent metric events grouped per speech id (eou + llm + tts of one reply), with the
    time of the end-of-turn event relative to the record."""
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for e in record.agent_events:
        if e.get("kind") != "metrics" or not e.get("speech_id"):
            continue
        sid = e["speech_id"]
        if sid not in by_id:
            by_id[sid] = {"t_ms": int(e["ts_ms"]) - record.t0_ms}
            order.append(sid)
        g = by_id[sid]
        mt = e.get("metric_type")
        if mt == "eou_metrics":
            g["t_ms"] = int(e["ts_ms"]) - record.t0_ms
            g["end_of_utterance_delay"] = e.get("end_of_utterance_delay")
            g["transcription_delay"] = e.get("transcription_delay")
        elif mt == "llm_metrics" and not e.get("cancelled"):
            g["ttft"] = e.get("ttft")
            g["prompt_tokens"] = e.get("prompt_tokens")
            g["completion_tokens"] = e.get("completion_tokens")
        elif mt == "tts_metrics" and not e.get("cancelled"):
            g["ttfb"] = e.get("ttfb")
            g["audio_duration"] = e.get("audio_duration")
    return [by_id[s] for s in order]


def _group_for(groups: list[dict[str, Any]], start_ms: int, used: set[int]) -> dict[str, Any]:
    """The latest unused metric group whose end-of-turn event precedes this turn's audio."""
    best = None
    for i, g in enumerate(groups):
        if i in used:
            continue
        if g["t_ms"] <= start_ms + 500 and start_ms - g["t_ms"] <= MATCH_WINDOW_MS:
            if best is None or g["t_ms"] > groups[best]["t_ms"]:
                best = i
    if best is None:
        return {}
    used.add(best)
    return groups[best]


def _nearest_response(responses: list[dict[str, Any]], start_ms: int) -> int | None:
    cands = [
        r
        for r in responses
        if r.get("agent_start_ms") is not None and abs(r["agent_start_ms"] - start_ms) <= 1500
    ]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(r["agent_start_ms"] - start_ms))["latency_ms"]


def _ms(v: Any) -> int | None:
    return int(round(float(v) * 1000)) if v is not None else None


def _p50_p95(vals: list[int]) -> dict[str, int | None]:
    if not vals:
        return {"p50": None, "p95": None, "n": 0}
    s = sorted(vals)
    return {
        "p50": s[len(s) // 2],
        "p95": s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))],
        "n": len(s),
    }
