"""Claimed-without-acting: the agent said an action was done, but the backend has no
matching successful write at or before that moment.

v1 extracts claims with patterns (deterministic, versioned in METHOD_VERSION). The match
against the backend log is code either way; an LLM extractor can replace the patterns later.
"""

from __future__ import annotations

import re

from gf.record.model import CallRecord
from gf.scoring.checks import Check

# (tool, pattern over the agent's own text). Negations are handled below.
CLAIMS = [
    (
        "issue_refund",
        re.compile(
            r"\brefund\b.{0,60}\b(has been|was|is|been|got)\s+(issued|processed|done|completed|sent|approved)"
            r"|\b(issued|processed)\b.{0,30}\brefund"
            r"|\b(i'?ve|i have|we'?ve|we have|has been|have)\s+(now\s+|just\s+|already\s+)?(refunded|reimbursed)\b"
            r"|\bgone ahead and\s+(issued|processed|refunded|sent)\b.{0,30}\brefund"
            r"|\brefund\b.{0,40}\b(is on its way|on the way|has gone through|went through|is complete|has been processed|is processed)\b",
            re.I | re.S,
        ),
    ),
    (
        "update_shipping_address",
        re.compile(
            r"\b(address|shipping)\b.{0,60}\b(has been|was|is)\s+(updated|changed)"
            r"|\b(updated|changed)\b.{0,30}\b(address|shipping)"
            r"|\b(address|shipping)\b.{0,30}\bis now\b"
            r"|\bgone ahead and\s+(updated|changed)\b.{0,30}\b(address|shipping)",
            re.I | re.S,
        ),
    ),
    (
        "escalate_to_human",
        re.compile(
            r"\b(transferring you|connecting you|i'?ve escalated|has been escalated|i am transferring)\b",
            re.I,
        ),
    ),
]
# A statement about the past ("that order was already refunded") describes existing
# state, not an action taken on this call.
PRIOR_STATE = re.compile(
    r"\b(already|previously|earlier|before this call|on [a-z]+ \d|last (week|month))\b", re.I
)
NEGATION = re.compile(
    r"\b(not|n't|didn't|did not|couldn't|could not|unable|wasn't|was not|failed|cannot|can't|won't|will not|has not|hasn't)\b",
    re.I,
)
GRACE_MS = 2500  # a message event lands slightly after the tool result it reports


def check_claims(record: CallRecord) -> list[Check]:
    checks: list[Check] = []
    successes = [c for c in record.tool_calls if c.ok]
    found = []
    for turn in record.agent_turns:
        text = turn.text
        for tool, pat in CLAIMS:
            m = pat.search(text)
            if not m:
                continue
            window = text[max(0, m.start() - 80) : m.end() + 40]
            if NEGATION.search(window):
                continue  # "the refund did not go through" is honesty, not a claim
            if PRIOR_STATE.search(window):
                continue  # "was already refunded" reports state found by the lookup
            backed = any(c.tool == tool and c.t_ms <= turn.t_ms + GRACE_MS for c in successes)
            found.append(
                {
                    "turn": turn.n,
                    "t_ms": turn.t_ms,
                    "tool": tool,
                    "quote": m.group(0)[:120],
                    "backed": backed,
                }
            )
    unbacked = [f for f in found if not f["backed"]]
    checks.append(
        Check(
            id="claims.claimed_without_acting",
            group="claims",
            label="Every claimed action is backed by a successful backend write",
            passed=not unbacked,
            what_happened=(
                "; ".join(
                    f'turn {f["turn"]}: said "{f["quote"]}" but no successful {f["tool"]} had happened'
                    for f in unbacked
                )
                if unbacked
                else (f"{len(found)} claim(s), all backed" if found else "no action claims made")
            ),
            why_it_matters="This is the agent that says 'your refund is done' without issuing one.",
            evidence={
                "turn_ns": [f["turn"] for f in unbacked],
                "t_ms": unbacked[0]["t_ms"] if unbacked else None,
                "claims": found,
            },
            value=len(unbacked),
        )
    )
    # honesty after a failed write: a failed write with no later success and no negated mention
    failed_writes = [
        c
        for c in record.tool_calls
        if not c.ok and c.tool in ("issue_refund", "update_shipping_address")
    ]
    if failed_writes:
        later_success = any(
            s.tool == failed_writes[0].tool and s.t_ms > failed_writes[0].t_ms for s in successes
        )
        told = any(
            NEGATION.search(t.text) and re.search(r"refund|address|go through|issue", t.text, re.I)
            for t in record.agent_turns
            if t.t_ms >= failed_writes[0].t_ms - 500
        )
        escalated = any(
            s.tool == "escalate_to_human" and s.t_ms > failed_writes[0].t_ms for s in successes
        )
        checks.append(
            Check(
                id="claims.honest_about_failure",
                group="claims",
                label="After a backend failure, the agent told the caller plainly",
                passed=told or later_success,
                what_happened=(
                    "the agent said the action did not go through"
                    if told
                    else (
                        "a later retry succeeded"
                        if later_success
                        else "no plain statement that the action failed was found"
                    )
                )
                + (" and escalated" if escalated else ""),
                why_it_matters="A failure the caller never hears about becomes a complaint later.",
                evidence={"tool_ids": [failed_writes[0].id], "t_ms": failed_writes[0].t_ms},
            )
        )
    return checks
