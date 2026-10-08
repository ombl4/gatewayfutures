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
