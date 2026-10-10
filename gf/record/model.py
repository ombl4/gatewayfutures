"""CallRecord: the only input to scoring and reporting. Loads an attempt folder as written
by runner/call.py; every time is milliseconds relative to the audio origin (t0)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: int
    tool: str
    args: dict[str, Any]
    response: dict[str, Any]
    status: int
    ok: bool
    t_ms: int  # relative to t0
    duration_ms: int
    arg_problems: list[str] = Field(default_factory=list)
    fault: str | None = None


class CallerTurn(BaseModel):
    n: int
    text: str  # what the caller said (reference)
    t_start_ms: int
    t_end_ms: int
    interruption: bool = False


class AgentTurn(BaseModel):
    n: int
    text: str  # what the agent said (its own text)
    t_ms: int  # when the message landed (generation), audio start comes from the timeline
    interrupted: bool = False
    metrics: dict[str, Any] = Field(default_factory=dict)


def _audio_file(f: Path) -> str | None:
    """The recording: audio.wav from a run, or audio.mp3 when a run is committed to the
    repository without its WAV files (the MP3 plays and draws the waveform; the committed
    timeline.json is kept rather than rebuilt from lossy audio)."""
    for name in ("audio.wav", "audio.mp3"):
        if (f / name).exists():
            return str(f / name)
    return None


class CallRecord(BaseModel):
    folder: str
    call_id: str
    session_id: str
    attempt: int
    meta: dict[str, Any]
    t0_ms: int
    audio_path: str | None
    audio_duration_s: float
    caller_turns: list[CallerTurn]
    caller_heard: list[dict[str, Any]]  # what the caller heard the agent say, with t_ms
    agent_turns: list[AgentTurn]
    agent_heard: list[dict[str, Any]]  # agent STT finals: what the agent heard, with t_ms
    agent_user_messages: list[dict[str, Any]]  # merged user turns as the agent's LLM saw them
    tool_calls: list[ToolCall]
    backend_state: dict[str, Any]
    caller_result: dict[str, Any]
    agent_events: list[dict[str, Any]]
    caller_events: list[dict[str, Any]]
    ended_by: str | None
    end_reason: str

    @classmethod
    def load(cls, folder: str | Path) -> CallRecord:
        f = Path(folder)
        meta = _json(f / "meta.json")
        audio = _json(f / "audio.json", default={})
        caller = _json(f / "caller.json", default={})
        backend_log = _json(f / "backend_log.json", default=[])
        state = _json(f / "backend_state.json", default={})
        agent_events = _jsonl(f / "agent_events.jsonl")
        caller_events = _jsonl(f / "caller_events.jsonl")
        t0 = int(audio.get("t0_epoch_ms") or meta.get("started_ms") or 0)
        rel = lambda ts: int(ts) - t0  # noqa: E731

        caller_turns = [
            CallerTurn(
                n=i + 1,
                text=t["text"],
                t_start_ms=rel(t["ts_ms"]),
                t_end_ms=rel(t["end_ms"]),
                interruption=bool(t.get("interruption")),
            )
            for i, t in enumerate(caller.get("turns_said", []))
        ]
        caller_heard = [
            {"t_ms": rel(t["ts_ms"]), "text": t["text"]} for t in caller.get("turns_heard", [])
        ]
        agent_turns, agent_heard, agent_user = [], [], []
        for e in agent_events:
            k = e.get("kind")
            if k == "message" and e.get("role") == "assistant":
                agent_turns.append(
                    AgentTurn(
                        n=len(agent_turns) + 1,
                        text=e.get("text", ""),
                        t_ms=rel(e["ts_ms"]),
                        interrupted=bool(e.get("interrupted")),
                        metrics=e.get("metrics") or {},
                    )
                )
            elif k == "message" and e.get("role") == "user":
                agent_user.append({"t_ms": rel(e["ts_ms"]), "text": e.get("text", "")})
            elif k == "user_transcript_final":
                agent_heard.append({"t_ms": rel(e["ts_ms"]), "text": e.get("text", "")})
        tools = [
            ToolCall(
                id=e["id"],
                tool=e["tool"],
                args=e["args"],
                response=e["response"],
                status=e["status"],
                ok=e["ok"],
                t_ms=rel(e["ts_ms"]),
                duration_ms=e["duration_ms"],
                arg_problems=e.get("arg_problems", []),
                fault=e.get("fault"),
            )
            for e in backend_log
        ]
        return cls(
            folder=str(f),
            call_id=meta.get("call_id", f.name),
            session_id=meta.get("session_id", ""),
            attempt=int(meta.get("attempt", 1)),
            meta=meta,
            t0_ms=t0,
            audio_path=_audio_file(f),
            audio_duration_s=float(audio.get("duration_s") or 0.0),
            caller_turns=caller_turns,
            caller_heard=caller_heard,
            agent_turns=agent_turns,
            agent_heard=agent_heard,
            agent_user_messages=agent_user,
            tool_calls=tools,
            backend_state=state,
            caller_result=caller,
            agent_events=agent_events,
            caller_events=caller_events,
            ended_by=meta.get("ended_by"),
            end_reason=meta.get("end_reason", ""),
        )


def _json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        if default is None:
            raise FileNotFoundError(path)
        return default
    return json.loads(path.read_text())


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out
