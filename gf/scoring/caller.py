"""Caller (persona) score (spec T5.11): did the simulated caller do its job?

These checks judge the simulator, never the agent: a caller that states its goal late, hangs
up because of a limit, or drifts from its persona makes the call a weaker test. Hard
character breaks live in `validity.simulation`; these are soft and are reported as caller
flags, separate from the agent's flags.
"""

from __future__ import annotations

import re

from gf.record.model import CallRecord
from gf.scoring.checks import Check
from gf.sessions.schema import Session

LEGITIMATE_ENDINGS = ("caller", "agent_left", "room_closed", "agent")


def check_caller(record: CallRecord, session: Session | None) -> list[Check]:
    checks: list[Check] = []
    if record.meta.get("engine") == "livekit-simulate" or session is None:
        return checks
    leaked = [
        t.n
        for t in record.caller_turns
        if re.search(r"functions\.\w+\s*\(|\bend_call\s*\(", t.text)
    ]
    checks.append(
        Check(
            id="caller.spoke_tool_syntax",
            group="caller",
            label="The simulated caller never spoke its own tool syntax",
            passed=not leaked,
            severity="soft",
            what_happened=f"turn(s) {leaked}: the caller read its end_call tool out loud"
            if leaked
            else "clean",
            why_it_matters=(
                "The agent then hears 'functions dot end underscore call'; a simulator fault, "
                "filtered out of the caller's speech since 2026-10-09."
            ),
            evidence={"turn_ns": leaked},
        )
    )
    legit = (record.ended_by or "") in LEGITIMATE_ENDINGS
    checks.append(
        Check(
            id="caller.ended_legitimately",
            group="caller",
            label="The call ended for a reason, not a limit",
            passed=legit,
            severity="soft",
            what_happened=f"ended by {record.ended_by or 'unknown'}: {record.end_reason}"[:160],
            why_it_matters="A call cut by a turn or time limit may have been about to resolve; it says less about the agent.",
            evidence={"ended_by": record.ended_by, "end_reason": record.end_reason},
        )
    )
    return checks
