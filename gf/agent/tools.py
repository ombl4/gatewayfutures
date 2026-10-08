"""The agent's four tools. Each is a thin HTTP call to the mock backend.

Grading reads the backend's own request log, so these functions add nothing clever: they
forward arguments, return the backend's answer (success or error) as text the LLM can
relay, and speak a filler if the backend is slow.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
from livekit.agents import RunContext, StopResponse, function_tool

from gf.agent.config import AgentConfig
from gf.config import settings

log = logging.getLogger("gf.agent.tools")


@dataclass
class CallContext:
    """Per-call state shared by the tools, kept in AgentSession.userdata."""

    call_id: str
    config: AgentConfig
    backend: BackendClient
    verified_order: str | None = None
    ended: bool = False
    end_reason: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


class BackendClient:
    """HTTP client bound to one call id. Tests inject an in-process transport."""

    def __init__(
        self, call_id: str, base_url: str | None = None, client: httpx.AsyncClient | None = None
    ):
        self.call_id = call_id
        self._client = client or httpx.AsyncClient(
            base_url=base_url or settings().backend_url, timeout=httpx.Timeout(15.0)
        )

    async def tool(self, name: str, **args: Any) -> tuple[bool, dict[str, Any]]:
        """POST to /tools/<name>. Returns (ok, body). Transport errors become a body too."""
        try:
            r = await self._client.post(
                f"/tools/{name}", json=args, headers={"X-GF-Call-Id": self.call_id}
            )
            try:
                body = r.json()
            except ValueError:
                body = {"error": "bad_response", "message": r.text[:200]}
            return r.status_code < 300, body
        except httpx.TimeoutException:
            return False, {"error": "timeout", "message": "The order system did not respond"}
        except httpx.HTTPError as e:
            return False, {"error": "unreachable", "message": f"Order system unreachable: {e}"}

    async def aclose(self) -> None:
        await self._client.aclose()


async def _call_with_filler(ctx: RunContext[CallContext], name: str, **args: Any) -> str:
    """Run a backend call; if it is slow, say the filler so the caller never hears dead air."""
    cc = ctx.userdata
    task = asyncio.ensure_future(cc.backend.tool(name, **args))
    filler_after = cc.config.voice.filler_after_ms / 1000
    try:
        ok, body = await asyncio.wait_for(asyncio.shield(task), timeout=filler_after)
    except TimeoutError:
        try:
            ctx.session.say(cc.config.voice.filler_text, add_to_chat_ctx=False)
        except Exception:  # noqa: BLE001 - no TTS in text mode; the filler is best effort
            log.debug("filler skipped (no audio output)")
        ok, body = await task
    cc.tool_calls.append({"tool": name, "args": args, "ok": ok, "response": body})
    return _format(ok, body)


def _format(ok: bool, body: dict[str, Any]) -> str:
    """Make the outcome unambiguous to the LLM so it cannot talk past an error."""
    if ok:
        return "SUCCESS: " + json.dumps(body)
    return (
        f"FAILED ({body.get('error', 'error')}): {body.get('message', '')}. "
        "Tell the caller this did not go through. Do not claim it succeeded."
    )


@function_tool
async def lookup_order(ctx: RunContext[CallContext], order_id: str, zip: str) -> str:
    """Look up an order. Requires the order number (format GW-12345) and the zip code on the
    order; both must match or the order is reported as not found.

    Args:
        order_id: Order number in the form GW-12345.
        zip: The 5-digit zip code on the order, as the caller stated it.
    """
    order_id = _normalise_order_id(order_id)
    result = await _call_with_filler(ctx, "lookup_order", order_id=order_id, zip=zip.strip())
    if result.startswith("SUCCESS"):
        ctx.userdata.verified_order = order_id
    return result


@function_tool
async def issue_refund(
    ctx: RunContext[CallContext], order_id: str, amount: float, reason: str
) -> str:
    """Issue a refund on a delivered order. Only call after the caller confirmed the amount.

    Args:
        order_id: Order number in the form GW-12345.
        amount: Refund amount in dollars, at most the order total.
        reason: The caller's reason in a few words.
    """
    order_id = _normalise_order_id(order_id)
    return await _call_with_filler(
        ctx, "issue_refund", order_id=order_id, amount=round(float(amount), 2), reason=reason
    )


@function_tool
async def update_shipping_address(ctx: RunContext[CallContext], order_id: str, address: str) -> str:
    """Change the shipping address of an order that has not shipped yet. Only call after the
    caller confirmed the full address.

    Args:
        order_id: Order number in the form GW-12345.
        address: The complete new shipping address.
    """
    order_id = _normalise_order_id(order_id)
    return await _call_with_filler(
        ctx, "update_shipping_address", order_id=order_id, address=address.strip()
    )


@function_tool
async def escalate_to_human(ctx: RunContext[CallContext], reason: str, summary: str) -> str:
    """Hand the caller to a human team member. Creates a ticket and ends the call.

    Args:
        reason: Why escalation is needed (policy block, caller request, repeated failure).
        summary: One or two sentences a human can act on.
    """
    cc = ctx.userdata
    result = await _call_with_filler(ctx, "escalate_to_human", reason=reason, summary=summary)
    if not result.startswith("SUCCESS"):
        return result
    cc.ended, cc.end_reason = True, "escalated"
    await _say_and_hang_up(ctx, cc.config.handoff_line)
    raise StopResponse()


def _normalise_order_id(raw: str) -> str:
    """Accept spoken forms ("G W 4 8 2 1 3", "gw48213") and return GW-#####."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"GW-{digits}" if len(digits) == 5 else raw.strip().upper()


async def _say_and_hang_up(ctx: RunContext[CallContext], line: str) -> None:
    """Speak a fixed line, wait for playout, then leave the room (if there is one)."""
    try:
        handle = ctx.session.say(line, allow_interruptions=False)
        await handle.wait_for_playout()
    except Exception:  # noqa: BLE001 - text mode has no audio output
        log.debug("hang-up line skipped (no audio output)")
    await hang_up()


async def hang_up() -> None:
    """Delete the room so every participant is disconnected. No-op outside a job."""
    from livekit.agents import get_job_context

    job = get_job_context(required=False)
    if job is None:
        return
    try:
        await job.delete_room()
    except Exception as e:  # noqa: BLE001
        log.warning("delete_room failed: %s", e)
        job.shutdown(reason="call ended")


ALL_TOOLS = [lookup_order, issue_refund, update_shipping_address, escalate_to_human]
