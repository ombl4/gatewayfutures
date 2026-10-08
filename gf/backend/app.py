"""FastAPI mock backend. Four tool endpoints, per-call state, a request log, fault injection.

Rules enforced here (not trusted to the agent):
- lookup_order needs the order id AND the customer's zip; a mismatch is "not found".
- issue_refund: delivered orders only, amount <= total, once per order.
- update_shipping_address: only while the order is still processing.
- escalate_to_human: creates a ticket and marks the call ended.
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gf.backend.models import ORDER_ID_RE, CallState, Fault, Fixture, LogEntry, Refund, Ticket
from gf.util import now_ms

DEFAULT_FIXTURE = "orders_basic"
CALL_ID_HEADER = "X-GF-Call-Id"

app = FastAPI(title="Gateway Goods mock backend", version="0.1.0")
_calls: dict[str, CallState] = {}
_initial: dict[str, dict] = {}  # state snapshot at seed time, for before/after diffs


# ---------- seeding and inspection ----------


class SeedRequest(BaseModel):
    fixture: str = DEFAULT_FIXTURE
    faults: list[Fault] = Field(default_factory=list)


def _get_or_seed(call_id: str) -> CallState:
    """Unknown call ids are seeded from the default fixture so a human can test by hand."""
    if call_id not in _calls:
        seed(call_id, SeedRequest())
    return _calls[call_id]


@app.post("/calls/{call_id}/seed")
def seed(call_id: str, body: SeedRequest) -> dict:
    fixture = Fixture.load(body.fixture)
    state = CallState.from_fixture(call_id, fixture, body.faults, now_ms())
    _calls[call_id] = state
    _initial[call_id] = copy.deepcopy(state.snapshot())
    return {"call_id": call_id, "fixture": body.fixture, "faults": len(body.faults)}


@app.get("/calls/{call_id}/log")
def get_log(call_id: str) -> list[dict]:
    return [e.model_dump() for e in _get_or_seed(call_id).log]


@app.get("/calls/{call_id}/state")
def get_state(call_id: str) -> dict:
    state = _get_or_seed(call_id)
    return {"before": _initial.get(call_id), "after": state.snapshot()}


@app.delete("/calls/{call_id}")
def delete_call(call_id: str) -> dict:
    _calls.pop(call_id, None)
    _initial.pop(call_id, None)
    return {"deleted": call_id}


@app.get("/health")
def health() -> dict:
    return {"ok": True, "calls": len(_calls)}


# ---------- tool endpoints ----------


class ToolError(Exception):
    def __init__(self, status: int, error: str, message: str):
        self.status, self.error, self.message = status, error, message


def _validate(tool: str, args: dict[str, Any]) -> list[str]:
    """Strict argument formats, so invented or malformed arguments are visible."""
    problems = []
    if "order_id" in args and not ORDER_ID_RE.match(str(args.get("order_id") or "")):
        problems.append(f"order_id {args.get('order_id')!r} does not match GW-#####")
    if tool == "lookup_order" and not str(args.get("zip") or "").strip():
        problems.append("zip is empty")
    if tool == "issue_refund":
        amount = args.get("amount")
        if not isinstance(amount, int | float) or amount <= 0:
            problems.append(f"amount {amount!r} is not a positive number")
        elif round(amount, 2) != amount:
            problems.append(f"amount {amount!r} has more than two decimals")
        if not str(args.get("reason") or "").strip():
            problems.append("reason is empty")
    if tool == "update_shipping_address" and len(str(args.get("address") or "").strip()) < 8:
        problems.append("address is empty or too short")
    if tool == "escalate_to_human":
        for key in ("reason", "summary"):
            if not str(args.get(key) or "").strip():
                problems.append(f"{key} is empty")
    return problems


async def _apply_fault(state: CallState, tool: str) -> str | None:
    """Return a fault tag if one fires for this call of `tool`; may delay or raise."""
    n = state.tool_counts.get(tool, 0) + 1
    state.tool_counts[tool] = n
    for f in state.faults:
        if f.tool == tool and f.nth == n:
            if f.type == "latency_ms":
                await asyncio.sleep(f.value / 1000)
                return f"latency_ms:{int(f.value)}"
            if f.type == "timeout":
                await asyncio.sleep(f.value or 20)
                return "timeout"
            if f.type == "error_500":
                raise ToolError(500, "internal_error", "Order system unavailable")
            if f.type == "reject":
                raise ToolError(409, "rejected_by_policy", "Request rejected by order system")
    return None


def _lookup_order(state: CallState, args: dict) -> dict:
    order = state.orders.get(args["order_id"])
    if not order:
        raise ToolError(404, "order_not_found", "No order found for that id and zip")
    customer = state.customers.get(order.customer_id)
    if not customer or customer.zip != str(args["zip"]).strip():
        raise ToolError(404, "order_not_found", "No order found for that id and zip")
    refund = state.refunds.get(order.order_id)
    return {
        "order_id": order.order_id,
        "customer_name": customer.name,
        "status": order.status,
        "total": order.total,
        "items": [i.model_dump() for i in order.items],
        "shipping_address": order.shipping_address,
        "refunded": order.refunded,
        "refund": refund.model_dump() if refund else None,
    }


def _issue_refund(state: CallState, args: dict) -> dict:
    order = state.orders.get(args["order_id"])
    if not order:
        raise ToolError(404, "order_not_found", "No such order")
    if order.status != "delivered":
        raise ToolError(409, "not_delivered", f"Order is {order.status}; refunds need delivery")
    if order.refunded:
        raise ToolError(409, "already_refunded", "This order was already refunded")
    if float(args["amount"]) > order.total + 1e-9:
        raise ToolError(409, "amount_exceeds_total", f"Amount exceeds order total {order.total}")
    refund = Refund(
        refund_id=f"RF-{len(state.refunds) + 1:04d}",
        order_id=order.order_id,
        amount=float(args["amount"]),
        reason=str(args["reason"]),
        created_ms=now_ms(),
    )
    state.refunds[order.order_id] = refund
    order.refunded = True
    return {"refund_id": refund.refund_id, "amount": refund.amount, "status": "issued"}


def _update_shipping_address(state: CallState, args: dict) -> dict:
    order = state.orders.get(args["order_id"])
    if not order:
        raise ToolError(404, "order_not_found", "No such order")
    if order.status != "processing":
        raise ToolError(409, "already_shipped", f"Order is {order.status}; address is locked")
    order.shipping_address = str(args["address"]).strip()
    return {"order_id": order.order_id, "shipping_address": order.shipping_address}


def _escalate_to_human(state: CallState, args: dict) -> dict:
    ticket = Ticket(
        ticket_id=f"TK-{len(state.tickets) + 1:04d}",
        reason=str(args["reason"]),
        summary=str(args["summary"]),
        created_ms=now_ms(),
    )
    state.tickets.append(ticket)
    state.ended = True
    return {"ticket_id": ticket.ticket_id, "status": "escalated"}


TOOLS = {
    "lookup_order": _lookup_order,
    "issue_refund": _issue_refund,
    "update_shipping_address": _update_shipping_address,
    "escalate_to_human": _escalate_to_human,
}


@app.post("/tools/{tool}")
async def call_tool(
    tool: str,
    request: Request,
    x_gf_call_id: str | None = Header(default=None, alias=CALL_ID_HEADER),
) -> JSONResponse:
    if not x_gf_call_id:
        raise HTTPException(400, f"{CALL_ID_HEADER} header is required")
    if tool not in TOOLS:
        raise HTTPException(404, f"unknown tool {tool}")
    state = _get_or_seed(x_gf_call_id)
    args = await request.json()
    started = now_ms()
    problems = _validate(tool, args)
    fault_tag: str | None = None
    status, body = 200, {}
    try:
        if problems:
            raise ToolError(422, "invalid_arguments", "; ".join(problems))
        fault_tag = await _apply_fault(state, tool)
        body = TOOLS[tool](state, args)
    except ToolError as e:
        status, body = e.status, {"error": e.error, "message": e.message}
    finished = now_ms()
    state.log.append(
        LogEntry(
            id=len(state.log) + 1,
            tool=tool,
            args=args,
            response=body,
            status=status,
            ok=status < 300,
            ts_ms=started,
            duration_ms=finished - started,
            arg_problems=problems,
            fault=fault_tag,
        )
    )
    return JSONResponse(status_code=status, content=body)
