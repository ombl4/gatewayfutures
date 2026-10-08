"""The Check record every scorer produces, with plain-language fields for non-technical readers."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Severity = Literal["hard", "soft", "info"]


class Check(BaseModel):
    id: str
    group: str  # tools | claims | speech | ux | quality | validity
    label: str  # what was checked, plain language
    passed: bool
    severity: Severity = "hard"  # hard: fails the call; soft: flagged; info: reported only
    what_happened: str = ""
    why_it_matters: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)  # t_ms, tool_ids, turn_ns, quotes
    value: Any = None  # the measured number, when there is one


def hard_fails(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.passed and c.severity == "hard"]


def evidence_ms(check: Check | dict[str, Any]) -> int | None:
    """The moment in the call a check's evidence points at, in ms from the start of the
    recording, or None when the check has no time (e.g. a missing tool call)."""
    ev = (check.evidence if isinstance(check, Check) else check.get("evidence")) or {}
    t = ev.get("t_ms")
    if t is None and ev.get("claims"):
        bad = [c for c in ev["claims"] if not c.get("backed")]
        t = (bad or ev["claims"])[0].get("t_ms")
    if t is None and ev.get("gaps"):
        t = ev["gaps"][0].get("start_ms")
    if t is None and ev.get("events"):
        e0 = ev["events"][0]
        t = e0.get("agent_start_ms") or e0.get("caller_start_ms")
    if t is None and ev.get("calls"):
        c0 = ev["calls"][0]
        t = c0.get("t_ms") if isinstance(c0, dict) else None
    return int(t) if t is not None else None


def first_evidence_ms(checks: list[Check]) -> int | None:
    """Evidence time of the first failed check: hard failures first, then soft flags."""
    for sev in ("hard", "soft"):
        for c in checks:
            if not c.passed and c.severity == sev:
                t = evidence_ms(c)
                if t is not None:
                    return t
    return None
