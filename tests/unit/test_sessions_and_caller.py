"""Session schema, immutability, audio conditions, recorder placement, caller prompt."""

from pathlib import Path

import numpy as np
import pytest
import yaml

from gf.caller.conditions import ConditionChain, parse_noise
from gf.caller.recorder import SAMPLE_RATE, Channel
from gf.caller.simulator import build_prompt
from gf.config import ROOT
from gf.sessions.schema import Conditions, Session, load_all

SESSIONS = ROOT / "sessions"


# ---- T4.1 sessions ----


def test_all_bundled_sessions_load_and_have_hash_ids():
    sessions = load_all(SESSIONS)
    assert len(sessions) >= 3
    for s in sessions:
        assert len(s.id) == 12 and s.caller.goal and s.expected.outcome


def test_same_content_same_id_and_tampering_is_refused(tmp_path: Path):
    src = (SESSIONS / "refund-basic.yaml").read_text()
    a = tmp_path / "a.yaml"
    a.write_text(src)
    s1 = Session.load(a)
    b = tmp_path / "b.yaml"
    b.write_text(s1.dump())  # dump writes the id in
    s2 = Session.load(b)
    assert s1.id == s2.id
    tampered = b.read_text().replace("amount: 89.99", "amount: 10.0", 1)
    (tmp_path / "c.yaml").write_text(tampered)
    with pytest.raises(ValueError, match="immutable"):
        Session.load(tmp_path / "c.yaml")


def test_unknown_tool_in_expected_is_rejected(tmp_path: Path):
    raw = yaml.safe_load((SESSIONS / "refund-basic.yaml").read_text())
    raw["expected"]["tool_calls"]["forbidden"] = ["cancel_order"]
    p = tmp_path / "x.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="unknown tools"):
        Session.load(p)


def test_missing_fixture_is_rejected(tmp_path: Path):
    raw = yaml.safe_load((SESSIONS / "refund-basic.yaml").read_text())
    raw["fixtures"] = "nope"
    p = tmp_path / "x.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match="fixture"):
        Session.load(p)


# ---- T3.4 conditions ----


def _speech(seconds=2.0, fs=16000):
    t = np.arange(int(fs * seconds)) / fs
    sig = 0.3 * np.sin(2 * np.pi * 220 * t) + 0.15 * np.sin(2 * np.pi * 1200 * t)
    return (sig * 32767).astype(np.int16)


def _frames(pcm, n=320):
    return [pcm[i : i + n] for i in range(0, len(pcm) - n + 1, n)]


def test_parse_noise():
    assert parse_noise("cafe@15dB") == ("cafe", 15.0)
    assert parse_noise(None) is None
    with pytest.raises(ValueError):
        parse_noise("loud")


def test_noise_hits_target_snr_within_1db():
    chain = ConditionChain(Conditions(noise="cafe@15dB"), 16000, seed=1)
    clean = _speech()
    out = np.concatenate([chain.process(f) for f in _frames(clean)])
    noise = out[: len(clean)].astype(np.float64) - clean[: len(out)].astype(np.float64)
    # skip the first 0.3 s while the level tracker settles
    k = 16000 * 3 // 10
    snr = 20 * np.log10(
        np.sqrt(np.mean(clean[k:].astype(float) ** 2)) / np.sqrt(np.mean(noise[k:] ** 2))
    )
    assert abs(snr - 15.0) < 1.0, snr


def test_packet_loss_extremes():
    frames = _frames(_speech())
    none_lost = ConditionChain(Conditions(packet_loss=0.0), 16000)
    all_lost = ConditionChain(Conditions(packet_loss=1.0), 16000)
    assert all(none_lost.process(f) is not None for f in frames)
    assert all(all_lost.process(f) is None for f in frames)
    assert all_lost.describe()["dropped_frames"] == len(frames)


def test_phone_line_removes_energy_above_4khz():
    fs = 16000
    t = np.arange(fs) / fs
    hi = (0.5 * np.sin(2 * np.pi * 6000 * t) * 32767).astype(np.int16)
    lo = (0.5 * np.sin(2 * np.pi * 1000 * t) * 32767).astype(np.int16)
    chain = ConditionChain(Conditions(phone_line=True), fs)
    out_hi = np.concatenate([chain.process(f) for f in _frames(hi)])
    chain = ConditionChain(Conditions(phone_line=True), fs)
    out_lo = np.concatenate([chain.process(f) for f in _frames(lo)])
    rms = lambda x: np.sqrt(np.mean(x.astype(float) ** 2))  # noqa: E731
    assert rms(out_hi[4000:]) < 0.15 * rms(hi), "6 kHz tone should be attenuated hard"
    assert rms(out_lo[4000:]) > 0.6 * rms(lo), "1 kHz tone should pass"


# ---- T4.3 recorder placement ----


def test_channel_places_gaps_as_silence():
    ch = Channel()
    one = np.ones(160, dtype=np.int16)
    ch.place(0, one)
    ch.place(160, one)  # contiguous
    ch.place(160 * 40, one)  # a 380 ms gap, beyond the 100 ms tolerance: silence opens
    out = ch.render(160 * 41)
    assert out[:320].all() and not out[320 : 160 * 40].any() and out[160 * 40 :].all()
    assert len(ch.segments) == 2


def test_recorder_sample_rate_is_16k():
    assert SAMPLE_RATE == 16000


# ---- T3.1 prompt ----


def test_prompt_contains_facts_goal_and_rules():
    s = Session.load(SESSIONS / "refund-noisy-cafe.yaml")
    p = build_prompt(s.caller)
    assert "GW-48213" in p and "94110" in p and "Maria Lopez" in p
    assert "end_call" in p and "Never mention that you are an AI" in p
    assert "at most 12" in p
