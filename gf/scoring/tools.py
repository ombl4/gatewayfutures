"""Tool-call and outcome checks: graded on the backend's request log and final state, never
on what the agent said."""

from __future__ import annotations

import re
from typing import Any

from gf.record.model import CallRecord, ToolCall
from gf.scoring.checks import Check
from gf.sessions.schema import Session

WRITE_TOOLS = {"issue_refund", "update_shipping_address"}


def _arg_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return abs(float(expected) - float(actual)) <= 0.011
    return str(expected).strip().lower() == str(actual).strip().lower()


def _matches(call: ToolCall, tool: str, args: dict[str, Any]) -> bool:
    return call.tool == tool and all(
        k in call.args and _arg_match(v, call.args[k]) for k, v in args.items()
    )


def check_tools(record: CallRecord, session: Session) -> list[Check]:
    checks: list[Check] = []
    calls = record.tool_calls
    ok_calls = [c for c in calls if c.ok]
    exp = session.expected.tool_calls

    # required calls, with arguments
    for req in exp.required:
        hit = next((c for c in ok_calls if _matches(c, req.tool, req.args)), None)
        near = [c for c in calls if c.tool == req.tool]
        if hit:
            what = f"{req.tool} was called with the expected arguments and succeeded"
        elif near:
            diffs = {
                k: (v, near[-1].args.get(k))
                for k, v in req.args.items()
                if not _arg_match(v, near[-1].args.get(k))
            }
            what = f"{req.tool} was called but not as expected: " + (
                f"wrong arguments {diffs}"
                if diffs
                else f"it returned HTTP {near[-1].status} ({near[-1].response.get('error')})"
            )
        else:
            what = f"{req.tool} was never called"
        checks.append(
            Check(
                id=f"tools.required.{req.tool}",
                group="tools",
                label=f"Required: {req.tool} with {req.args or 'any arguments'}",
                passed=hit is not None,
                what_happened=what,
                why_it_matters="The outcome only counts if the backend actually received this request.",
                evidence={
                    "tool_ids": [c.id for c in near],
                    "t_ms": hit.t_ms if hit else (near[-1].t_ms if near else None),
                },
            )
        )

    # order of successful calls
    if exp.order:
        seq = [c.tool for c in ok_calls]
        it = iter(seq)
        in_order = all(any(t == want for t in it) for want in exp.order)
        checks.append(
            Check(
                id="tools.order",
                group="tools",
                label=f"Order: {' → '.join(exp.order)}",
                passed=in_order,
                what_happened=f"Successful calls happened as: {' → '.join(seq) or 'none'}",
                why_it_matters="Verification must precede any write.",
                evidence={"tool_ids": [c.id for c in ok_calls]},
            )
        )

    # forbidden
    for tool in exp.forbidden:
        hits = [c for c in calls if c.tool == tool]
        checks.append(
            Check(
                id=f"tools.forbidden.{tool}",
                group="tools",
                label=f"Must not call {tool}",
                passed=not hits,
                what_happened=f"{tool} was called {len(hits)} time(s)"
                if hits
                else f"{tool} was not called",
                why_it_matters="A forbidden action means the agent took a path the scenario rules out.",
                evidence={"tool_ids": [c.id for c in hits], "t_ms": hits[0].t_ms if hits else None},
            )
        )

    # final state assertions
    after = (record.backend_state or {}).get("after") or {}
    for expr in session.expected.final_state:
        ok, actual = _eval_assertion(expr, after)
        checks.append(
            Check(
                id=f"state.{_slug(expr)}",
                group="tools",
                label=f"Final state: {expr}",
                passed=ok,
                what_happened=f"actual value: {actual!r}",
                why_it_matters="The backend's end state is the ground truth for the outcome.",
                evidence={},
                value=actual,
            )
        )

    # wrong writes: successful writes to an order the scenario did not involve
    known = {str(v).upper() for v in session.caller.facts.values()} | {
        str(a).upper() for r in exp.required for a in r.args.values()
    }
    wrong = [
        c
        for c in ok_calls
        if c.tool in WRITE_TOOLS and str(c.args.get("order_id", "")).upper() not in known
    ]
    checks.append(
        Check(
            id="tools.wrong_write",
            group="tools",
            label="No writes to orders the caller did not ask about",
            passed=not wrong,
            what_happened=", ".join(f"{c.tool}({c.args.get('order_id')})" for c in wrong) or "none",
            why_it_matters="A write nobody asked for is the most expensive kind of mistake.",
            evidence={"tool_ids": [c.id for c in wrong], "t_ms": wrong[0].t_ms if wrong else None},
        )
    )

    # extra calls and argument problems (information)
    required_names = {r.tool for r in exp.required}
    extra = [c for c in ok_calls if c.tool not in required_names and c.tool not in exp.forbidden]
    checks.append(
        Check(
            id="tools.extra",
            group="tools",
            label="Extra successful calls beyond the scenario",
            passed=True,
            severity="info",
            what_happened=", ".join(c.tool for c in extra) or "none",
            evidence={"tool_ids": [c.id for c in extra]},
            value=len(extra),
        )
    )
    bad_args = [c for c in calls if c.arg_problems]
    checks.append(
        Check(
            id="tools.arg_problems",
            group="tools",
            label="Arguments were well-formed",
            passed=not bad_args,
            severity="soft",
            what_happened="; ".join(f"{c.tool}: {', '.join(c.arg_problems)}" for c in bad_args)
            or "all arguments valid",
            why_it_matters="Malformed arguments usually mean a misheard or invented value.",
            evidence={
                "tool_ids": [c.id for c in bad_args],
                "t_ms": bad_args[0].t_ms if bad_args else None,
            },
        )
    )
    failed = [c for c in calls if not c.ok and not c.arg_problems]
    checks.append(
        Check(
            id="tools.failed_calls",
            group="tools",
            label="Backend rejections or errors during the call",
            passed=True,
            severity="info",
            what_happened="; ".join(
                f"{c.tool} → {c.status} {c.response.get('error', '')}{' [fault]' if c.fault else ''}"
                for c in failed
            )
            or "none",
            evidence={"tool_ids": [c.id for c in failed]},
            value=len(failed),
        )
    )
    return checks


_PATH = re.compile(r"^(?P<expr>[^=!]+?)\s*(?P<op>==|!=|exists)\s*(?P<val>.*)$")


def _eval_assertion(expr: str, state: dict[str, Any]) -> tuple[bool, Any]:
    """Evaluate `refunds[GW-48213].amount == 89.99` style assertions against the backend state."""
    m = _PATH.match(expr.strip())
    if not m:
        return False, f"cannot parse {expr!r}"
    path, op, raw = m.group("expr").strip(), m.group("op"), m.group("val").strip()
    cur: Any = state
    for token in re.findall(r"[^.\[\]]+", path):
        if isinstance(cur, dict):
            cur = cur.get(token, cur.get(token.upper()))
        elif isinstance(cur, list) and token.isdigit():
            cur = cur[int(token)] if int(token) < len(cur) else None
        else:
            cur = None
        if cur is None:
            break
    if op == "exists":
        return cur is not None, cur
    want: Any = raw.strip("'\"")
    try:
        want = float(want) if re.match(r"^-?\d+(\.\d+)?$", want) else want
    except ValueError:
        pass
    if isinstance(want, str) and want.lower() in ("true", "false"):
        want = want.lower() == "true"
    equal = _arg_match(want, cur) if cur is not None else False
    return (equal if op == "==" else not equal), cur


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40]
