"""Audio conditions applied to the caller's outgoing audio, frame by frame, before it is
published. Every parameter is explicit so a call can be reproduced.

- noise "cafe@15dB": synthetic pink/babble-like noise mixed at a target SNR (dB) measured
  against the running speech level.
- phone_line: 300-3400 Hz band-limit (a cheap biquad pair), the sound of an 8 kHz line.
- packet_loss: a seeded Bernoulli drop of whole 20 ms frames (replaced by silence).
- low_quality_mic: mild clipping + high-pass, a laptop/handset microphone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from gf.sessions.schema import Conditions

NOISE_RE = re.compile(r"^(?P<kind>[a-z_]+)@(?P<snr>-?\d+(\.\d+)?)dB$", re.I)


def parse_noise(spec: str | None) -> tuple[str, float] | None:
    if not spec:
        return None
    m = NOISE_RE.match(spec.strip())
    if not m:
        raise ValueError(f"noise must look like 'cafe@15dB', got {spec!r}")
    return m.group("kind").lower(), float(m.group("snr"))


class _Biquad:
    """Direct-form-I biquad, state kept across frames."""

    def __init__(self, b: tuple[float, float, float], a: tuple[float, float, float]):
        self.b, self.a = b, a
        self.x1 = self.x2 = self.y1 = self.y2 = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        b0, b1, b2 = self.b
        _, a1, a2 = self.a
        y = np.empty_like(x)
        x1, x2, y1, y2 = self.x1, self.x2, self.y1, self.y2
        for i, xi in enumerate(x):
            yi = b0 * xi + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, xi, y1, yi
            y[i] = yi
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        return y


def _butter2(kind: str, fc: float, fs: float) -> _Biquad:
    """Second-order Butterworth low/high-pass coefficients (RBJ cookbook)."""
    w0 = 2 * np.pi * fc / fs
    q = 1 / np.sqrt(2)  # Butterworth
    alpha = np.sin(w0) / (2 * q)
    cos = np.cos(w0)
    if kind == "low":
        b = ((1 - cos) / 2, 1 - cos, (1 - cos) / 2)
    else:
        b = ((1 + cos) / 2, -(1 + cos), (1 + cos) / 2)
    a = (1 + alpha, -2 * cos, 1 - alpha)
    return _Biquad(tuple(x / a[0] for x in b), tuple(x / a[0] for x in a))


@dataclass
class ConditionStats:
    frames: int = 0
    dropped: int = 0
    noise_rms: float = 0.0
    speech_rms: float = 0.0


class ConditionChain:
    def __init__(self, cond: Conditions, sample_rate: int, seed: int = 0):
        self.cond = cond
        self.fs = sample_rate
        self.rng = np.random.default_rng(seed)
        self.noise = parse_noise(cond.noise)
        self.stats = ConditionStats()
        self._speech_level = 0.05  # running RMS estimate (float scale), seeded sensibly
        self._pink_state = np.zeros(7)
        self._filters: list[_Biquad] = []
        if cond.phone_line:
            self._filters += [
                _butter2("high", 300, sample_rate),
                _butter2("low", 3400, sample_rate),
            ]
        if cond.low_quality_mic:
            self._filters.append(_butter2("high", 180, sample_rate))

    # ---- noise generation ----
    def _pink(self, n: int) -> np.ndarray:
        """Pink noise via the Voss-McCartney-ish filter (Paul Kellet's refinement)."""
        white = self.rng.standard_normal(n)
        b = self._pink_state
        out = np.empty(n)
        for i, w in enumerate(white):
            b[0] = 0.99886 * b[0] + w * 0.0555179
            b[1] = 0.99332 * b[1] + w * 0.0750759
            b[2] = 0.96900 * b[2] + w * 0.1538520
            b[3] = 0.86650 * b[3] + w * 0.3104856
            b[4] = 0.55000 * b[4] + w * 0.5329522
            b[5] = -0.7616 * b[5] - w * 0.0168980
            out[i] = (b[0] + b[1] + b[2] + b[3] + b[4] + b[5] + b[6] + w * 0.5362) * 0.11
            b[6] = w * 0.115926
        return out

    def _noise_frame(self, n: int) -> np.ndarray:
        kind, _ = self.noise  # type: ignore[misc]
        base = self._pink(n)
        if kind in ("cafe", "babble"):
            # a slow amplitude wobble makes it sound like a room, not a hiss
            t = np.arange(n) / self.fs
            base *= 1 + 0.4 * np.sin(2 * np.pi * 0.7 * t + self.rng.uniform(0, 6.28))
        elif kind == "street":
            base = 0.7 * base + 0.3 * self.rng.standard_normal(n) * 0.1
        return base

    # ---- the chain ----
    def process(self, pcm: np.ndarray) -> np.ndarray | None:
        """int16 mono frame in, int16 mono frame out; None means the frame was dropped."""
        self.stats.frames += 1
        if self.cond.packet_loss > 0 and self.rng.random() < self.cond.packet_loss:
            self.stats.dropped += 1
            return None
        x = pcm.astype(np.float32) / 32768.0
        for f in self._filters:
            x = f.process(x).astype(np.float32)
        if self.cond.low_quality_mic:
            x = np.clip(x * 1.6, -0.6, 0.6)
        if self.noise is not None:
            frame_rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
            if frame_rms > 0.01:  # only track level on speech frames
                self._speech_level = 0.9 * self._speech_level + 0.1 * frame_rms
            _, snr_db = self.noise
            target_noise_rms = self._speech_level / (10 ** (snr_db / 20))
            noise = self._noise_frame(len(x))
            noise *= target_noise_rms / (float(np.sqrt(np.mean(noise * noise))) + 1e-9)
            x = x + noise.astype(np.float32)
            self.stats.noise_rms = target_noise_rms
            self.stats.speech_rms = self._speech_level
        return np.clip(x * 32768.0, -32768, 32767).astype(np.int16)

    def describe(self) -> dict:
        return {
            "noise": self.cond.noise,
            "phone_line": self.cond.phone_line,
            "packet_loss": self.cond.packet_loss,
            "low_quality_mic": self.cond.low_quality_mic,
            "frames": self.stats.frames,
            "dropped_frames": self.stats.dropped,
            "measured_snr_db": round(20 * np.log10(self.stats.speech_rms / self.stats.noise_rms), 1)
            if self.stats.noise_rms
            else None,
        }
