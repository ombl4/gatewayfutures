"""Second opinion on a mishearing (spec T5.14): what does an independent recogniser hear in
the agent's audio for the turn the simulated caller misheard?

The answer decides whose fault the mishearing is. It is cached in the call folder as
`second_opinion.json` (turn number → transcript) so scoring a run again never calls the
network and gives the same answer. `GF_SECOND_OPINION=0` disables it.
"""

from __future__ import annotations

import io
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gf.config import ROOT, settings
from gf.record.model import CallRecord

log = logging.getLogger("gf.scoring")
CACHE = "second_opinion.json"
MODEL = "nova-3"


def enabled() -> bool:
    return os.environ.get("GF_SECOND_OPINION", "1") != "0" and bool(settings().deepgram_api_key)


def agent_audio(
    record: CallRecord, timeline: dict[str, Any], start_ms: int, until_ms: int
) -> bytes | None:
    """WAV bytes of the agent channel for one turn: its speech segments between the turn's
    start and the moment the caller's transcript of it was final."""
    if not record.audio_path:
        return None
    try:
        import numpy as np
        import soundfile as sf
    except ImportError:  # pragma: no cover
        return None
    path = Path(record.audio_path)
    if not path.is_absolute():
        for cand in (ROOT / path, Path(record.folder) / path.name, path):
            if cand.exists():
                path = cand
                break
    if not path.exists():
        return None
    segs = [
        (int(e["t_ms"]), int(e["end_ms"]))
        for e in timeline.get("events") or []
        if e.get("kind") == "agent_speech"
        and int(e.get("end_ms") or 0) > start_ms - 500
        and int(e.get("t_ms") or 0) < until_ms + 600
    ]
    t0 = (min(s for s, _ in segs) if segs else start_ms) - 300
    t1 = (max(e for _, e in segs) if segs else until_ms) + 300
    data, sr = sf.read(str(path), dtype="int16")
    if data.ndim == 1:
        return None
    seg = data[max(0, int(t0 / 1000 * sr)) : max(0, int(t1 / 1000 * sr)), 1]
    if len(seg) < sr // 4 or int(np.abs(seg).max()) == 0:
        return None
    buf = io.BytesIO()
    sf.write(buf, seg, sr, format="WAV")
    return buf.getvalue()


def transcribe(wav: bytes) -> str | None:
    """Deepgram prerecorded, no keyterms, so it is independent of both sides' recognisers."""
    import httpx

    key = settings().deepgram_api_key
    try:
        r = httpx.post(
            f"https://api.deepgram.com/v1/listen?model={MODEL}&smart_format=true",
            headers={"Authorization": f"Token {key}", "Content-Type": "audio/wav"},
            content=wav,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    except Exception as e:  # noqa: BLE001
        log.warning("second opinion failed: %s", e)
        return None


def _cache_path(record: CallRecord) -> Path:
    return Path(record.folder) / CACHE


def load_cache(record: CallRecord) -> dict[str, Any]:
    p = _cache_path(record)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except ValueError:
            return {}
    return {}


def second_opinion(
    record: CallRecord, timeline: dict[str, Any], turn: int, start_ms: int, until_ms: int
) -> str | None:
    """The independent transcript of the agent's audio for `turn`, from the cache or the
    recogniser; None when there is no way to get one."""
    cache = load_cache(record)
    hit = cache.get(str(turn))
    if hit is not None:
        return hit.get("text")
    if not enabled():
        return None
    wav = agent_audio(record, timeline, start_ms, until_ms)
    if wav is None:
        return None
    text = transcribe(wav)
    if text is None:
        return None
    cache[str(turn)] = {
        "text": text,
        "model": MODEL,
        "start_ms": start_ms,
        "until_ms": until_ms,
        "at": datetime.now(UTC).isoformat(),
    }
    try:
        _cache_path(record).write_text(json.dumps(cache, indent=2))
    except OSError:  # pragma: no cover
        pass
    return text
