"""Spec T5.14: a second recogniser decides whether a mishearing is the simulator's or the
agent's."""

from __future__ import annotations

import json

from gf.record.model import AgentTurn, CallerTurn, CallRecord
from gf.scoring import second_opinion as so
from gf.scoring.hearing import check_hearing

SAID = ["Please confirm the new address: 12 Beacon Street, Boston, Massachusetts 02108."]
HEARD = ["Please confirm the new address. One two Eakin Street, Boston, Massachusetts 02108."]
ARGUED = ["No, it's Beacon Street, not Eakin Street. The rest is correct."]


def _record(folder: str, agent_said, caller_heard, caller_said) -> CallRecord:
    return CallRecord(
        folder=folder,
        call_id="c",
        session_id="s",
        attempt=1,
        meta={},
        t0_ms=0,
        audio_path=None,
        audio_duration_s=30.0,
        caller_turns=[
            CallerTurn(n=i + 1, text=t, t_start_ms=i * 10_000 + 5000, t_end_ms=i * 10_000 + 8000)
            for i, t in enumerate(caller_said)
        ],
        caller_heard=[{"t_ms": i * 10_000 + 1500, "text": t} for i, t in enumerate(caller_heard)],
        agent_turns=[
            AgentTurn(n=i + 1, text=t, t_ms=i * 10_000 + 1000) for i, t in enumerate(agent_said)
        ],
        agent_heard=[],
        agent_user_messages=[],
        tool_calls=[],
        backend_state={},
        caller_result={},
        agent_events=[],
        caller_events=[],
        ended_by="caller",
        end_reason="",
    )


def _cache(tmp_path, text):
    (tmp_path / so.CACHE).write_text(json.dumps({"1": {"text": text, "model": "nova-3"}}))


def test_second_opinion_hearing_the_agent_correctly_keeps_the_simulator_at_fault(tmp_path):
    _cache(
        tmp_path, "Please confirm the new address: 12 Beacon Street, Boston, Massachusetts 02108."
    )
    checks, reasons = check_hearing(_record(str(tmp_path), SAID, HEARD, ARGUED), {})
    by = {c.id: c for c in checks}
    assert len(reasons) == 1 and "second recogniser hears the agent correctly" in reasons[0]
    assert not by["speech.caller_hearing"].passed and by["speech.caller_hearing"].severity == "hard"
    assert by["speech.agent_intelligible"].passed
    m = by["speech.caller_hearing"].evidence["mishearings"][0]
    assert m["side"] == "simulator" and "Beacon" in m["second_opinion"]


def test_second_opinion_hearing_eakin_blames_the_agent(tmp_path):
    _cache(
        tmp_path, "Please confirm the new address. 12 Eakin Street, Boston, Massachusetts 02108."
    )
    checks, reasons = check_hearing(_record(str(tmp_path), SAID, HEARD, ARGUED), {})
    by = {c.id: c for c in checks}
    assert reasons == []  # the call is valid
    assert by["speech.caller_hearing"].passed
    ai = by["speech.agent_intelligible"]
    assert not ai.passed and ai.severity == "hard"
    assert "beacon" in ai.what_happened and "eakin" in ai.what_happened
    assert ai.evidence["mishearings"][0]["side"] == "agent" and ai.evidence["turn_ns"] == [1]


def test_no_second_opinion_stays_invalid_but_unverified(tmp_path, monkeypatch):
    monkeypatch.setenv("GF_SECOND_OPINION", "0")
    checks, reasons = check_hearing(_record(str(tmp_path), SAID, HEARD, ARGUED), {})
    by = {c.id: c for c in checks}
    assert len(reasons) == 1 and "not verified" in reasons[0]
    assert not by["speech.caller_hearing"].passed and by["speech.agent_intelligible"].passed
    assert by["speech.caller_hearing"].evidence["mishearings"][0]["side"] == "unverified"


def test_cache_is_used_before_the_network_and_written_after(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(so, "enabled", lambda: True)
    monkeypatch.setattr(so, "agent_audio", lambda *a, **k: b"RIFF")
    monkeypatch.setattr(so, "transcribe", lambda wav: calls.append(wav) or "12 Beacon Street")
    rec = _record(str(tmp_path), SAID, HEARD, ARGUED)
    assert so.second_opinion(rec, {}, 1, 1000, 1500) == "12 Beacon Street" and len(calls) == 1
    assert so.second_opinion(rec, {}, 1, 1000, 1500) == "12 Beacon Street" and len(calls) == 1
    assert json.loads((tmp_path / so.CACHE).read_text())["1"]["model"] == so.MODEL


def test_numbers_are_attributed_too(tmp_path):
    said = ["Your order G W 4 8 2 1 3 total is $89.99. Shall I refund it?"]
    heard = [
        "Your order GW four eight two one seven total is eighty nine ninety nine. Shall I refund it?"
    ]
    argued = ["No, it is not four eight two one seven."]
    _cache(tmp_path, "Your order GW 48217 total is $89.99. Shall I refund it?")
    checks, reasons = check_hearing(_record(str(tmp_path), said, heard, argued), {})
    by = {c.id: c for c in checks}
    assert reasons == [] and not by["speech.agent_intelligible"].passed
