"""Audio helpers shared by the probe and the simulated caller: Deepgram TTS, WAV writing,
simple energy detection. Everything is 16 kHz mono int16 unless stated."""

from __future__ import annotations

from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from gf.config import settings

SAMPLE_RATE = 16_000


async def deepgram_tts(
    text: str, model: str = "aura-2-thalia-en", sample_rate: int = SAMPLE_RATE
) -> np.ndarray:
    """Synthesise `text` to int16 PCM at `sample_rate` via Deepgram's REST API."""
    params = {
        "model": model,
        "encoding": "linear16",
        "sample_rate": str(sample_rate),
        "container": "none",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.deepgram.com/v1/speak",
            params=params,
            headers={"Authorization": f"Token {settings().deepgram_api_key}"},
            json={"text": text},
        )
    r.raise_for_status()
    return np.frombuffer(r.content, dtype=np.int16)


def write_stereo_wav(
    path: Path, left: np.ndarray, right: np.ndarray, sample_rate: int = SAMPLE_RATE
) -> None:
    """Left = caller as sent, right = agent as heard. Pads the shorter channel with silence."""
    n = max(len(left), len(right))
    stereo = np.zeros((n, 2), dtype=np.int16)
    stereo[: len(left), 0] = left
    stereo[: len(right), 1] = right
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, stereo, sample_rate, subtype="PCM_16")


def rms_db(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return -120.0
    x = samples.astype(np.float32) / 32768.0
    rms = float(np.sqrt(np.mean(x * x))) + 1e-9
    return 20 * np.log10(rms)


def speech_segments(
    samples: np.ndarray,
    sample_rate: int = SAMPLE_RATE,
    frame_ms: int = 20,
    threshold_db: float = -45.0,
    min_ms: int = 200,
) -> list[tuple[float, float]]:
    """Energy-based speech segments as (start_s, end_s). Good enough for the probe; the
    timeline builder uses a proper VAD."""
    frame = int(sample_rate * frame_ms / 1000)
    n = len(samples) // frame
    active = [rms_db(samples[i * frame : (i + 1) * frame]) > threshold_db for i in range(n)]
    segments: list[tuple[float, float]] = []
    start = None
    for i, on in enumerate(active + [False]):
        if on and start is None:
            start = i
        elif not on and start is not None:
            if (i - start) * frame_ms >= min_ms:
                segments.append((start * frame_ms / 1000, i * frame_ms / 1000))
            start = None
    return segments
