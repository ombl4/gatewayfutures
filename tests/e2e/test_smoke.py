"""`make smoke`: one real simulated call end to end. Needs the order system, the agent worker
and the API keys (skips with a clear reason otherwise)."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import httpx
import pytest

from gf.config import ROOT, settings

pytestmark = pytest.mark.e2e


def _backend_up() -> bool:
    try:
        return httpx.get(f"{settings().backend_url}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(
    not _backend_up(), reason="order system not running (uvicorn gf.backend.app:app --port 8080)"
)
@pytest.mark.skipif(not settings().livekit_api_key, reason="no LiveKit keys in .env")
def test_one_call_end_to_end(tmp_path: Path):
    from gf.record.model import CallRecord
    from gf.record.timeline import write_timeline
    from gf.runner.call import run_call
    from gf.scoring.score import score_attempt
    from gf.sessions.schema import Session

    session = Session.load(ROOT / "sessions" / "refund-basic.yaml")
    call_id = f"smoke-{uuid.uuid4().hex[:6]}"
    folder = tmp_path / "smoke" / "1"
    meta = asyncio.run(run_call(session, call_id, folder, attempt=1))

    assert meta["agent_record_complete"], (
        "agent worker did not join or did not write its record (is `gf agent dev` running?)"
    )
    for name in (
        "audio.wav",
        "audio.json",
        "caller.json",
        "agent_events.jsonl",
        "backend_log.json",
        "backend_state.json",
        "meta.json",
    ):
        assert (folder / name).exists(), name
    record = CallRecord.load(folder)
    assert record.caller_turns and record.agent_turns
    assert record.audio_duration_s > 5
    timeline = write_timeline(folder)
    assert timeline["ux"]["agent_segments"] > 0 and timeline["ux"]["caller_segments"] > 0
    scores = score_attempt(folder, session)
    assert scores["valid"], scores["failure_reason"]
    assert [t for t, _ in scores["tool_calls"]][:1] == ["lookup_order"]
    json.dumps(scores)  # serialisable
