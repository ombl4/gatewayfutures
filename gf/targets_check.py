"""Connection test for an agent under test (spec T9.3).

Create a room in the target's LiveKit project, dispatch its agent with a test call id, join
as a caller, and wait for the agent's audio. The verdict names the first thing that failed,
in the order a person would check it:

    no_credentials   the target has no server url, key or secret anywhere
    api_refused      the project rejected the key (room creation failed)
    dispatch_refused dispatch failed (wrong agent name or no permission)
    never_joined     dispatched, but no agent participant appeared in time
    joined_silent    the agent joined but published no audible speech in time
    ok               the agent joined and spoke

The result is stored next to the target record as `<id>.last-check.json` (gitignored) so
the UI can show when the target was last checked and what happened.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gf.config import settings
from gf.targets import MissingSecrets, Target, targets_dir

log = logging.getLogger("gf.targets")
CHECK_IDENTITY = "gf-check"
REASONS = {
    "no_credentials": "No server URL, API key or secret is configured for this target.",
    "api_refused": "The LiveKit project refused the API key (could not create a room).",
    "dispatch_refused": (
        "The project accepted the key but refused the dispatch: check the agent name."
    ),
    "never_joined": (
        "The dispatch was accepted but no agent joined the room in time: is the worker running?"
    ),
    "joined_silent": (
        "The agent joined but did not speak: it may be waiting for the caller to talk first."
    ),
    "ok": "The agent joined and greeted the caller.",
}


def verdict(
    *,
    credentials: bool,
    room_created: bool,
    dispatched: bool,
    joined_ms: int | None,
    spoke_ms: int | None,
) -> tuple[bool, str]:
    """(ok, reason code) from what happened, in check order."""
    if not credentials:
        return False, "no_credentials"
    if not room_created:
        return False, "api_refused"
    if not dispatched:
        return False, "dispatch_refused"
    if joined_ms is None:
        return False, "never_joined"
    if spoke_ms is None:
        return False, "joined_silent"
    return True, "ok"


def result_path(target_id: str) -> Path:
    return targets_dir() / f"{target_id}.last-check.json"


def last_result(target_id: str) -> dict[str, Any] | None:
    p = result_path(target_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except ValueError:
        return None


def store(target_id: str, result: dict[str, Any]) -> Path:
    p = result_path(target_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result, indent=2, default=str))
    return p


async def connection_test(target: Target, *, timeout_s: float = 20.0) -> dict[str, Any]:
    """Run the check against the target and store the result. Never raises for a failing
    target: the failure is the result."""
    import numpy as np
    from livekit import api, rtc

    from gf.caller.audio import SAMPLE_RATE, rms_db

    started = time.time()
    state: dict[str, Any] = {
        "credentials": False,
        "room_created": False,
        "dispatched": False,
        "joined_ms": None,
        "spoke_ms": None,
        "agent_identity": None,
        "error": None,
    }
    check_id = f"check-{uuid.uuid4().hex[:8]}"
    room_name = f"gf-{check_id}"
    try:
        url, key, secret = target.credentials()
        state["credentials"] = True
    except MissingSecrets as e:
        state["error"] = str(e)
        return _finish(target, state, started, check_id)
    lk = api.LiveKitAPI(url, key, secret)
    room = rtc.Room()
    joined = asyncio.Event()
    spoke = asyncio.Event()
    pumps: list[asyncio.Task] = []
    try:
        try:
            await lk.room.create_room(api.CreateRoomRequest(name=room_name, empty_timeout=60))
            state["room_created"] = True
        except Exception as e:  # noqa: BLE001
            state["error"] = f"{type(e).__name__}: {e}"
            return _finish(target, state, started, check_id)
        record_dir = settings().runs_dir / "_checks" / check_id
        try:
            await lk.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=target.agent_name,
                    room=room_name,
                    metadata=target.dispatch_metadata(
                        call_id=check_id,
                        record_dir=str(record_dir),
                        agent_variant="",
                        sandbox_url=settings().backend_url,
                        session_id="connection-check",
                    ),
                )
            )
            state["dispatched"] = True
        except Exception as e:  # noqa: BLE001
            state["error"] = f"{type(e).__name__}: {e}"
            return _finish(target, state, started, check_id)

        async def pump(track: rtc.Track) -> None:
            stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=1)
            try:
                async for ev in stream:
                    samples = np.frombuffer(ev.frame.data, dtype=np.int16)
                    if rms_db(samples) > -45:
                        if state["spoke_ms"] is None:
                            state["spoke_ms"] = int((time.time() - started) * 1000)
                        spoke.set()
                        return
            finally:
                await stream.aclose()

        @room.on("participant_connected")
        def _joined(p: rtc.RemoteParticipant):
            if state["joined_ms"] is None:
                state["joined_ms"] = int((time.time() - started) * 1000)
                state["agent_identity"] = p.identity
            joined.set()

        @room.on("track_subscribed")
        def _track(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                if state["joined_ms"] is None:
                    state["joined_ms"] = int((time.time() - started) * 1000)
                    state["agent_identity"] = participant.identity
                joined.set()
                pumps.append(asyncio.ensure_future(pump(track)))

        token = (
            api.AccessToken(key, secret)
            .with_identity(CHECK_IDENTITY)
            .with_name("Connection check")
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .to_jwt()
        )
        try:
            await room.connect(url, token)
        except Exception as e:  # noqa: BLE001
            state["error"] = f"{type(e).__name__}: {e}"
            return _finish(target, state, started, check_id)
        for p in room.remote_participants.values():
            if p.identity != CHECK_IDENTITY:
                state["joined_ms"] = state["joined_ms"] or 0
                state["agent_identity"] = p.identity
                joined.set()
        deadline = started + timeout_s
        try:
            await asyncio.wait_for(joined.wait(), max(0.1, deadline - time.time()))
            await asyncio.wait_for(spoke.wait(), max(0.1, deadline - time.time()))
        except TimeoutError:
            pass
    finally:
        for t in pumps:
            t.cancel()
        try:
            await room.disconnect()
        except Exception:  # noqa: BLE001
            pass
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:  # noqa: BLE001
            pass
        await lk.aclose()
    return _finish(target, state, started, check_id)


def _finish(target: Target, state: dict[str, Any], started: float, check_id: str) -> dict[str, Any]:
    ok, reason = verdict(
        credentials=state["credentials"],
        room_created=state["room_created"],
        dispatched=state["dispatched"],
        joined_ms=state["joined_ms"],
        spoke_ms=state["spoke_ms"],
    )
    result = {
        "target_id": target.id,
        "ok": ok,
        "reason": reason,
        "message": REASONS[reason],
        "error": state["error"],
        "agent_identity": state["agent_identity"],
        "joined_ms": state["joined_ms"],
        "spoke_ms": state["spoke_ms"],
        "took_ms": int((time.time() - started) * 1000),
        "check_id": check_id,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    store(target.id, result)
    return result


def connection_test_sync(target: Target, *, timeout_s: float = 20.0) -> dict[str, Any]:
    return asyncio.run(connection_test(target, timeout_s=timeout_s))
