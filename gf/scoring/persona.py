"""Advisory persona judge: did the simulated caller stay in character?

One small LLM call per attempt (OpenAI, temperature 0, structured output). It never decides
validity on its own: the deterministic `caller_in_character` check does that. The judge's
verdict is recorded as an info check so disagreements can be reviewed and labelled.
On by default; GF_PERSONA_JUDGE=0 turns it off (unit tests do), and it never runs without a key.
"""

from __future__ import annotations

import json
import os

from pydantic import BaseModel

from gf.record.model import CallRecord
from gf.scoring.checks import Check
from gf.sessions.schema import Session

JUDGE_MODEL = "gpt-4.1-mini"
PROMPT_VERSION = "persona-judge-v1"


class PersonaVerdict(BaseModel):
    in_character: bool
    used_only_known_facts: bool
    tone_matches_style: bool
    notes: str


def enabled() -> bool:
    """On by default; GF_PERSONA_JUDGE=0 disables it, and it never runs without a key."""
    flag = os.environ.get("GF_PERSONA_JUDGE", "1")
    return flag not in ("0", "false", "no") and bool(os.environ.get("OPENAI_API_KEY"))


def judge_persona(record: CallRecord, session: Session) -> Check:
    if not enabled():
        return Check(
            id="validity.persona_judge",
            group="validity",
            label="Persona judge (LLM, advisory)",
            passed=True,
            severity="info",
            what_happened="skipped (GF_PERSONA_JUDGE=0 or no OPENAI_API_KEY)",
        )
    from openai import OpenAI

    c = session.caller
    lines = "\n".join(f"- {t.text}" for t in record.caller_turns) or "(nothing)"
    prompt = (
        f"You review a simulated phone caller. The caller was told to be {c.persona.name}, "
        f"style: {c.persona.style}. The ONLY facts it may use: {json.dumps(c.facts)}. "
        f"Goal: {c.goal}\n\nHere is everything the caller said:\n{lines}\n\n"
        "Judge strictly from these lines. in_character: it behaved as that person and never "
        "mentioned being an AI, a test or a simulation. used_only_known_facts: every order "
        "number, zip code, amount or address it stated is in the facts. tone_matches_style: "
        "the wording fits the stated style. Keep notes to one sentence."
    )
    try:
        client = OpenAI()
        resp = client.beta.chat.completions.parse(
            model=JUDGE_MODEL,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
            response_format=PersonaVerdict,
        )
        v = resp.choices[0].message.parsed
        ok = v.in_character and v.used_only_known_facts
        return Check(
            id="validity.persona_judge",
            group="validity",
            label="Persona judge (LLM, advisory)",
            passed=ok,
            severity="info",
            what_happened=f"in_character={v.in_character}, only_known_facts={v.used_only_known_facts}, tone_ok={v.tone_matches_style}: {v.notes}",
            why_it_matters="Second opinion on the deterministic character check; disagreements go to the label set.",
            evidence={"prompt_version": PROMPT_VERSION, "model": JUDGE_MODEL},
            value=v.model_dump(),
        )
    except Exception as e:  # noqa: BLE001 - judge infrastructure errors are not agent failures
        return Check(
            id="validity.persona_judge",
            group="validity",
            label="Persona judge (LLM, advisory)",
            passed=True,
            severity="info",
            what_happened=f"JudgeUnavailable: {e}",
        )
