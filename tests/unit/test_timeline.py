"""Timeline builder on synthetic audio with known gaps: latency, dead air, talk-over, barge-in."""

import json
from pathlib import Path

import numpy as np
import soundfile as sf

from gf.record.model import CallRecord
from gf.record.timeline import build_timeline, speech_segments

SR = 16000


def tone(ms: int, f=440.0, amp=0.3) -> np.ndarray:
    t = np.arange(int(SR * ms / 1000)) / SR
    return (amp * np.sin(2 * np.pi * f * t) * 32767).astype(np.int16)


def silence(ms: int) -> np.ndarray:
    return np.zeros(int(SR * ms / 1000), dtype=np.int16)


def make_folder(tmp_path: Path, left: np.ndarray, right: np.ndarray, extra_meta=None) -> Path:
    n = max(len(left), len(right))
    stereo = np.zeros((n, 2), dtype=np.int16)
    stereo[: len(left), 0] = left
    stereo[: len(right), 1] = right
    sf.write(tmp_path / "audio.wav", stereo, SR, subtype="PCM_16")
    (tmp_path / "audio.json").write_text(json.dumps({"t0_epoch_ms": 1000, "duration_s": n / SR}))
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "call_id": "c1",
                "session_id": "s1",
                "attempt": 1,
                "ended_by": "caller",
                **(extra_meta or {}),
            }
        )
    )
    (tmp_path / "backend_log.json").write_text("[]")
    return tmp_path


def test_speech_segments_merge_short_pauses_and_drop_blips():
    pcm = np.concatenate(
        [tone(500), silence(100), tone(500), silence(1000), tone(100), silence(500)]
    )
    segs = speech_segments(pcm, SR)
    assert len(segs) == 1 and abs(segs[0][0] - 0) <= 20 and abs(segs[0][1] - 1100) <= 40


def test_latency_dead_air_and_greeting(tmp_path: Path):
    # agent greets at 0-1000; caller speaks 2000-3000; agent replies at 4500 (latency 1500);
    # then nobody speaks for 4000 ms (dead air); caller speaks 9500-10000; no reply.
    right = np.concatenate([tone(1000), silence(3500), tone(1000, 660), silence(10000)])
    left = np.concatenate(
        [silence(2000), tone(1000, 330), silence(6500), tone(500, 330), silence(5000)]
    )
    rec = CallRecord.load(make_folder(tmp_path, left, right))
    tl = build_timeline(rec)
    ux = tl["ux"]
    assert ux["greeting_first"] is True and abs(ux["first_agent_audio_ms"] - 0) <= 20
    assert ux["caller_segments"] == 2 and ux["agent_segments"] == 2
    r0 = ux["responses"][0]
    assert abs(r0["latency_ms"] - 1500) <= 50, r0
    assert ux["responses"][1]["latency_ms"] is None  # never answered
    assert len(ux["dead_air"]) == 1 and abs(ux["dead_air"][0]["gap_ms"] - 4000) <= 50
    assert ux["latency_ms"]["p50"] == r0["latency_ms"]


def test_talk_over_and_barge_in(tmp_path: Path):
    # caller speaks 0-2000; agent starts at 1000 while caller still talking (talk-over);
    # agent speaks 1000-4000; caller barges in at 3000; agent stops at 4000 (1000 ms later)
    left = np.concatenate([tone(2000, 330), silence(1000), tone(1000, 330), silence(1000)])
    right = np.concatenate([silence(1000), tone(3000, 660), silence(1000)])
    rec = CallRecord.load(make_folder(tmp_path, left, right))
    ux = build_timeline(rec)["ux"]
    assert len(ux["talk_over"]) == 1 and abs(ux["talk_over"][0]["agent_start_ms"] - 1000) <= 40
    assert len(ux["barge_in"]) == 1 and abs(ux["barge_in"][0]["agent_stop_after_ms"] - 1000) <= 60
    assert ux["greeting_first"] is False


def test_tool_calls_land_in_timeline_relative_to_t0(tmp_path: Path):
    folder = make_folder(tmp_path, tone(500), tone(500))
    (folder / "backend_log.json").write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "tool": "lookup_order",
                    "args": {},
                    "response": {},
                    "status": 200,
                    "ok": True,
                    "ts_ms": 1250,
                    "duration_ms": 5,
                }
            ]
        )
    )
    rec = CallRecord.load(folder)
    assert rec.tool_calls[0].t_ms == 250
    ev = [e for e in build_timeline(rec)["events"] if e["kind"] == "tool_call"]
    assert ev and ev[0]["t_ms"] == 250 and ev[0]["tool"] == "lookup_order"
