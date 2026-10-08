"""Spec T5.8: the simulated caller mishearing the agent is a simulator fault."""

from __future__ import annotations

from gf.record.model import AgentTurn, CallerTurn, CallRecord
from gf.scoring.hearing import check_hearing, entities_of, word_substitutions


def _record(agent_said: list[str], caller_heard: list[str], caller_said: list[str]) -> CallRecord:
    agent_turns = [
        AgentTurn(n=i + 1, text=t, t_ms=i * 10_000 + 1000) for i, t in enumerate(agent_said)
    ]
    heard = [{"t_ms": i * 10_000 + 1500, "text": t} for i, t in enumerate(caller_heard)]
    caller_turns = [
        CallerTurn(n=i + 1, text=t, t_start_ms=i * 10_000 + 5000, t_end_ms=i * 10_000 + 8000)
        for i, t in enumerate(caller_said)
    ]
    return CallRecord(
        folder="x",
        call_id="c",
        session_id="s",
        attempt=1,
        meta={},
        t0_ms=0,
        audio_path=None,
        audio_duration_s=30.0,
        caller_turns=caller_turns,
        caller_heard=heard,
        agent_turns=agent_turns,
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


def test_entities_and_substitutions():
    assert entities_of("Your order G W 4 8 2 1 3 total was $89.99, zip 94110.") == [
        "48213",
        "8999",
        "94110",
    ]
    assert word_substitutions("12 Beacon Street, Boston", "12 Beacon Street, Austin") == [
        ("boston", "austin")
    ]
    assert word_substitutions("the refund has been issued", "the refund has been issued") == []


def test_clean_hearing_is_valid():
    checks, reasons = check_hearing(
        _record(
            ["Your order G W 4 8 2 1 3 was delivered. The total was $89.99."],
            [
                "Your order g w four eight two one three was delivered. The total was eighty nine dollars and ninety nine cents."
            ],
            ["Yes, please refund the eighty-nine ninety-nine."],
        )
    )
    assert reasons == []
    from gf.scoring.hearing import WER_SOFT_FLAG

    assert checks[0].passed and checks[0].value is not None and checks[0].value < WER_SOFT_FLAG


def test_misheard_digit_not_acted_on_is_only_noted():
    checks, reasons = check_hearing(
        _record(
            ["Could you confirm the zip code 94110?"],
            ["Could you confirm the zip code 94118?"],
            ["Yes, the zip code is 9 4 1 1 0."],  # the caller used its own fact, not the mishearing
        )
    )
    assert reasons == []
    assert checks[0].evidence["mishearings"][0]["heard"] == "94118"
    assert checks[0].evidence["mishearings"][0]["acted_on"] is False


def test_misheard_city_acted_on_is_invalid():
    checks, reasons = check_hearing(
        _record(
            ["To confirm, the new address is 12 Beacon Street, Boston, Massachusetts 02108."],
            ["To confirm, the new address is 12 Beacon Street, Austin, Massachusetts 02108."],
            [
                "No, the city is Boston, not Austin. The correct address is 12 Beacon Street, Boston."
            ],
        )
    )
    assert len(reasons) == 1 and "austin" in reasons[0] and "boston" in reasons[0]
    assert not checks[0].passed and checks[0].severity == "hard"


def test_scoring_marks_such_a_call_invalid(tmp_path):
    """End to end through score_attempt on the real 'Austin/Boston' pattern in a synthetic folder."""
    import json
    import shutil
    from pathlib import Path

    from gf.scoring.score import score_attempt
    from gf.sessions.schema import Session

    root = Path(__file__).resolve().parents[2]
    folder = tmp_path / "rec"
    shutil.copytree(root / "fixtures" / "records" / "refund-basic", folder)
    caller = json.loads((folder / "caller.json").read_text())
    audio = json.loads((folder / "audio.json").read_text())
    t0 = audio["t0_epoch_ms"]
    # the caller mishears the agent's read-back and argues with the wrong value
    events = [
        json.loads(line)
        for line in (folder / "agent_events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    agent_msgs = [e for e in events if e.get("kind") == "message" and e.get("role") == "assistant"]
    target = agent_msgs[2]
    target["text"] = "Your order G W 4 8 2 1 3 ships to Valencia Street in Boston. Do you confirm?"
    (folder / "agent_events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    caller["turns_heard"] = [
        {
            "ts_ms": target["ts_ms"] + 200,
            "text": "Your order g w four eight two one three ships to Valencia Street in Austin. Do you confirm?",
        }
    ]
    caller["turns_said"].append(
        {
            "ts_ms": target["ts_ms"] + 3000,
            "end_ms": target["ts_ms"] + 6000,
            "text": "No, it is Boston, not Austin.",
        }
    )
    (folder / "caller.json").write_text(json.dumps(caller))
    scores = score_attempt(folder, Session.load(root / "sessions" / "refund-basic.yaml"))
    assert scores["valid"] is False
    assert "simulator misheard the agent" in scores["failure_reason"]
    _ = t0
