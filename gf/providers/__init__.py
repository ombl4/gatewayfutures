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


# How a new provider is added, day by day (deliverable: "how would you add a provider
# in the next week"). Shown on the Providers page and written out in docs/design-note.md.
WEEK_PLAN: tuple[tuple[str, str, str], ...] = (
    (
        "Day 1",
        "Lift the seam out of the runner",
        "Move what gf/runner/call.py does today behind a Provider protocol in "
        "gf/providers/base.py (agent_config, start_call, collect). The runner only uses the "
        "protocol. A FakeProvider in the tests proves the orchestration (folder layout, "
        "manifest, retry) without any network. Nothing a user sees changes.",
    ),
    (
        "Day 2",
        "Reach the new provider's agent with real audio",
        "Pipecat: run the bot on the LiveKit transport so it joins the caller's room. "
        "Telephony (Vapi, Retell, Bland): attach a SIP trunk to the caller's room and dial "
        "the agent's number. The caller, its audio conditions and the stereo recording are "
        "untouched, so every audio-based check (latency heard, dead air, talk-over, barge-in, "
        "WER) works on day two.",
    ),
    (
        "Day 3",
        "Point the provider's tools at the order system",
        "Register the four tools (lookup_order, issue_refund, update_shipping_address, "
        "escalate_to_human) as webhooks to the mock backend, carrying the call id. The backend "
        "log and final state then grade the call exactly as today: required calls, order, "
        "forbidden calls, final-state assertions and claimed-without-acting need nothing else.",
    ),
    (
        "Day 4",
        "Collect the provider's transcript and events",
        "Implement collect(): fetch the call-end webhook or call API (transcript with "
        "timestamps, tool-call records) and write agent_events.jsonl in the record format. "
        "Where the provider reports its own latency, map it to the heard-vs-reported "
        "breakdown; where it does not, the breakdown is simply absent for that provider.",
    ),
    (
        "Day 5",
        "Config hash, environment tag, and the first like-for-like run",
        "agent_config() hashes the assistant definition fetched from the provider's API, so "
        "the env tag changes when someone edits the agent in the vendor console. Run the core "
        "suite three times, compare with the LiveKit baseline on the same session set, and "
        "add the provider to the registry as runnable.",
    ),
)
