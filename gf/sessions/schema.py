"""Session schema. A session file is the only input to a simulated call; its id is the hash
of its content, so a changed file is a different session."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from gf.config import ROOT


class Persona(BaseModel):
    name: str
    style: str = "polite, plain-spoken"
    accent: str = "en-US"


class CallerLLM(BaseModel):
    model: str = "gpt-4.1-mini"
    temperature: float = 0.0
    seed: int = 101


class Conditions(BaseModel):
    noise: str | None = None  # e.g. "cafe@15dB": noise type @ SNR in dB
    phone_line: bool = False  # 8 kHz band-limit
    packet_loss: float = 0.0  # fraction of 20 ms frames dropped
    low_quality_mic: bool = False
    interruptions: float = 0.0  # probability the caller barges in (reserved; see manifest)
    patience_s: float = 20.0  # cumulative dead air before the caller gives up


class Limits(BaseModel):
    max_turns: int = 12
    max_duration_s: float = 120.0  # keeps a 36-call matrix inside ~15 min at concurrency 4
    mutual_silence_reprompt_s: float = 4.0
    mutual_silence_abort_s: float = 8.0


class Caller(BaseModel):
    persona: Persona
    facts: dict[str, Any] = Field(default_factory=dict)
    goal: str
    stop_when: str = "the agent has confirmed the outcome you asked for, or clearly cannot help"
    opening: str | None = None  # fixed first line; when None the LLM opens
    voice: str = "aura-2-asteria-en"
    pace: float = 1.0
    llm: CallerLLM = Field(default_factory=CallerLLM)
    conditions: Conditions = Field(default_factory=Conditions)
    limits: Limits = Field(default_factory=Limits)


class ExpectedCall(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class ExpectedTools(BaseModel):
    required: list[ExpectedCall] = Field(default_factory=list)
    order: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class Expected(BaseModel):
    outcome: str
    tool_calls: ExpectedTools = Field(default_factory=ExpectedTools)
    final_state: list[str] = Field(default_factory=list)
    notes: str = ""


class Session(BaseModel):
    id: str = ""
    title: str
    caller: Caller
    fixtures: str = "orders_basic"
    faults: list[dict[str, Any]] = Field(default_factory=list)
    expected: Expected
    engine: Literal["gf-caller", "livekit-simulate"] = "gf-caller"
    source_path: str = ""

    def content_hash(self) -> str:
        """Hash of the parsed session (defaults filled in), so a file written back out with
        every default spelled out hashes the same as the original."""
        body = self.model_dump(exclude={"id", "source_path"})
        canon = yaml.safe_dump(body, sort_keys=True, allow_unicode=True)
        return hashlib.sha256(canon.encode()).hexdigest()[:12]

    @model_validator(mode="after")
    def _check_tools(self) -> Session:
        known = {"lookup_order", "issue_refund", "update_shipping_address", "escalate_to_human"}
        named = (
            {c.tool for c in self.expected.tool_calls.required}
            | set(self.expected.tool_calls.order)
            | set(self.expected.tool_calls.forbidden)
        )
        unknown = named - known
        if unknown:
            raise ValueError(f"expected block names unknown tools: {sorted(unknown)}")
        return self

    @classmethod
    def load(cls, path: str | Path, *, verify_id: bool = True) -> Session:
        path = Path(path)
        raw = yaml.safe_load(path.read_text())
        stored = raw.pop("id", None)
        raw["source_path"] = str(path)
        session = cls(**raw)
        expected_id = session.content_hash()
        if verify_id and stored and stored != expected_id:
            raise ValueError(
                f"{path.name}: id {stored} does not match content hash {expected_id}; "
                "sessions are immutable, create a new file instead of editing"
            )
        session.id = expected_id
        fixture = ROOT / "fixtures" / f"{session.fixtures}.yaml"
        if not fixture.exists():
            raise ValueError(f"{path.name}: fixture {session.fixtures!r} not found")
        return session

    def dump(self) -> str:
        data = self.model_dump(exclude={"source_path"})
        data["id"] = self.content_hash()
        return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def load_all(folder: Path, include_retired: bool = True) -> list[Session]:
    """Hand-written sessions in `folder`, generated ones in `folder/generated/` and, unless
    excluded, retired ones in `folder/retired/`. Sessions are immutable and never deleted:
    a retired session is no longer run or offered, but old runs that used it still load."""
    paths = sorted(folder.glob("*.yaml")) + sorted((folder / "generated").glob("*.yaml"))
    if include_retired:
        paths += sorted((folder / "retired").glob("*.yaml"))
    return [Session.load(p) for p in paths]


def is_retired(session: Session) -> bool:
    return "/retired/" in (session.source_path or "").replace("\\", "/")
