"""Voice probe: a scripted participant that proves the agent works over real audio.

It creates a room, dispatches the agent into it, joins as "caller", waits for the greeting,
plays each scripted line through Deepgram TTS as its microphone, records everything the
agent says, and writes a stereo WAV (left = caller, right = agent) plus a small JSON summary.
No LLM on the caller side, so the result is deterministic apart from the agent itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import numpy as np
from livekit import api, rtc

from gf.caller.audio import SAMPLE_RATE, deepgram_tts, rms_db, speech_segments, write_stereo_wav
from gf.config import settings

log = logging.getLogger("gf.probe")
FRAME_MS = 20


class Recorder:
    """Accumulates one channel of audio with a wall-clock origin so the two sides align."""

    def __init__(self, t0: float):
        self.t0 = t0
        self.chunks: list[np.ndarray] = []
        self.positions: list[int] = []  # sample offset where each chunk starts

    def push(self, samples: np.ndarray, at: float | None = None) -> None:
        at = time.time() if at is None else at
        self.positions.append(int((at - self.t0) * SAMPLE_RATE))
        self.chunks.append(samples)

    def render(self) -> np.ndarray:
        if not self.chunks:
            return np.zeros(0, dtype=np.int16)
        end = max(p + len(c) for p, c in zip(self.positions, self.chunks, strict=True))
        out = np.zeros(end, dtype=np.int16)
        for p, c in zip(self.positions, self.chunks, strict=True):
            out[p : p + len(c)] = c
        return out


async def run_probe(
    room_name: str,
    lines: list[str],
    out_dir: Path,
    *,
    wait_greeting_s: float = 8.0,
    reply_silence_s: float = 1.5,
    reply_timeout_s: float = 20.0,
    call_id: str | None = None,
) -> dict:
    s = settings()
    call_id = call_id or room_name
    out_dir.mkdir(parents=True, exist_ok=True)
    lk = api.LiveKitAPI(s.livekit_url, s.livekit_api_key, s.livekit_api_secret)
    summary: dict = {"room": room_name, "call_id": call_id, "lines": [], "ok": False}
    try:
        await lk.room.create_room(api.CreateRoomRequest(name=room_name, empty_timeout=60))
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=s.agent_name,
                room=room_name,
                metadata=json.dumps({"call_id": call_id, "record_dir": str(out_dir)}),
            )
        )
        token = (
            api.AccessToken(s.livekit_api_key, s.livekit_api_secret)
            .with_identity("caller")
            .with_name("Probe caller")
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .to_jwt()
        )

        room = rtc.Room()
        t0 = time.time()
        caller_rec, agent_rec = Recorder(t0), Recorder(t0)
        agent_audio_evt = asyncio.Event()
        last_agent_audio = {"t": 0.0}

        async def read_agent(track: rtc.Track) -> None:
            stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
            async for ev in stream:
                frame = ev.frame
                samples = np.frombuffer(frame.data, dtype=np.int16)
                agent_rec.push(samples)
                if rms_db(samples) > -45:
                    last_agent_audio["t"] = time.time()
                    agent_audio_evt.set()

        @room.on("track_subscribed")
        def _on_track(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                log.info("subscribed to agent audio from %s", participant.identity)
                asyncio.ensure_future(read_agent(track))

        events: list[dict] = []

        @room.on("data_received")
        def _on_data(packet: rtc.DataPacket):
            if packet.topic == "gf.events":
                try:
                    events.append(json.loads(packet.data))
                except ValueError:
                    pass

        await room.connect(s.livekit_url, token)
        source = rtc.AudioSource(SAMPLE_RATE, 1)
        track = rtc.LocalAudioTrack.create_audio_track("mic", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        async def play(samples: np.ndarray) -> None:
            n = int(SAMPLE_RATE * FRAME_MS / 1000)
            started = time.time()
            caller_rec.push(samples, at=started)
            for i in range(0, len(samples), n):
                chunk = samples[i : i + n]
                if len(chunk) < n:
                    chunk = np.concatenate([chunk, np.zeros(n - len(chunk), dtype=np.int16)])
                frame = rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, n)
                await source.capture_frame(frame)

        async def wait_for_reply(timeout: float) -> bool:
            """Wait until the agent has spoken and then gone quiet for reply_silence_s."""
            agent_audio_evt.clear()
            try:
                await asyncio.wait_for(agent_audio_evt.wait(), timeout)
            except TimeoutError:
                return False
            while time.time() - last_agent_audio["t"] < reply_silence_s:
                await asyncio.sleep(0.1)
            return True

        greeted = await wait_for_reply(wait_greeting_s)
        summary["greeted_first"] = greeted
        summary["greeting_latency_s"] = round(last_agent_audio["t"] - t0, 2) if greeted else None

        for text in lines:
            pcm = await deepgram_tts(text)
            t_start = time.time()
            await play(pcm)
            t_end = time.time()
            replied = await wait_for_reply(reply_timeout_s)
            summary["lines"].append(
                {
                    "text": text,
                    "spoken_s": round(t_end - t_start, 2),
                    "agent_replied": replied,
                    "reply_latency_s": round(_first_audio_after(agent_rec, t_end) - t_end, 2)
                    if replied
                    else None,
                }
            )
        await asyncio.sleep(0.5)
        await room.disconnect()

        left, right = caller_rec.render(), agent_rec.render()
        write_stereo_wav(out_dir / "probe_audio.wav", left, right)
        summary["agent_speech_segments"] = speech_segments(right)
        summary["events"] = len(events)
        summary["tool_calls"] = [e for e in events if e.get("kind") == "tool_call"]
        summary["ok"] = greeted and all(ln["agent_replied"] for ln in summary["lines"])
        (out_dir / "probe_summary.json").write_text(json.dumps(summary, indent=2))
        return summary
    finally:
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:  # noqa: BLE001
            pass
        await lk.aclose()


def _first_audio_after(rec: Recorder, t: float) -> float:
    """Wall-clock time of the first agent chunk with energy after time t."""
    offset = int((t - rec.t0) * SAMPLE_RATE)
    for p, c in zip(rec.positions, rec.chunks, strict=True):
        if p >= offset and rms_db(c) > -45:
            return rec.t0 + p / SAMPLE_RATE
    return t


def main(room: str, lines: list[str], out: Path) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    summary = asyncio.run(run_probe(room, lines, out))
    print(json.dumps({k: v for k, v in summary.items() if k != "tool_calls"}, indent=2))
    print("tool calls:", [(t["name"], t.get("is_error")) for t in summary["tool_calls"]])
    return 0 if summary["ok"] else 1
