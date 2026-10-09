"""`gf demo-run`: a run folder built from the committed real call records so the UI can be
seen without API keys. The records are real calls from an earlier run (audio stripped before
commit); the audio here is synthesised tone bursts of the same length, so timing checks on
the demo run (latency, dead air, "caller spoke first") describe the tones, not the calls.
The manifest carries `demo: true` and an early start date, so a demo run is never the latest
run and never a comparison baseline."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from gf.config import ROOT, settings

RECORDS = ROOT / "fixtures" / "records"
CALLS = (
    ("refund-basic", "7c994c348001", 1, "refund-basic.yaml"),
    ("refund-noisy", "ef113fc07616", 2, "refund-noisy-cafe.yaml"),
)


def _synth_audio(folder: Path) -> None:
    sr = 16_000
    secs = float(json.loads((folder / "audio.json").read_text()).get("duration_s") or 60.0)
    t = np.arange(int(sr * secs)) / sr
    left = np.where(((t // 2) % 2) == 0, np.sin(2 * np.pi * 220 * t), 0.0) * 0.3
    right = np.where(((t // 2) % 2) == 1, np.sin(2 * np.pi * 330 * t), 0.0) * 0.3
    sf.write(folder / "audio.wav", np.stack([left, right], axis=1), sr, subtype="PCM_16")


def seed_demo_run(run_id: str = "demo") -> dict[str, Any]:
    from gf.scoring.score import score_run

    run = settings().runs_dir / run_id
    if run.exists():
        shutil.rmtree(run)
    calls = []
    for src, sid, n, _name in CALLS:
        dst = run / sid / str(n)
        shutil.copytree(RECORDS / src, dst)
        _synth_audio(dst)
        calls.append(
            {"session_id": sid, "attempt": n, "record_dir": str(dst), "call_id": f"{sid}-{n}"}
        )
    # one attempt without a recording, as LiveKit-simulator imports have
    noaudio = run / "ef113fc07616" / "1"
    shutil.copytree(RECORDS / "refund-noisy", noaudio)
    meta = json.loads((noaudio / "meta.json").read_text())
    meta["attempt"], meta["call_id"] = 1, "ef113fc07616-1"
    (noaudio / "meta.json").write_text(json.dumps(meta, indent=2))
    calls.append(
        {
            "session_id": "ef113fc07616",
            "attempt": 1,
            "record_dir": str(noaudio),
            "call_id": "ef113fc07616-1",
        }
    )
    manifest = {
        "run_id": run_id,
        "folder": str(run),
        "demo": True,
        "started_at": "2026-01-01T00:00:00+00:00",
        "agent_config_hash": "a7c6a425319d",
        "sessions_hash": "demo",
        "repeat": 2,
        "concurrency": 2,
        "engine": "gf-caller",
        "kind": "run",
        "duration_s": 0.0,
        "sessions": [
            {"id": sid, "title": sid, "path": str(ROOT / "sessions" / name)}
            for _, sid, _, name in CALLS
        ],
        "calls": calls,
    }
    (run / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return score_run(run_id)
