"""Voice agent providers (spec T4.6 / T4.6a).

The agent under test runs on a provider: today LiveKit Agents on LiveKit Cloud. The
registry below is the single list of providers the platform knows: the one that runs,
and the ones the seam is designed for. Every entry states the three obligations a
provider has to meet, which is what the Providers page and the design note explain.

Switching providers is a config change (`provider:` in `gf/agent/config.yaml`), never a UI
action, so it changes the environment tag like any other agent change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

RUNNABLE = "runnable"  # the runner can make calls against it today
DESIGNED = "designed"  # seam mapped, module not written: the next-week plan covers it


@dataclass(frozen=True)
class ProviderInfo:
    key: str
    name: str
    kind: str
    reach: str  # how the simulated caller gets real audio to the agent
    events: str  # where tool calls and transcripts come from
    config_hash: str  # what the agent config hash is made of
    module: str  # where the implementation lives (or would)
    note: str = ""


REGISTRY: tuple[ProviderInfo, ...] = (
    ProviderInfo(
        key="livekit",
        name="LiveKit Agents",
        kind=RUNNABLE,
        reach="the caller joins the room the runner created; the agent is dispatched by name "
        "(explicit dispatch) with the call id in the job metadata",
        events="agent events on the room data channel (what it heard, what it said, tool "
        "start/end, latency metrics) plus the mock order system's request log",
        config_hash="hash of gf/agent/config.yaml and its tool definitions",
        module="gf/runner/call.py",
    ),
    ProviderInfo(
        key="pipecat",
        name="Pipecat",
        kind=DESIGNED,
        reach="a Pipecat bot on the LiveKit or Daily transport joins the same room the caller "
        "is in; any other transport gets a WebRTC/WebSocket client",
        events="function-call frames from the bot's observer hooks plus the order system's log",
        config_hash="hash of the bot's pipeline definition",
        module="gf/providers/pipecat.py",
        note="same room, same recording; only the event capture changes",
    ),
    ProviderInfo(
        key="telephony",
        name="Vapi · Retell · Bland · ElevenLabs Agents",
        kind=DESIGNED,
        reach="the caller's room gets a SIP trunk and dials the agent's number, or opens the "
        "provider's web-call session; recording and timing stay on the caller's side",
        events="the call-end webhook or call API (transcript with timestamps, tool-call "
        "records); the order system's log stays authoritative for what was done",
        config_hash="hash of the assistant definition fetched from the provider's API",
        module="gf/providers/vapi.py",
        note="one module per vendor behind the same three methods",
    ),
)


def get(key: str) -> ProviderInfo | None:
    return next((p for p in REGISTRY if p.key == key), None)


def current_key() -> str:
    """The provider the agent under test runs on, from the agent config."""
    from gf.agent.config import agent_config

    return agent_config().provider


def providers() -> list[dict]:
    """Registry rows for the UI, the current one marked."""
    cur = current_key()
    rows = []
    for p in REGISTRY:
        row = asdict(p)
        row["current"] = p.key == cur
        row["runnable"] = p.kind == RUNNABLE
        rows.append(row)
    return rows
