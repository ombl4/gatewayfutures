"""`gf sessions generate` (spec T4.5): practice sessions from the agent description.

The LLM proposes sessions as JSON; each proposal is validated through the same schema as a
hand-written file (unknown tools, missing fixtures, bad values are rejected) and written to
sessions/generated/ for human review. Temperature 0 and a seed make a given request
reproducible.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from gf.agent.config import agent_config
from gf.config import ROOT, settings
from gf.sessions.schema import Session, load_all

VOICES = {
    "en-US": [
        "aura-2-asteria-en",
        "aura-2-luna-en",
        "aura-2-athena-en",
        "aura-2-orion-en",
        "aura-2-arcas-en",
    ],
    "en-GB": ["aura-2-pandora-en", "aura-2-draco-en"],
    "en-AU": ["aura-2-hyperion-en", "aura-2-theia-en"],
    "en-PH": ["aura-2-amalthea-en"],
}
TOOL_ARGS = {
    "lookup_order": "order_id (GW-#####), zip (5 digits)",
    "issue_refund": "order_id, amount (number, 2 dp), reason",
    "update_shipping_address": "order_id, new_address",
    "escalate_to_human": "reason, summary",
}
EXAMPLE = (
    (ROOT / "sessions" / "refund-basic.yaml").read_text()
    if (ROOT / "sessions" / "refund-basic.yaml").exists()
    else ""
)


def _prompt(count: int, focus: str, existing_titles: list[str]) -> str:
    cfg = agent_config()
    fixture = (ROOT / "fixtures" / "orders_basic.yaml").read_text()
    return f"""You design practice sessions for testing a phone support agent with simulated callers.

AGENT (plain description):
{cfg.description}

TOOLS the agent can call (name: arguments): {json.dumps(TOOL_ARGS)}
Business rules: lookup needs the matching zip; refunds only for delivered orders, up to the order
total, once per order; address changes only while processing; escalation ends the call.

FIXTURE DATA every call starts from (use ONLY these customers, orders, zips, totals, statuses):
{fixture}

SESSION FORMAT: produce JSON with the same structure as this YAML example (facts may only contain
values from the fixture data; the caller may never invent an order number, zip or amount):
{EXAMPLE}

Rules for a good set:
- Spread across the tools and policy edges: refund (full, partial, over the limit, already refunded,
  not delivered), address change (processing vs shipped), escalation (asked for, or forced by policy
  or by an injected fault), verification problems (wrong zip first, order number misheard).
- Vary the callers: accents en-US / en-GB / en-AU / en-PH with a matching voice from
  {json.dumps(VOICES)}; styles (calm, impatient, confused, chatty, elderly and slow); pace 0.9–1.2;
  conditions: noise like "cafe@15dB" or "street@10dB" or null, phone_line true/false,
  packet_loss 0–0.05, low_quality_mic, interruptions 0–0.7 (probability of barging in),
  patience_s 10–25.
- Injected faults where useful: faults: [{{"tool": "issue_refund", "type": "error_500", "nth": 1}}]
  (types: latency_ms with latency_ms: 3000, error_500, timeout, reject).
- expected.tool_calls.required lists the exact calls a correct agent makes, with ONLY the
  deterministic args order_id, zip and amount taken from the fixture (never reason, summary or
  new_address: those are free text the agent words itself; assert addresses through final_state);
  forbidden lists tools it must not call; final_state uses assertions like
  "refunds[GW-48213].amount == 89.99", "orders[GW-48502].shipping_address != \\"...\\"",
  "refunds[GW-48377] not exists". outcome is a short snake_case label.
- The goal must be achievable from the caller's facts, and stop_when must be observable.
- llm: {{"model": "gpt-4.1-mini", "temperature": 0.0, "seed": <a distinct integer 400-999>}}.
- Titles must differ from these existing sessions: {json.dumps(existing_titles)}.
{("Focus: " + focus) if focus else ""}

Return ONLY a JSON object: {{"sessions": [ ...{count} session objects... ]}}."""


def generate(
    count: int = 10,
    focus: str = "",
    *,
    seed: int = 42,
    model: str = "gpt-4.1-mini",
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Ask the LLM for `count` sessions, validate each, write the valid ones. Returns a report
    with the written files and the rejected proposals (reason + title)."""
    from openai import OpenAI

    s = settings()
    out_dir = out_dir or (s.sessions_dir / "generated")
    out_dir.mkdir(parents=True, exist_ok=True)
    existing = load_all(s.sessions_dir)
    titles = [x.title for x in existing]
    client = OpenAI(api_key=s.openai_api_key)
    resp = client.chat.completions.create(
        model=model,
        temperature=0,
        seed=seed,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": _prompt(count, focus, titles)}],
    )
    raw = resp.choices[0].message.content or "{}"
    return write_proposals(json.loads(raw).get("sessions", []), out_dir, known_titles=titles)


def write_proposals(
    proposals: list[dict[str, Any]], out_dir: Path, *, known_titles: list[str] = ()
) -> dict[str, Any]:
    """Validate proposals through the Session schema; write valid ones as immutable YAML files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written, rejected = [], []
    seen = set(known_titles)
    for i, prop in enumerate(proposals):
        title = (
            str(prop.get("title", f"untitled-{i}")) if isinstance(prop, dict) else f"proposal-{i}"
        )
        try:
            if not isinstance(prop, dict):
                raise ValueError("not an object")
            prop.pop("id", None)
            _sanitise(prop)
            _check_against_fixture(prop)
            if title in seen:
                raise ValueError("duplicate title")
            slug = _slug(title)
            target = out_dir / f"{slug}.yaml"
            if target.exists():
                raise ValueError(f"{target.name} already exists")
            text = yaml.safe_dump(prop, sort_keys=False, allow_unicode=True)
            tmp = out_dir / f".{slug}.tmp.yaml"
            tmp.write_text(text)
            try:
                sess = Session.load(tmp)
            finally:
                tmp.unlink(missing_ok=True)
            target.write_text(sess.dump())
            seen.add(title)
            written.append({"file": str(target), "id": sess.id, "title": sess.title})
        except Exception as e:  # noqa: BLE001 - every rejection is reported, never written
            rejected.append({"title": title, "reason": str(e)[:300]})
    return {"written": written, "rejected": rejected}


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:60] or "session"


DETERMINISTIC_ARGS = {"order_id", "zip", "amount"}


def _sanitise(prop: dict[str, Any]) -> None:
    """Required calls may only pin arguments the agent cannot word differently; free-text
    arguments (reason, summary, new_address) are dropped, never asserted."""
    tc = (prop.get("expected") or {}).get("tool_calls") or {}
    for call in tc.get("required") or []:
        if isinstance(call, dict) and isinstance(call.get("args"), dict):
            call["args"] = {k: v for k, v in call["args"].items() if k in DETERMINISTIC_ARGS}


def _check_against_fixture(prop: dict[str, Any]) -> None:
    """Reject proposals the backend could never satisfy: unknown orders, a zip that does not
    match the order, a required refund on an undelivered or already refunded order, a required
    address change on a shipped order, or an address change without an address to give."""
    fx_path = ROOT / "fixtures" / f"{prop.get('fixtures', 'orders_basic')}.yaml"
    if not fx_path.exists():
        return  # the schema reports the missing fixture
    fx = yaml.safe_load(fx_path.read_text()) or {}
    orders = {o["order_id"].upper(): o for o in fx.get("orders", [])}
    customers = {c["id"]: c for c in fx.get("customers", [])}
    refunded = {r["order_id"].upper() for r in fx.get("refunds", [])}
    facts = (prop.get("caller") or {}).get("facts") or {}
    oid = str(facts.get("order_id", "")).upper()
    if oid and oid not in orders:
        raise ValueError(f"facts.order_id {oid} is not in the fixture")
    if oid and facts.get("zip") is not None:
        cust = customers.get(orders[oid]["customer_id"], {})
        if str(facts["zip"]).zfill(5) != str(cust.get("zip", "")).zfill(5):
            raise ValueError(
                f"facts.zip {facts['zip']} does not match order {oid} (zip {cust.get('zip')})"
            )
    required = ((prop.get("expected") or {}).get("tool_calls") or {}).get("required") or []
    for call in required:
        if not isinstance(call, dict):
            continue
        args = call.get("args") or {}
        coid = str(args.get("order_id", oid)).upper()
        order = orders.get(coid)
        if call.get("tool") == "issue_refund" and order:
            if order.get("status") != "delivered":
                raise ValueError(
                    f"required issue_refund on {coid}, which is {order.get('status')}, not delivered"
                )
            if coid in refunded:
                raise ValueError(f"required issue_refund on {coid}, which is already refunded")
            if (
                args.get("amount") is not None
                and float(args["amount"]) > float(order.get("total", 0)) + 1e-6
            ):
                raise ValueError(
                    f"required refund {args['amount']} exceeds the order total {order.get('total')}"
                )
        if call.get("tool") == "update_shipping_address":
            if order and order.get("status") != "processing":
                raise ValueError(
                    f"required update_shipping_address on {coid}, which is {order.get('status')}, not processing"
                )
            if not facts.get("new_address"):
                raise ValueError(
                    "required update_shipping_address but facts.new_address is missing"
                )
