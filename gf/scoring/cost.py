"""Cost and usage per call (spec T5.10), from the providers' own usage events.

The agent side comes from `agent_session_report.json` (`usage`: OpenAI tokens incl. cached,
Deepgram TTS characters, Deepgram STT audio seconds), with the per-turn metric events as the
fallback. The simulated caller has no session report, so its tokens and characters are summed
from its metric events and its STT time is the call's audio length (streaming STT bills every
second the stream is open). Prices come from `pricing.yaml`; a model with no price is listed as
unpriced rather than silently counted as free.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from gf.config import ROOT
from gf.record.model import CallRecord

PRICING_PATH = ROOT / "pricing.yaml"


@lru_cache
def pricing(path: Path | None = None) -> dict[str, Any]:
    path = path or PRICING_PATH
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def _price(kind: str, model: str | None) -> dict[str, float] | None:
    """Exact model match, then prefix match ("aura-2-luna-en" -> "aura-2")."""
    table = pricing().get(kind) or {}
    if not model:
        return None
    if model in table:
        return table[model]
    for key, val in table.items():
        if model.startswith(key):
            return val
    return None


def _empty() -> dict[str, Any]:
    return {
        "llm_model": None,
        "llm_in": 0,
        "llm_cached": 0,
        "llm_out": 0,
        "tts_model": None,
        "tts_chars": 0,
        "tts_audio_s": 0.0,
        "stt_model": None,
        "stt_s": 0.0,
    }


def _from_metrics(events: list[dict[str, Any]], side: dict[str, Any]) -> None:
    for e in events:
        if e.get("kind") != "metrics" or e.get("cancelled"):
            continue
        mt = e.get("metric_type")
        if mt == "llm_metrics":
            side["llm_in"] += int(e.get("prompt_tokens") or 0)
            side["llm_out"] += int(e.get("completion_tokens") or 0)
        elif mt == "tts_metrics":
            side["tts_chars"] += int(e.get("characters_count") or 0)
            side["tts_audio_s"] += float(e.get("audio_duration") or 0.0)


def usage_of(record: CallRecord) -> dict[str, Any]:
    """{"agent": {...}, "caller": {...}} raw usage, before pricing."""
    folder = Path(record.folder)
    meta = record.meta
    models = meta.get("agent_models") or {}
    agent = _empty() | {
        "llm_model": (models.get("llm") or {}).get("model"),
        "tts_model": (models.get("tts") or {}).get("model"),
        "stt_model": (models.get("stt") or {}).get("model"),
    }
    report_path = folder / "agent_session_report.json"
    usage: list[dict[str, Any]] = []
    if report_path.exists() and report_path.read_text().strip():
        try:
            usage = json.loads(report_path.read_text()).get("usage") or []
        except ValueError:
            usage = []
    if usage:
        for u in usage:
            provider = str(u.get("provider", "")).lower()
            if "openai" in provider or u.get("input_tokens") is not None:
                agent["llm_model"] = u.get("model") or agent["llm_model"]
                agent["llm_in"] += int(u.get("input_tokens") or 0)
                agent["llm_cached"] += int(u.get("input_cached_tokens") or 0)
                agent["llm_out"] += int(u.get("output_tokens") or 0)
            elif u.get("characters_count") is not None:
                agent["tts_model"] = u.get("model") or agent["tts_model"]
                agent["tts_chars"] += int(u.get("characters_count") or 0)
                agent["tts_audio_s"] += float(u.get("audio_duration") or 0.0)
            elif u.get("audio_duration") is not None:
                agent["stt_model"] = u.get("model") or agent["stt_model"]
                agent["stt_s"] += float(u.get("audio_duration") or 0.0)
        agent["source"] = "agent_session_report.usage"
    else:
        _from_metrics(record.agent_events, agent)
        agent["stt_s"] = float(record.audio_duration_s or 0.0)
        agent["source"] = "agent metric events"

    caller_spec = meta.get("caller") or {}
    caller = _empty() | {
        "llm_model": (caller_spec.get("llm") or {}).get("model"),
        "tts_model": caller_spec.get("voice"),
        "stt_model": "nova-3",
        "source": "caller metric events",
    }
    _from_metrics(record.caller_events, caller)
    caller["stt_s"] = float(record.audio_duration_s or 0.0)
    return {"agent": agent, "caller": caller}


def _price_side(side: dict[str, Any], unpriced: list[str]) -> float:
    usd = 0.0
    llm = _price("llm", side["llm_model"])
    if llm:
        uncached = max(0, side["llm_in"] - side["llm_cached"])
        usd += uncached / 1e6 * llm.get("input_per_m", 0.0)
        usd += side["llm_cached"] / 1e6 * llm.get("cached_input_per_m", llm.get("input_per_m", 0.0))
        usd += side["llm_out"] / 1e6 * llm.get("output_per_m", 0.0)
    elif side["llm_in"] or side["llm_out"]:
        unpriced.append(f"llm {side['llm_model']}")
    tts = _price("tts", side["tts_model"])
    if tts:
        usd += side["tts_chars"] / 1000 * tts.get("per_1k_chars", 0.0)
    elif side["tts_chars"]:
        unpriced.append(f"tts {side['tts_model']}")
    stt = _price("stt", side["stt_model"])
    if stt:
        usd += side["stt_s"] / 60 * stt.get("per_minute", 0.0)
    elif side["stt_s"]:
        unpriced.append(f"stt {side['stt_model']}")
    side["usd"] = round(usd, 4)
    return usd


def call_cost(record: CallRecord) -> dict[str, Any]:
    """Priced usage for one call. Stored in scores.json as `cost`."""
    u = usage_of(record)
    unpriced: list[str] = []
    total = _price_side(u["agent"], unpriced) + _price_side(u["caller"], unpriced)
    return {
        "usd": round(total, 4),
        "agent": u["agent"],
        "caller": u["caller"],
        "unpriced": sorted(set(unpriced)),
        "not_metered": ["LiveKit Cloud minutes", "persona judge"],
        "pricing": PRICING_PATH.name,
    }


def estimate_cost(folder: str | Path) -> float:
    """Cost of a finished call from its record folder (used by the runner's budget)."""
    try:
        return float(call_cost(CallRecord.load(folder))["usd"])
    except Exception:  # noqa: BLE001 - a missing record must not stop the run
        return 0.0


def run_cost(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals for summary.json from per-attempt `cost` blocks."""
    priced = [a["cost"] for a in attempts if isinstance(a.get("cost"), dict)]
    usd = sum(c["usd"] for c in priced)
    return {
        "usd": round(usd, 2),
        "calls_priced": len(priced),
        "per_call_usd": round(usd / len(priced), 4) if priced else None,
        "agent_usd": round(sum(c["agent"]["usd"] for c in priced), 2),
        "caller_usd": round(sum(c["caller"]["usd"] for c in priced), 2),
        "llm_tokens": sum(
            c["agent"]["llm_in"]
            + c["agent"]["llm_out"]
            + c["caller"]["llm_in"]
            + c["caller"]["llm_out"]
            for c in priced
        ),
        "audio_minutes": round(sum(c["agent"]["stt_s"] for c in priced) / 60, 1),
        "unpriced": sorted({u for c in priced for u in c["unpriced"]}),
    }
