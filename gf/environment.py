"""Environment tags (spec T6.20): two short tags that say what a run was made with.

* `env-<8 hex>`: everything that shapes the agent and the measurement except the sessions:
  agent config hash and variant, STT/LLM/TTS model names, engine, scoring method and
  thresholds, the versions of livekit-agents, the LiveKit SDK, the OpenAI and Deepgram
  plugins, and Python.
* `set-<8 hex>`: the session files that were run.

Two runs are like-for-like when both tags match. Every run stores the tags and the full
component list in its manifest and in `environment.json`; a registry under
`runs/_environments/<env_tag>.json` keeps the components, when the tag was first seen and
which runs used it, so a tag quoted in a message can always be resolved.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gf.config import settings

REGISTRY_DIR = "_environments"
TAGGED_LIBS = (
    "livekit-agents",
    "livekit",
    "livekit-plugins-deepgram",
    "livekit-plugins-openai",
    "livekit-plugins-silero",
    "livekit-plugins-turn-detector",
    "livekit-plugins-noise-cancellation",
    "openai",
)


def components(
    *,
    agent_variant: str | None = None,
    engine: str = "gf-caller",
    info: dict | None = None,
    target: Any = None,
) -> dict[str, Any]:
    """The hashed components of the environment tag, in a stable order. For the reference
    agent (or no target) the agent's config hash and models are the components, exactly as
    before targets existed; for an external agent under test its id and version label stand
    in, since its configuration is not ours to hash (T9.2)."""
    from gf.agent.config import agent_config
    from gf.runner.batch import environment_info
    from gf.scoring.score import method_version

    cfg = agent_config()
    info = info or environment_info()
    external = target is not None and not getattr(target, "reference", False)
    return {
        "agent_config_hash": f"target:{target.id}@{target.version_label()}"
        if external
        else cfg.config_hash,
        "agent_variant": agent_variant,
        "models": {"stt": "external", "llm": "external", "tts": "external"}
        if external
        else {
            "stt": f"{cfg.models.stt.vendor} {cfg.models.stt.model}",
            "llm": f"{cfg.models.llm.vendor} {cfg.models.llm.model}",
            "tts": f"{cfg.models.tts.vendor} {cfg.models.tts.model}",
        },
        "engine": engine,
        "scoring_method": method_version(),
        "libraries": {k: info.get(k) for k in TAGGED_LIBS if info.get(k) is not None},
        "python": info.get("python"),
    }


def env_tag(comp: dict[str, Any]) -> str:
    canon = json.dumps(comp, sort_keys=True, separators=(",", ":"), default=str)
    return "env-" + hashlib.sha256(canon.encode()).hexdigest()[:8]


def set_tag(session_ids: list[str] | tuple[str, ...]) -> str:
    return "set-" + hashlib.sha256("".join(sorted(session_ids)).encode()).hexdigest()[:8]


def stamp(
    run_id: str,
    folder: Path,
    manifest: dict[str, Any],
    session_ids: list[str],
    *,
    agent_variant: str | None = None,
    engine: str = "gf-caller",
    target: Any = None,
) -> dict[str, Any]:
    """Compute both tags for a run, store them in the manifest and in `environment.json`,
    and record the run in the registry. Returns the environment record."""
    comp = components(
        agent_variant=agent_variant, engine=engine, info=manifest.get("environment"), target=target
    )
    tag = env_tag(comp)
    rec = {
        "env_tag": tag,
        "set_tag": set_tag(session_ids),
        "components": comp,
        "details": manifest.get("environment") or {},
        "run_id": run_id,
        "started_at": manifest.get("started_at"),
    }
    manifest["env_tag"] = rec["env_tag"]
    manifest["set_tag"] = rec["set_tag"]
    manifest["env_components"] = comp
    (folder / "environment.json").write_text(json.dumps(rec, indent=2, default=str))
    register(rec)
    return rec


def register(rec: dict[str, Any]) -> Path:
    reg = settings().runs_dir / REGISTRY_DIR
    reg.mkdir(parents=True, exist_ok=True)
    path = reg / f"{rec['env_tag']}.json"
    entry = (
        json.loads(path.read_text())
        if path.exists()
        else {
            "env_tag": rec["env_tag"],
            "components": rec["components"],
            "details": rec.get("details") or {},
            "first_seen": rec.get("started_at") or datetime.now(UTC).isoformat(),
            "runs": [],
        }
    )
    if rec["run_id"] not in [r["run_id"] for r in entry["runs"]]:
        entry["runs"].append(
            {
                "run_id": rec["run_id"],
                "set_tag": rec["set_tag"],
                "started_at": rec.get("started_at"),
            }
        )
    path.write_text(json.dumps(entry, indent=2, default=str))
    return path


def registry() -> list[dict[str, Any]]:
    """Every environment seen, newest first, with the runs that used it."""
    reg = settings().runs_dir / REGISTRY_DIR
    if not reg.exists():
        return []
    out = []
    for p in sorted(reg.glob("env-*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda e: e.get("first_seen") or "", reverse=True)
    return out


def tags_of(manifest: dict[str, Any]) -> dict[str, str | None]:
    """Tags for any manifest: stored ones, or derived from what an older manifest recorded
    (then the environment tag is `env-?` because library versions were not stored)."""
    env = manifest.get("env_tag")
    if not env:
        env = "env-?" if manifest.get("agent_config_hash") else None
    st = manifest.get("set_tag")
    if not st and manifest.get("sessions"):
        st = set_tag([s["id"] for s in manifest["sessions"]])
    return {"env_tag": env, "set_tag": st}


def current(engine: str = "gf-caller", agent_variant: str | None = None) -> dict[str, Any]:
    """The environment a run started now would carry (`gf env`)."""
    comp = components(agent_variant=agent_variant, engine=engine)
    return {"env_tag": env_tag(comp), "components": comp}


RECORDED_LIBS = (
    "livekit-agents",
    "livekit-plugins-deepgram",
    "livekit-plugins-openai",
    "livekit-plugins-silero",
    "livekit-plugins-turn-detector",
    "livekit-plugins-noise-cancellation",
    "openai",
    "jiwer",
)


def environment_from_history(started_at: str) -> dict[str, Any] | None:
    """For a run made before manifests recorded versions: the library versions from the
    lockfile at the last commit before the run started (the checkout the run was made from),
    with the evidence recorded. Python is taken as the current interpreter and marked so."""
    import platform
    import re
    import subprocess

    from gf.config import ROOT

    try:
        commit = subprocess.run(
            ["git", "log", "-1", "--format=%h", f"--before={started_at}"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=5,
        ).stdout.strip()
        lock = subprocess.run(
            ["git", "show", f"{commit}:uv.lock"],
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not commit or not lock:
        return None
    found = {
        m.group(1): m.group(2)
        for m in re.finditer(r'\[\[package\]\]\nname = "([^"]+)"\nversion = "([^"]+)"', lock)
    }
    libs = {k: found.get(k) for k in RECORDED_LIBS}
    if not libs.get("livekit-agents"):
        return None
    return {
        "git_commit": commit,
        "python": platform.python_version(),
        **libs,
        "backfilled": {
            "from": f"uv.lock at commit {commit} (last commit before the run started)",
            "python": "assumed: the interpreter used for the backfill",
        },
    }


def backfill() -> list[str]:
    """Tag runs that recorded their library versions before tags existed, when the agent
    config they used is the current one (so the components are known). Returns run ids."""
    from gf.agent.config import agent_config

    done = []
    cfg_hash = agent_config().config_hash
    for p in sorted(settings().runs_dir.iterdir()):
        mp = p / "manifest.json"
        if not mp.exists():
            continue
        man = json.loads(mp.read_text())
        if man.get("env_tag"):
            continue
        if not man.get("environment"):
            hist = environment_from_history(man.get("started_at") or "")
            if not hist:
                continue
            man["environment"] = hist
        base = (man.get("agent_config_hash") or "").split("+")[0]
        if base != cfg_hash:
            continue
        stamp(
            p.name,
            p,
            man,
            [x["id"] for x in man.get("sessions", [])],
            agent_variant=man.get("agent_variant"),
            engine=man.get("engine") or "gf-caller",
        )
        mp.write_text(json.dumps(man, indent=2))
        done.append(p.name)
    return done
