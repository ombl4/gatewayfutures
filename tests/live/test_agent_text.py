"""T2.2 / T2.4: the support agent in text mode (real OpenAI LLM, no audio), against the
in-process mock backend. Each test is one voice behaviour from the spec.

Run with `make test-live` (needs OPENAI_API_KEY).
"""

from __future__ import annotations

import httpx
import pytest
from livekit.agents import AgentSession

from gf.agent.config import agent_config
from gf.agent.tools import BackendClient, CallContext
from gf.agent.worker import SupportAgent, build_session
from gf.backend import app as backend

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _fresh_backend():
    backend._calls.clear()
    backend._initial.clear()


async def make_session(call_id: str, faults: list[dict] | None = None) -> AgentSession:
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=backend.app), base_url="http://b")
    if faults:
        await client.post(f"/calls/{call_id}/seed", json={"faults": faults})
    cfg = agent_config()
    cc = CallContext(call_id=call_id, config=cfg, backend=BackendClient(call_id, client=client))
    session = build_session(cfg, cc, voice=False)
    await session.start(agent=SupportAgent(cfg))
    return session


def backend_log(call_id: str) -> list[dict]:
    """Empty when the backend never heard from this call (no tool was called)."""
    state = backend._calls.get(call_id)
    return [e.model_dump() for e in state.log] if state else []


def agent_said(result) -> str:
    return " ".join(
        ev.item.text_content or ""
        for ev in result.events
        if ev.type == "message" and ev.item.role == "assistant"
    ).lower()


async def say(session: AgentSession, text: str):
    return await session.run(user_input=text)


# ---- behaviours ----


async def test_refund_flow_calls_tools_in_order_with_right_args_after_confirmation():
    s = await make_session("t-refund")
    await say(s, "Hi, I need a refund. My order is GW-48213 and the zip code is 94110.")
    log = backend_log("t-refund")
    assert [e["tool"] for e in log] == ["lookup_order"], "lookup must come first, nothing else yet"
    assert log[0]["args"] == {"order_id": "GW-48213", "zip": "94110"}

    r = await say(s, "The blender arrived broken, I want the full amount back.")
    assert "issue_refund" not in [e["tool"] for e in backend_log("t-refund")], (
        "no refund before the caller confirms"
    )
    assert "89.99" in agent_said(r) or "eighty-nine" in agent_said(r), "reads back the amount"

    r = await say(s, "Yes, that's right, go ahead.")
    log = backend_log("t-refund")
    assert [e["tool"] for e in log] == ["lookup_order", "issue_refund"]
    assert log[1]["args"]["order_id"] == "GW-48213" and log[1]["args"]["amount"] == 89.99
    assert log[1]["ok"] is True
    await s.aclose()


async def test_refuses_order_details_before_verification():
    s = await make_session("t-verify")
    r = await say(s, "What's the status of order GW-48213?")
    assert backend_log("t-verify") == [], "no lookup without a zip code"
    assert "zip" in agent_said(r)
    await s.aclose()


async def test_unknown_order_asks_to_repeat_digits():
    s = await make_session("t-unknown")
    r = await say(s, "My order number is GW-11111, zip 94110, I want a refund.")
    log = backend_log("t-unknown")
    assert log and log[0]["tool"] == "lookup_order" and log[0]["status"] == 404
    said = agent_said(r)
    assert "find" in said or "found" in said
    assert "digit" in said or "repeat" in said or "again" in said
    assert "issue_refund" not in [e["tool"] for e in log]
    await s.aclose()


async def test_tool_fault_is_reported_honestly_not_claimed():
    faults = [{"tool": "issue_refund", "type": "error_500", "nth": 1}]
    s = await make_session("t-fault", faults=faults)
    await say(s, "Refund please, order GW-48213, zip 94110, it arrived broken, full amount.")
    r = await say(s, "Yes, confirm the refund of 89.99.")
    log = backend_log("t-fault")
    refunds = [e for e in log if e["tool"] == "issue_refund"]
    assert refunds and refunds[0]["status"] == 500
    said = agent_said(r)
    assert not any(p in said for p in ("has been processed", "has been issued", "is done")), (
        f"must not claim success: {said}"
    )
    assert any(
        w in said
        for w in (
            "sorry",
            "apolog",
            "didn't go through",
            "did not go through",
            "unable",
            "couldn't",
            "could not",
        )
    )
    await s.aclose()


async def test_policy_block_explains_and_offers_escalation():
    s = await make_session("t-policy")
    r = await say(s, "I want a refund on order GW-48377, zip 60614.")
    said = agent_said(r)
    log = backend_log("t-policy")
    assert "issue_refund" not in [e["tool"] for e in log]
    assert any(w in said for w in ("shipped", "delivered", "not yet")), said
    await s.aclose()


async def test_escalation_creates_ticket_and_ends_call():
    s = await make_session("t-escalate")
    await say(s, "I want to speak to a human right now, please.")
    await say(s, "Yes, transfer me.")
    log = backend_log("t-escalate")
    assert [e["tool"] for e in log][-1:] == ["escalate_to_human"], log
    assert backend._calls["t-escalate"].ended is True
    assert s.userdata.ended and s.userdata.end_reason == "escalated"
    await s.aclose()
