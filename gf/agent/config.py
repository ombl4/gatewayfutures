"""Agent configuration: one YAML file, loaded into a model and hashed."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.yaml"
DESCRIPTION_PATH = HERE / "description.md"


class STTConfig(BaseModel):
    vendor: str
    model: str
    keyterms: list[str] = Field(default_factory=list)


class LLMConfig(BaseModel):
    vendor: str
    model: str
    temperature: float = 0.3


class TTSConfig(BaseModel):
    vendor: str
    model: str


class Models(BaseModel):
    stt: STTConfig
    llm: LLMConfig
    tts: TTSConfig


class VoiceConfig(BaseModel):
    vad: str = "silero"
    turn_detection: str = "multilingual"
    noise_cancellation: str = "bvc"
    allow_interruptions: bool = True
    min_endpointing_delay_s: float = 0.5
    max_endpointing_delay_s: float = 3.0
    filler_after_ms: int = 700
    filler_text: str = "One moment while I check that."
    silence_hangup_s: float = 20.0
    max_call_duration_s: float = 300.0


class AgentConfig(BaseModel):
    name: str
    persona_name: str
    provider: str
    models: Models
    voice: VoiceConfig
    greeting: str
    handoff_line: str
    goodbye_line: str
    instructions: str
    tools: list[str]
    config_hash: str = ""
    description: str = ""

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> AgentConfig:
        raw = path.read_text()
        cfg = cls(**yaml.safe_load(raw))
        tools_src = (HERE / "tools.py").read_text() if (HERE / "tools.py").exists() else ""
        cfg.config_hash = hashlib.sha256((raw + tools_src).encode()).hexdigest()[:12]
        if DESCRIPTION_PATH.exists():
            cfg.description = DESCRIPTION_PATH.read_text().strip()
        return cfg


@lru_cache
def agent_config() -> AgentConfig:
    return AgentConfig.load()
