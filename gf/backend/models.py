"""Data model and per-call state for the mock backend."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from gf.config import ROOT

ORDER_ID_RE = re.compile(r"^GW-\d{5}$")
OrderStatus = Literal["processing", "shipped", "delivered"]
FaultType = Literal["latency_ms", "error_500", "timeout", "reject"]


class Customer(BaseModel):
    id: str
    name: str
    zip: str


class Item(BaseModel):
    sku: str
    name: str
    qty: int
    price: float


class Order(BaseModel):
    order_id: str
    customer_id: str
    status: OrderStatus
    total: float
    items: list[Item]
    shipping_address: str
    refunded: bool = False


class Refund(BaseModel):
    refund_id: str
    order_id: str
    amount: float
    reason: str
    created_ms: int


class Ticket(BaseModel):
    ticket_id: str
    reason: str
    summary: str
    created_ms: int


class Fault(BaseModel):
    """One injected fault: applies to the nth call of `tool` within a call."""

    tool: str
    type: FaultType
    nth: int = 1
    value: float = 0  # latency_ms: delay; timeout: seconds to hang; reject: unused


class LogEntry(BaseModel):
    """One backend request. This log, not the transcript, is what grading reads."""

    id: int
    tool: str
    args: dict
    response: dict
    status: int
    ok: bool
    ts_ms: int
    duration_ms: int
    arg_problems: list[str] = Field(default_factory=list)
    fault: str | None = None


class Fixture(BaseModel):
    customers: list[Customer]
    orders: list[Order]

    @classmethod
    def load(cls, name_or_path: str | Path) -> Fixture:
        path = Path(name_or_path)
        if not path.exists():
            path = ROOT / "fixtures" / f"{name_or_path}.yaml"
        if not path.exists():
            path = ROOT / name_or_path
        return cls(**yaml.safe_load(path.read_text()))


class CallState(BaseModel):
    """Everything the backend knows about one call. Calls never share state."""

    call_id: str
    customers: dict[str, Customer]
    orders: dict[str, Order]
    refunds: dict[str, Refund] = Field(default_factory=dict)  # keyed by order_id
    tickets: list[Ticket] = Field(default_factory=list)
    faults: list[Fault] = Field(default_factory=list)
    log: list[LogEntry] = Field(default_factory=list)
    tool_counts: dict[str, int] = Field(default_factory=dict)
    ended: bool = False
    created_ms: int

    @classmethod
    def from_fixture(cls, call_id: str, fixture: Fixture, faults: list[Fault], created_ms: int):
        return cls(
            call_id=call_id,
            customers={c.id: c for c in fixture.customers},
            orders={o.order_id: o.model_copy() for o in fixture.orders},
            faults=faults,
            created_ms=created_ms,
        )

    def snapshot(self) -> dict:
        """State as the scorer sees it: orders, refunds, tickets."""
        return {
            "orders": {k: v.model_dump() for k, v in self.orders.items()},
            "refunds": {k: v.model_dump() for k, v in self.refunds.items()},
            "tickets": [t.model_dump() for t in self.tickets],
            "ended": self.ended,
        }
