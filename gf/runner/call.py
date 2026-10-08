"""Run one simulated call and write its record folder.

Folder layout (every timestamp is epoch ms; `audio.json` carries the recorder's origin):
  audio.wav               stereo, L = caller as sent, R = agent as heard
  audio.json              recorder metadata
  caller_events.jsonl     the caller session's events (what it heard, said, states)
  caller.json             caller result: end reason, goal_met, turns said/heard, heard latency
  agent_events.jsonl      written by the agent worker (job metadata points it here)
  agent_session_report.json   written by the agent worker
  backend_log.json        every tool request the backend saw for this call
  backend_state.json      backend state before and after
  meta.json               session, call id, engine, parameters, config hash, timings
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

import aiohttp
import httpx
from livekit import api, rtc
from livekit.agents import AgentSession
from livekit.agents.voice.room_io import RoomOptions
from livekit.plugins import silero

from gf.agent.config import agent_config
from gf.agent.events import EventRecorder
from gf.caller.recorder import CallRecorder
from gf.caller.simulator import CallerAgent
from gf.config import settings
from gf.sessions.schema import Session
from gf.util import now_ms

log = logging.getLogger("gf.runner")
CALLER_IDENTITY = "caller"
GOODBYE_GRACE_S = 4.0
AGENT_RECORD_TIMEOUT_S = 20.0


async def run_call(session: Session, call_id: str, record_dir: Path, *, attempt: int = 1) -> dict:
    s = settings()
    cfg = agent_config()
    record_dir.mkdir(parents=True, exist_ok=True)
    room_name = f"gf-sim-{call_id}"
    meta: dict = {
        "call_id": call_id,
        "session_id": session.id,
        "session_title": session.title,
        "attempt": attempt,
        "engine": "gf-caller",
        "room": room_name,
        "agent_config_hash": cfg.config_hash,
        "agent_models": cfg.models.model_dump(),
        "caller": session.caller.model_dump(),
        "caller_params_unsupported": ["conditions.interruptions"],
        "fixtures": session.fixtures,
        "faults": session.faults,
        "started_ms": now_ms(),
    }

    lk = api.LiveKitAPI(s.livekit_url, s.livekit_api_key, s.livekit_api_secret)
    http = aiohttp.ClientSession()
    backend = httpx.AsyncClient(base_url=s.backend_url, timeout=15)
    caller = CallerAgent(session.caller, http_session=http, seed_offset=attempt - 1)
    recorder = CallRecorder(room_name, CALLER_IDENTITY)
    room = rtc.Room()
    sim: AgentSession | None = None
    events = EventRecorder(record_dir / "caller_events.jsonl", room=None, call_id=call_id)
    silence = {"last_audio_ms": now_ms(), "reprompted": False, "mutual_ms": 0}

    try:
        await backend.post(
            f"/calls/{call_id}/seed", json={"fixture": session.fixtures, "faults": session.faults}
        )
        await lk.room.create_room(api.CreateRoomRequest(name=room_name, empty_timeout=120))
        await lk.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=s.agent_name,
                room=room_name,
                metadata=json.dumps({"call_id": call_id, "record_dir": str(record_dir)}),
            )
        )
        token = (
            api.AccessToken(s.livekit_api_key, s.livekit_api_secret)
            .with_identity(CALLER_IDENTITY)
            .with_name(session.caller.persona.name)
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .to_jwt()
        )

        @room.on("participant_disconnected")
        def _left(p: rtc.RemoteParticipant):
            if p.identity != "gf-recorder":
                caller.end_from_outside("agent_left", f"{p.identity} left the room")

        @room.on("disconnected")
        def _room_gone(*_):
            caller.end_from_outside("room_closed", "room closed")

        recorder.t0 = time.time()
        await recorder.start()
        await room.connect(s.livekit_url, token)

        sim = AgentSession(vad=silero.VAD.load(), user_away_timeout=None)
        events.attach(sim)

        @sim.on("agent_state_changed")
        def _agent_state(ev):
            t = int(ev.created_at * 1000)
            caller.heard.on_agent_state(ev.old_state, ev.new_state, t)
            if ev.new_state == "speaking":
                silence["last_audio_ms"] = t

        @sim.on("user_state_changed")
        def _user_state(ev):
            t = int(ev.created_at * 1000)
            caller.heard.on_user_state(ev.old_state, ev.new_state, t)
            if ev.new_state == "speaking":
                silence["last_audio_ms"] = t

        await sim.start(
            agent=caller,
            room=room,
            room_options=RoomOptions(
                participant_kinds=[
                    rtc.ParticipantKind.PARTICIPANT_KIND_AGENT,
                    rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD,
                ]
            ),
        )
        events.emit("call_start", room=room_name)

        async def watchdog():
            lim = session.caller.limits
            while not caller.done.is_set():
                await asyncio.sleep(0.5)
                quiet_s = (now_ms() - silence["last_audio_ms"]) / 1000
                if quiet_s >= lim.mutual_silence_abort_s:
                    events.emit("mutual_silence_abort", quiet_s=round(quiet_s, 1))
                    caller.end_from_outside("mutual_silence", f"{quiet_s:.1f}s of mutual silence")
                elif quiet_s >= lim.mutual_silence_reprompt_s and not silence["reprompted"]:
                    silence["reprompted"] = True
                    events.emit("mutual_silence_reprompt", quiet_s=round(quiet_s, 1))
                    try:
                        sim.say("Hello? Are you still there?")
                    except Exception as e:  # noqa: BLE001
                        log.debug("re-prompt failed: %s", e)

        wd = asyncio.create_task(watchdog())
        try:
            await asyncio.wait_for(caller.done.wait(), timeout=session.caller.limits.max_duration_s)
        except TimeoutError:
            caller.end_from_outside(
                "timeout", f"max_duration {session.caller.limits.max_duration_s}s"
            )
        wd.cancel()
        await asyncio.sleep(GOODBYE_GRACE_S)  # let goodbyes play out before the room goes
    except Exception as e:  # noqa: BLE001
        log.error("call %s failed: %s", call_id, e, exc_info=True)
        meta["runner_error"] = str(e)
        caller.end_from_outside("runner_error", str(e))
    finally:
        events.emit("call_end", ended_by=caller.result.ended_by, reason=caller.result.end_reason)
        await recorder.stop()
        if sim is not None:
            try:
                await sim.aclose()
            except Exception:  # noqa: BLE001
                pass
        try:
            await room.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:  # noqa: BLE001
            pass

    # ---- collect ----
    audio_meta = recorder.write(record_dir / "audio.wav")
    (record_dir / "audio.json").write_text(json.dumps(audio_meta, indent=2))
    result = asdict(caller.result)
    result["heard_latency"] = caller.heard.rows
    result["conditions"] = caller.conditions_report()
    (record_dir / "caller.json").write_text(json.dumps(result, indent=2, default=str))
    try:
        log_ = (await backend.get(f"/calls/{call_id}/log")).json()
        state = (await backend.get(f"/calls/{call_id}/state")).json()
    except Exception as e:  # noqa: BLE001
        log_, state = [], {"error": str(e)}
    (record_dir / "backend_log.json").write_text(json.dumps(log_, indent=2))
    (record_dir / "backend_state.json").write_text(json.dumps(state, indent=2))
    agent_ok = await _wait_for_agent_record(record_dir)
    meta.update(
        ended_ms=now_ms(),
        ended_by=caller.result.ended_by,
        end_reason=caller.result.end_reason,
        goal_met=caller.result.goal_met,
        agent_record_complete=agent_ok,
        tool_calls=[(e["tool"], e["status"]) for e in log_],
    )
    (record_dir / "meta.json").write_text(json.dumps(meta, indent=2, default=str))
    events.close()
    await backend.aclose()
    await http.close()
    await lk.aclose()
    return meta


async def _wait_for_agent_record(record_dir: Path, timeout: float = AGENT_RECORD_TIMEOUT_S) -> bool:
    """The agent worker writes job_end from its shutdown callback after the room closes."""
    path = record_dir / "agent_events.jsonl"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and '"job_end"' in path.read_text()[-4000:]:
            return True
        await asyncio.sleep(0.5)
    return False
