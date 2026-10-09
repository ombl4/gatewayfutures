"""Suites and areas (spec T7.4): two ways to group sessions without touching their immutable
files.

* **Areas** are derived from the session itself: what the agent must do (`refund`,
  `address change`, `escalation`, `denial`) and how hard the call is (`fault handling`,
  `hard line`, `interruptions`, `impatient`).
* **Suites** are named lists in `sessions/suites.yaml` (`smoke`, `regression`, …) that
  reference session files by name, so an edited session keeps its membership and the
  session-set tag still hashes the files actually run.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from gf.sessions.schema import Session, load_all

SUITES_FILE = "suites.yaml"
PRIMARY_AREAS = ("refund", "address change", "escalation", "denial")
MODIFIER_AREAS = ("fault handling", "hard line", "interruptions", "impatient")
IMPATIENT_WORDS = ("impatient", "hurried", "annoyed", "brisk", "talks fast", "wants it done")


def areas_of(session: Session) -> list[str]:
    """Primary area first (what the caller is after), then `denial` when the agent must
    refuse it, then the modifiers that make the call hard."""
    tc = session.expected.tool_calls
    required = [r.tool for r in tc.required]
    goal = session.caller.goal.lower()
    out: list[str] = []
    if "refund" in goal or "money back" in goal or "issue_refund" in required:
        out.append("refund")
    elif "address" in goal or "update_shipping_address" in required:
        out.append("address change")
    elif (
        any(w in goal for w in ("human", "person", "representative", "someone"))
        or "escalate_to_human" in required
    ):
        out.append("escalation")
    else:
        out.append("other")
    write_for = {"refund": "issue_refund", "address change": "update_shipping_address"}.get(out[0])
    # the caller wants a write the expected outcome does not include: the agent must refuse
    # (unless an injected fault is what stops it: that is fault handling, below)
    if write_for and write_for not in required and not session.faults:
        out.append("denial")
    if "escalate_to_human" in required and out[0] != "escalation":
        out.append("escalation")
    if session.faults:
        out.append("fault handling")
    c = session.caller.conditions
    if c.noise or c.phone_line or c.packet_loss or c.low_quality_mic:
        out.append("hard line")
    if c.interruptions:
        out.append("interruptions")
    style = session.caller.persona.style.lower()
    if any(w in style for w in IMPATIENT_WORDS) or c.patience_s < 15:
        out.append("impatient")
    return out


def session_file(session: Session) -> str:
    """The name a suite uses for a session: its file name without `.yaml`."""
    return Path(session.source_path or session.title).stem


def load_suites(folder: Path) -> dict[str, list[str]]:
    p = folder / SUITES_FILE
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return {str(k): [str(x) for x in (v or [])] for k, v in data.items()}


def save_suites(folder: Path, suites: dict[str, list[str]]) -> None:
    text = (
        "# Named suites of practice sessions, by file name (without .yaml).\n"
        "# smoke: a quick check; regression: sessions that have failed before and must run\n"
        "# every time. Edit here or from a session's page in the UI. Never lists session ids,\n"
        "# so an edited session keeps its membership.\n"
    )
    text += yaml.safe_dump(
        {k: sorted(set(v)) for k, v in sorted(suites.items())}, sort_keys=False, allow_unicode=True
    )
    (folder / SUITES_FILE).write_text(text)


def suites_of(session: Session, suites: dict[str, list[str]]) -> list[str]:
    name = session_file(session)
    return [s for s, members in suites.items() if name in members]


def sessions_in_suite(folder: Path, suite: str) -> list[Session]:
    """The sessions a suite names, in suite order; retired sessions are skipped, unknown
    names raise."""
    suites = load_suites(folder)
    if suite not in suites:
        raise ValueError(f"unknown suite {suite!r}; known: {', '.join(sorted(suites)) or 'none'}")
    by_name = {session_file(s): s for s in load_all(folder, include_retired=False)}
    retired = {session_file(s) for s in load_all(folder)} - set(by_name)
    out = []
    for name in suites[suite]:
        if name in by_name:
            out.append(by_name[name])
        elif name in retired:
            continue
        else:
            raise ValueError(f"suite {suite!r} names a session that does not exist: {name}")
    return out


def add_to_suite(folder: Path, suite: str, name: str) -> dict[str, list[str]]:
    suites = load_suites(folder)
    suites.setdefault(suite, [])
    if name not in suites[suite]:
        suites[suite].append(name)
    save_suites(folder, suites)
    return suites


def remove_from_suite(folder: Path, suite: str, name: str) -> dict[str, list[str]]:
    suites = load_suites(folder)
    if suite in suites:
        suites[suite] = [n for n in suites[suite] if n != name]
        if not suites[suite]:
            del suites[suite]
    save_suites(folder, suites)
    return suites
