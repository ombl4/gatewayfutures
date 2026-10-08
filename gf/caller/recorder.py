"""Stereo call recorder: a hidden participant subscribes to both audio tracks and places
every frame by stream time against one origin. Left = caller as sent, right = agent as heard.

Adapted from the voice test recorder in meetocean-ai/assistant (tests/voice/audio.py):
frames arrive in real time; a frame that lands later than where its channel ends opens a
silence gap of the right length, so timings stay stream-relative even with packet loss.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import numpy as np
import soundfile as sf
from livekit import api, rtc

from gf.config import settings

log = logging.getLogger("gf.recorder")
SAMPLE_RATE = 16_000
GAP_TOLERANCE_S = 0.1
RECORDER_IDENTITY = "gf-recorder"


class Channel:
    def __init__(self) -> None:
        self.segments: list[list] = []  # [offset_samples, np.ndarray]

    @property
    def end(self) -> int:
        if not self.segments:
            return 0
        off, pcm = self.segments[-1]
        return off + len(pcm)

    def place(self, offset: int, pcm: np.ndarray) -> None:
        offset = max(offset, 0)
        contiguous = offset <= self.end + int(GAP_TOLERANCE_S * SAMPLE_RATE)
        if self.segments and contiguous:
            self.segments[-1][1] = np.concatenate([self.segments[-1][1], pcm])
        else:
            self.segments.append([offset, pcm])

    def render(self, length: int) -> np.ndarray:
        out = np.zeros(length, dtype=np.int32)
        for off, pcm in self.segments:
            end = min(off + len(pcm), length)
            if end > off:
                out[off:end] += pcm[: end - off]
        return np.clip(out, -32768, 32767).astype(np.int16)


class CallRecorder:
    """Joins `room_name` hidden and records `caller_identity` left, agent participants right."""

    def __init__(self, room_name: str, caller_identity: str):
        self.room_name = room_name
        self.caller_identity = caller_identity
        self.t0 = time.time()
        self.caller = Channel()
        self.agent = Channel()
        self._tasks: list[asyncio.Task] = []
        self._room: rtc.Room | None = None
        self.track_first_seen: dict[str, float] = {}

    def _offset(self, t: float) -> int:
        return int(round((t - self.t0) * SAMPLE_RATE))

    async def _pump(self, track: rtc.Track, channel: Channel, who: str) -> None:
        stream = rtc.AudioStream.from_track(track=track, sample_rate=SAMPLE_RATE, num_channels=1)
        try:
            async for ev in stream:
                now = time.time()
                pcm = np.frombuffer(ev.frame.data, dtype=np.int16)
                if who not in self.track_first_seen:
                    self.track_first_seen[who] = now - self.t0
                channel.place(self._offset(now) - len(pcm), pcm)
        except asyncio.CancelledError:
            pass
        except Exception as e:  # noqa: BLE001 - recording is best effort
            log.warning("recording %s stopped: %s", who, e)
        finally:
            await stream.aclose()

    async def start(self) -> None:
        s = settings()
        token = (
            api.AccessToken(s.livekit_api_key, s.livekit_api_secret)
            .with_identity(RECORDER_IDENTITY)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=self.room_name,
                    can_publish=False,
                    can_subscribe=True,
                    hidden=True,
                )
            )
            .to_jwt()
        )
        room = rtc.Room()

        @room.on("track_subscribed")
        def _on_track(track, publication, participant):
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            if participant.identity == self.caller_identity:
                channel, who = self.caller, "caller"
            else:
                channel, who = self.agent, "agent"
            log.info("recording %s (%s)", participant.identity, who)
            self._tasks.append(asyncio.create_task(self._pump(track, channel, who)))

        await room.connect(s.livekit_url, token)
        self._room = room

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        if self._room is not None:
            try:
                await self._room.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def write(self, path: Path) -> dict:
        n = max(self.caller.end, self.agent.end)
        stereo = np.stack([self.caller.render(n), self.agent.render(n)], axis=1)
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(path, stereo, SAMPLE_RATE, subtype="PCM_16")
        return {
            "path": str(path),
            "sample_rate": SAMPLE_RATE,
            "duration_s": round(n / SAMPLE_RATE, 2),
            "t0_epoch_ms": int(self.t0 * 1000),
            "channels": {"left": "caller_as_sent", "right": "agent_as_heard"},
            "track_first_seen_s": {k: round(v, 3) for k, v in self.track_first_seen.items()},
        }
