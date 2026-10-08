"""Capture everything the agent session emits into one JSONL file (and the room data channel).

This is the agent's half of the call record. Tool calls are taken from
`function_tools_executed`, never from conversation items.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from livekit import rtc
from livekit.agents import AgentSession

from gf.util import now_ms

log = logging.getLogger("gf.agent.events")
DATA_TOPIC = "gf.events"


class EventRecorder:
    def __init__(self, path: Path, room: rtc.Room | None = None, call_id: str = ""):
        self.path = path
        self.room = room
        self.call_id = call_id
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a")
        self.t0_ms = now_ms()

    def emit(self, kind: str, **data: Any) -> None:
        record = {"ts_ms": now_ms(), "kind": kind, "call_id": self.call_id, **data}
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()
        if self.room is not None:
            try:
                payload = json.dumps(record, default=str).encode()
                coro = self.room.local_participant.publish_data(
                    payload, topic=DATA_TOPIC, reliable=True
                )
                asyncio.ensure_future(coro)  # publish_data is a coroutine in livekit-rtc 1.x
            except Exception as e:  # noqa: BLE001 - never let telemetry break the call
                log.debug("publish_data failed: %s", e)

    def close(self) -> None:
        self._fh.close()

    def attach(self, session: AgentSession) -> None:
        """Subscribe to every event the scorer needs."""

        @session.on("user_input_transcribed")
        def _on_user(ev):
            if ev.is_final:
                self.emit("user_transcript_final", text=ev.transcript, speaker_id=ev.speaker_id)

        @session.on("conversation_item_added")
        def _on_item(ev):
            item = ev.item
            if getattr(item, "type", None) != "message":
                return
            self.emit(
                "message",
                role=item.role,
                text=item.text_content or "",
                interrupted=getattr(item, "interrupted", False),
                metrics=dict(getattr(item, "metrics", {}) or {}),
            )

        @session.on("function_tools_executed")
        def _on_tools(ev):
            for call, out in ev.zipped():
                self.emit(
                    "tool_call",
                    call_id=call.call_id,
                    name=call.name,
                    arguments=_json_or_text(call.arguments),
                    output=out.output,
                    is_error=out.is_error,
                )

        @session.on("metrics_collected")
        def _on_metrics(ev):
            m = ev.metrics
            self.emit("metrics", metric_type=m.type, **_metric_fields(m))

        @session.on("agent_state_changed")
        def _on_agent_state(ev):
            self.emit("agent_state", old=ev.old_state, new=ev.new_state)

        @session.on("user_state_changed")
        def _on_user_state(ev):
            self.emit("user_state", old=ev.old_state, new=ev.new_state)

        @session.on("speech_created")
        def _on_speech(ev):
            self.emit("speech_created", source=ev.source, user_initiated=ev.user_initiated)

        @session.on("agent_false_interruption")
        def _on_false_interruption(ev):
            self.emit("false_interruption")

        @session.on("error")
        def _on_error(ev):
            self.emit("error", error=str(getattr(ev, "error", ev)))

        @session.on("close")
        def _on_close(ev):
            self.emit("session_close", reason=str(getattr(ev, "reason", "")))


def _json_or_text(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _metric_fields(m: Any) -> dict[str, Any]:
    wanted = (
        "timestamp",
        "duration",
        "ttft",
        "ttfb",
        "audio_duration",
        "end_of_utterance_delay",
        "transcription_delay",
        "on_user_turn_completed_delay",
        "speech_id",
        "cancelled",
        "completion_tokens",
        "prompt_tokens",
        "characters_count",
    )
    return {k: getattr(m, k) for k in wanted if hasattr(m, k)}
