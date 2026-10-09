"""Persona library (personas/*.yaml): how a caller talks and what the line sounds like.

A persona is never *who* the caller is: the name and the facts stay with the session, because
they belong to the order. A session may reference a persona with `caller.persona_ref`; the
persona then fills style, accent, voice, pace and line conditions for every field the session
does not set itself. The session's content hash covers the resolved values, so editing a
persona changes the id of every session that uses it (results stay tied to one definition).

`derive(session, persona)` makes the matrix variant used by `gf run --persona ...`: the same
facts, goal and expectations, with the persona's style, accent, voice, pace and conditions
replacing the session's, as a new session with its own id.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from gf.config import ROOT
from gf.sessions.schema import Conditions, Session

PERSONAS_DIR = ROOT / "personas"
GROUPS = ("standard", "hard-line", "difficult")


class Persona(BaseModel):
    name: str
    group: str
    style: str
    accent: str = "en-US"
    voice: str = "aura-2-asteria-en"
    pace: float = 1.0
    conditions: Conditions = Field(default_factory=Conditions)


@lru_cache
def load_personas(folder: Path | None = None) -> dict[str, Persona]:
    folder = folder or PERSONAS_DIR
    out: dict[str, Persona] = {}
    for p in sorted(folder.glob("*.yaml")):
        raw = yaml.safe_load(p.read_text()) or {}
        out[p.stem] = Persona(name=p.stem, **raw)
    return out


def get_persona(name: str) -> Persona:
    personas = load_personas()
    if name not in personas:
        raise ValueError(f"unknown persona {name!r}; known: {', '.join(sorted(personas))}")
    return personas[name]


def in_group(group: str) -> list[str]:
    names = [n for n, p in load_personas().items() if p.group == group]
    if not names:
        raise ValueError(f"unknown persona group {group!r}; known: {', '.join(GROUPS)}")
    return names


def apply_to_raw(raw_caller: dict[str, Any], persona: Persona, *, force: bool = False) -> None:
    """Fill a raw session's caller block from a persona. Only fields the session does not
    set are filled, unless `force` (the matrix case, where the persona wins)."""
    pers = raw_caller.setdefault("persona", {})
    if not isinstance(pers, dict):
        pers = raw_caller["persona"] = {"name": str(pers)}
    for key, val in (("style", persona.style), ("accent", persona.accent)):
        if force or key not in pers:
            pers[key] = val
    for key, val in (("voice", persona.voice), ("pace", persona.pace)):
        if force or key not in raw_caller:
            raw_caller[key] = val
    cond = raw_caller.setdefault("conditions", {})
    if not isinstance(cond, dict):
        cond = raw_caller["conditions"] = {}
    for key, val in persona.conditions.model_dump().items():
        if force or key not in cond:
            cond[key] = val
    raw_caller["persona_ref"] = persona.name


def derive(session: Session, persona_name: str) -> Session:
    """The session as spoken by `persona_name`: same name, facts, goal and expectations."""
    persona = get_persona(persona_name)
    raw = session.model_dump(exclude={"id", "source_path"})
    apply_to_raw(raw["caller"], persona, force=True)
    raw["title"] = f"{session.title} · as {persona_name}"
    raw["base_session"] = session.id
    derived = Session(**raw)
    derived.id = derived.content_hash()
    return derived


def groups() -> dict[str, list[Persona]]:
    out: dict[str, list[Persona]] = {g: [] for g in GROUPS}
    for p in load_personas().values():
        out.setdefault(p.group, []).append(p)
    return out
