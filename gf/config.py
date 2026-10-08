"""Settings loaded from the environment (.env) and the thresholds file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# The LiveKit plugins (OpenAI, Deepgram) and the LiveKit SDK read their keys from the
# environment, so .env is loaded into os.environ as soon as gf is imported.
load_dotenv(ROOT / ".env", override=False)


class Settings(BaseSettings):
    """Keys and endpoints. Every field maps to an environment variable of the same name."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""
    deepgram_api_key: str = ""
    openai_api_key: str = ""

    backend_url: str = "http://localhost:8080"
    runs_dir: Path = ROOT / "runs"
    sessions_dir: Path = ROOT / "sessions"

    agent_name: str = "gf-support-agent"
    caller_name: str = "gf-sim-caller"


class Thresholds(BaseModel):
    """Every flag threshold used by scoring and reports, in one place."""

    latency_p95_warn_s: float = 2.0
    latency_p95_fail_s: float = 3.5
    dead_air_gap_s: float = 3.0
    talk_over_max: int = 2
    barge_in_stop_s: float = 1.0
    repeats_max: int = 2
    filler_tool_latency_ms: int = 700
    repeated_question_similarity: float = 0.85
    repeated_question_window_s: float = 15.0
    mutual_silence_watchdog_s: float = 10.0
    rate_green: float = 0.95
    rate_amber: float = 0.66


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def thresholds(path: Path | None = None) -> Thresholds:
    path = path or ROOT / "thresholds.yaml"
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    return Thresholds(**(data or {}))
