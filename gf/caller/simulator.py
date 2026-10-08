"""The simulated caller: a LiveKit Agent that plays a persona over real audio.

It hears the support agent through its own STT, decides what to say with an LLM at
temperature 0 and a fixed seed, speaks through Deepgram TTS, and applies the session's audio
conditions to every outgoing frame. It only ENDS the call (`end_call`); pass/fail is decided
later from the call record, never by the caller.

Pattern adapted from meetocean-ai/assistant tests/voice/patient_simulator.py.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncGenerator, AsyncIterable
from dataclasses import dataclass, field

import aiohttp
import numpy as np
from livekit import rtc
from livekit.agents import Agent, ModelSettings, RunContext, function_tool
from livekit.plugins import deepgram, openai

from gf.caller.conditions import ConditionChain
from gf.sessions.schema import Caller
from gf.util import now_ms

log = logging.getLogger("gf.caller")


@dataclass
class CallerResult:
    ended_by: str | None = None  # caller | agent_left | timeout | mutual_silence | max_turns
    end_reason: str = ""
    goal_met: bool | None = None
    gave_up: bool = False
    summary: str = ""
    turns_heard: list[dict] = field(default_factory=list)  # what the caller heard the agent say
    turns_said: list[dict] = field(default_factory=list)  # what the caller said (reference text)
    heard_latency: list[dict] = field(default_factory=list)
    broke_character: bool = False
    repeats: int = 0
    started_ms: int = 0
    ended_ms: int = 0
    interruptions_planned: int = 0
    interruptions_made: int = 0
    interruptions: list[dict] = field(default_factory=list)


class HeardLatency:
    """Caller's-ear response latency from the caller session's own state events."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._open: dict | None = None

    def on_agent_state(self, old: str, new: str, t_ms: int) -> None:
        # the caller IS the "agent" of its own session: speaking -> anything else = it fell silent
        if old == "speaking" and new != "speaking":
            row = {"n": len(self.rows) + 1, "caller_stop_ms": t_ms, "agent_heard_ms": None}
            self.rows.append(row)
            self._open = row

    def on_user_state(self, old: str, new: str, t_ms: int) -> None:
        # its "user" is the support agent: VAD hearing it start
        if new == "speaking" and old != "speaking" and self._open is not None:
            self._open["agent_heard_ms"] = t_ms
            self._open["response_ms"] = t_ms - self._open["caller_stop_ms"]
            self._open = None


def build_prompt(c: Caller) -> str:
    facts = "\n".join(f"- {k}: {v}" for k, v in c.facts.items()) or "- (none)"
    opening = (
        f'Your first line, said as soon as the agent finishes greeting you: "{c.opening}"'
        if c.opening
        else "Open the call by saying why you are calling, in one sentence."
    )
    return f"""You are {c.persona.name}, a customer calling Gateway Goods support on the phone.
Persona: {c.persona.style}. Accent: {c.persona.accent}.

The ONLY facts you know (never invent others, never change them):
{facts}

YOUR GOAL: {c.goal}
{opening}

RULES:
- You are a real person on a phone call. Short, natural sentences. One thing at a time.
- Say order numbers as "G W" followed by the five digits spoken one at a time. Say zip codes
  digit by digit. Say dollar amounts in words ("eighty-nine ninety-nine").
- Answer the agent's questions honestly from your facts. If asked for something you do not
  know, say you do not have it.
- If the agent reads something back and it is correct, confirm clearly with "Yes". If it is
  wrong, correct it.
- If the agent did not understand you, repeat yourself once, slower.
- Stay in character. Never mention that you are an AI, a test, or a simulation.
- When {c.stop_when}: say thank you and goodbye, then call end_call with goal_met=true.
- If the agent clearly cannot help, loops, or you are stuck after several tries: say goodbye
  and call end_call with goal_met=false and giving_up=true.
- Keep to at most {c.limits.max_turns} of your own turns. You MUST call end_call to finish;
  do not just stop talking."""


class InterruptPlanner:
    """Decides, reproducibly, whether and when the caller barges in on an agent turn.

    `p` is the session's `conditions.interruptions`; draws come from a seeded generator so the
    same session interrupts at the same turns with the same delays on every run."""

    MIN_DELAY_S = 1.2
    MAX_DELAY_S = 2.5

    def __init__(self, p: float, seed: int):
        self.p = max(0.0, min(1.0, float(p)))
        self._rng = random.Random(f"{seed}:interrupt")

    def decide(self) -> float | None:
        """Delay in seconds after the agent starts speaking, or None for no interruption."""
        if self.p <= 0:
            return None
        if self._rng.random() >= self.p:
            return None
        return round(self._rng.uniform(self.MIN_DELAY_S, self.MAX_DELAY_S), 2)


INTERRUPT_INSTRUCTIONS = (
    "The support agent is still talking. Interrupt right now with ONE short sentence, in "
    "character, that pushes toward your goal or asks them to get to the point (for example "
    "'Sorry, I just need the refund.' or 'Yes, yes, that is right, go ahead.'). Do not repeat "
    "facts you already gave. Do not call end_call now."
)


class CallerAgent(Agent):
    def __init__(self, spec: Caller, http_session: aiohttp.ClientSession, seed_offset: int = 0):
        self.spec = spec
        self.result = CallerResult()
        self.done = asyncio.Event()
        self.heard = HeardLatency()
        self._turns = 0
        self._chain: ConditionChain | None = None
        self._last_said: str = ""
        seed = spec.llm.seed + seed_offset
        self._planner = InterruptPlanner(spec.conditions.interruptions, seed)
        self._interrupt_task: asyncio.Task | None = None
        self._agent_speaking = False
        self._pending_interruption = False
        super().__init__(
            instructions=build_prompt(spec),
            llm=openai.LLM(
                model=spec.llm.model, temperature=spec.llm.temperature, extra_body={"seed": seed}
            ),
            stt=deepgram.STT(model="nova-3", language="en-US", http_session=http_session),
            tts=deepgram.TTS(model=spec.voice, speed=spec.pace, http_session=http_session),
        )

    async def on_enter(self) -> None:
        self.result.started_ms = now_ms()

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        """The "user" here is the support agent: one completed turn of its speech, as heard."""
        text = getattr(new_message, "text_content", None) or ""
        self.result.turns_heard.append({"ts_ms": now_ms(), "text": text})

    async def tts_node(
        self, text: AsyncIterable[str], model_settings: ModelSettings
    ) -> AsyncGenerator[rtc.AudioFrame, None]:
        """Default TTS, then the session's audio conditions on every frame before publish."""
        said: list[str] = []

        async def tee() -> AsyncGenerator[str, None]:
            async for chunk in text:
                said.append(chunk)
                yield chunk

        self._turns += 1
        t_start = now_ms()
        async for frame in Agent.default.tts_node(self, tee(), model_settings):
            if self._chain is None:
                self._chain = ConditionChain(
                    self.spec.conditions, frame.sample_rate, seed=self.spec.llm.seed
                )
            pcm = np.frombuffer(frame.data, dtype=np.int16)
            out = self._chain.process(pcm)
            if out is None:
                out = np.zeros_like(pcm)  # a dropped packet is silence of the same length
            yield rtc.AudioFrame(
                out.tobytes(), frame.sample_rate, frame.num_channels, frame.samples_per_channel
            )
        line = "".join(said).strip()
        if line:
            norm = line.lower()
            if norm == self._last_said:
                self.result.repeats += 1
            self._last_said = norm
            self.result.turns_said.append(
                {
                    "ts_ms": t_start,
                    "end_ms": now_ms(),
                    "text": line,
                    "interruption": self._pending_interruption,
                }
            )
            if self._pending_interruption:
                self.result.interruptions_made += 1
                self.result.interruptions.append({"ts_ms": t_start, "text": line})
            self._pending_interruption = False
        if self._turns >= self.spec.limits.max_turns and not self.done.is_set():
            self._finish("max_turns", "turn limit reached", goal_met=False)

    def on_agent_speaking(self, speaking: bool) -> None:
        """Called by the runner on the agent's speaking state. Plans a barge-in when the agent
        starts a turn; cancels it when the agent finishes first."""
        self._agent_speaking = speaking
        if speaking:
            if self.done.is_set() or (self._interrupt_task and not self._interrupt_task.done()):
                return
            delay = self._planner.decide()
            if delay is None:
                return
            self.result.interruptions_planned += 1
            self._interrupt_task = asyncio.create_task(self._interrupt_after(delay))
        elif self._interrupt_task and not self._interrupt_task.done():
            self._interrupt_task.cancel()

    async def _interrupt_after(self, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
        except asyncio.CancelledError:
            return
        if not self._agent_speaking or self.done.is_set():
            return
        self._pending_interruption = True
        try:
            # Uninterruptible: the agent's continuing audio must not cancel the interjection.
            self.session.generate_reply(
                instructions=INTERRUPT_INSTRUCTIONS, allow_interruptions=False
            )
        except Exception:  # noqa: BLE001 - the session may be closing
            self._pending_interruption = False

    def conditions_report(self) -> dict | None:
        return self._chain.describe() if self._chain else None

    def _finish(self, ended_by: str, reason: str, *, goal_met: bool | None, gave_up=False) -> None:
        if self.done.is_set():
            return
        r = self.result
        r.ended_by, r.end_reason, r.goal_met, r.gave_up = ended_by, reason, goal_met, gave_up
        r.ended_ms = now_ms()
        self.done.set()

    def end_from_outside(self, ended_by: str, reason: str) -> None:
        self._finish(ended_by, reason, goal_met=None)

    @function_tool()
    async def end_call(
        self, context: RunContext, summary: str, goal_met: bool, giving_up: bool = False
    ) -> str:
        """Hang up. Call this only after you have said goodbye.

        Args:
            summary: One sentence on what happened (e.g. "Refund of $89.99 confirmed").
            goal_met: True if the agent confirmed what you called for.
            giving_up: True if you are ending because the agent could not help.
        """
        if len(self.result.turns_heard) < 2:
            return (
                "Not yet: the agent has not answered your request. Say what you called about, "
                "wait for the answer, say goodbye, then call end_call."
            )
        self.result.summary = summary
        self._finish("caller", summary, goal_met=goal_met, gave_up=giving_up)
        return "Goodbye."
