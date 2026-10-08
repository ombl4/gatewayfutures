"""The support agent worker: a LiveKit Agents 1.x server registered as `gf-support-agent`.

Explicit dispatch only (because `agent_name` is set): it joins a room only when the runner
or `lk dispatch create` asks it to. Job metadata (JSON) may carry `call_id` and `record_dir`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    EndpointingOptions,
    InterruptionOptions,
    JobContext,
    TurnHandlingOptions,
    cli,
)
from livekit.agents.voice.room_io import AudioInputOptions, RoomOptions
from livekit.plugins import deepgram, noise_cancellation, openai, silero

from gf.agent.config import AgentConfig, agent_config
from gf.agent.events import EventRecorder
from gf.agent.tools import ALL_TOOLS, BackendClient, CallContext, hang_up
from gf.config import settings

log = logging.getLogger("gf.agent")


class SupportAgent(Agent):
    def __init__(self, cfg: AgentConfig):
        super().__init__(instructions=cfg.instructions, tools=ALL_TOOLS)
        self.cfg = cfg

    async def on_enter(self) -> None:
        # The agent speaks first, so the caller never opens into silence.
        self.session.say(self.cfg.greeting, allow_interruptions=True)


def build_session(cfg: AgentConfig, call_ctx: CallContext, *, voice: bool = True) -> AgentSession:
    """One place that turns config into a session. `voice=False` gives a text-only session
    (LLM + tools) for tests."""
    turn_handling = TurnHandlingOptions(
        endpointing=EndpointingOptions(
            min_delay=cfg.voice.min_endpointing_delay_s,
            max_delay=cfg.voice.max_endpointing_delay_s,
        ),
        interruption=InterruptionOptions(enabled=cfg.voice.allow_interruptions),
    )
    kwargs: dict = {
        "llm": openai.LLM(model=cfg.models.llm.model, temperature=cfg.models.llm.temperature),
        "userdata": call_ctx,
        "turn_handling": turn_handling,
        "user_away_timeout": cfg.voice.silence_hangup_s,
    }
    if voice:
        from livekit.agents import inference

        # LiveKit's hosted end-of-turn model: no local model files to download.
        turn_handling["turn_detection"] = inference.TurnDetector()
        kwargs.update(
            stt=deepgram.STT(model=cfg.models.stt.model, keyterm=cfg.models.stt.keyterms),
            tts=deepgram.TTS(model=cfg.models.tts.model),
            vad=silero.VAD.load(),
        )
    return AgentSession(**kwargs)


def _job_metadata(ctx: JobContext) -> dict:
    try:
        return json.loads(ctx.job.metadata or "{}")
    except ValueError:
        return {}


async def entrypoint(ctx: JobContext) -> None:
    cfg = agent_config()
    meta = _job_metadata(ctx)
    call_id = meta.get("call_id") or ctx.room.name
    record_dir = Path(meta.get("record_dir") or settings().runs_dir / "_adhoc" / call_id)
    record_dir.mkdir(parents=True, exist_ok=True)

    await ctx.connect()

    call_ctx = CallContext(call_id=call_id, config=cfg, backend=BackendClient(call_id))
    session = build_session(cfg, call_ctx)
    recorder = EventRecorder(record_dir / "agent_events.jsonl", room=ctx.room, call_id=call_id)
    recorder.attach(session)
    recorder.emit("job_start", room=ctx.room.name, config_hash=cfg.config_hash, meta=meta)

    @session.on("user_state_changed")
    def _on_user_state(ev):
        if ev.new_state == "away" and not call_ctx.ended:
            call_ctx.ended, call_ctx.end_reason = True, "caller_silent"
            asyncio.ensure_future(_say_goodbye_and_leave(session, cfg.goodbye_line))

    async def _max_duration():
        await asyncio.sleep(cfg.voice.max_call_duration_s)
        if not call_ctx.ended:
            call_ctx.ended, call_ctx.end_reason = True, "max_duration"
            await _say_goodbye_and_leave(session, cfg.goodbye_line)

    watchdog = asyncio.ensure_future(_max_duration())

    async def _on_shutdown(reason: str = "") -> None:
        watchdog.cancel()
        recorder.emit("job_end", reason=call_ctx.end_reason or reason)
        try:
            report = ctx.make_session_report()
            (record_dir / "agent_session_report.json").write_text(
                json.dumps(report.to_dict(), default=str)
            )
        except Exception as e:  # noqa: BLE001
            log.warning("session report unavailable: %s", e)
        recorder.close()
        await call_ctx.backend.aclose()

    ctx.add_shutdown_callback(_on_shutdown)

    await session.start(
        agent=SupportAgent(cfg),
        room=ctx.room,
        record=True,
        room_options=RoomOptions(
            audio_input=AudioInputOptions(
                noise_cancellation=noise_cancellation.BVC()
                if cfg.voice.noise_cancellation == "bvc"
                else None
            )
        ),
    )


async def _say_goodbye_and_leave(session: AgentSession, line: str) -> None:
    try:
        await session.say(line, allow_interruptions=False).wait_for_playout()
    finally:
        await hang_up()


server = AgentServer()
server.rtc_session(entrypoint, agent_name=settings().agent_name)


def main() -> None:
    # LiveKit Cloud's telemetry exporter rate-limits (HTTP 429) and logs every batch; it
    # carries nothing we use, so keep it out of the worker log.
    for name in ("opentelemetry.exporter.otlp", "opentelemetry.sdk"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    cli.run_app(server)


if __name__ == "__main__":
    main()


__all__ = ["SupportAgent", "build_session", "entrypoint", "server", "main", "rtc"]
