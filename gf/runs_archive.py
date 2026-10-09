"""Archive runs: move run folders into runs/_archive/ (kept whole, out of every list) and
drop them from the environment registry; `restore` reverses it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from gf.config import settings
from gf.environment import REGISTRY_DIR
from gf.runner.batch import list_runs

ARCHIVE_DIR = "_archive"


def archive(run_ids: list[str], *, all_runs: bool = False) -> list[str]:
    root = settings().runs_dir
    target = root / ARCHIVE_DIR
    target.mkdir(parents=True, exist_ok=True)
    ids = [p.name for p in list_runs()] if all_runs else list(run_ids)
    moved = []
    for rid in ids:
        src = root / rid
        if not (src / "manifest.json").exists():
            continue
        if (src / "job.json").exists():
            job = json.loads((src / "job.json").read_text())
            man = json.loads((src / "manifest.json").read_text())
            if man.get("duration_s") is None and job.get("pid"):
                continue  # possibly still running
        shutil.move(str(src), str(target / rid))
        _registry_drop(rid)
        moved.append(rid)
    return moved


def restore(run_ids: list[str]) -> list[str]:
    root = settings().runs_dir
    moved = []
    for rid in run_ids:
        src = root / ARCHIVE_DIR / rid
        if not src.exists():
            continue
        shutil.move(str(src), str(root / rid))
        man = json.loads((root / rid / "manifest.json").read_text())
        if man.get("env_tag"):
            from gf.environment import register

            register(
                {
                    "env_tag": man["env_tag"],
                    "set_tag": man.get("set_tag"),
                    "components": man.get("env_components") or {},
                    "details": man.get("environment") or {},
                    "run_id": rid,
                    "started_at": man.get("started_at"),
                }
            )
        moved.append(rid)
    return moved


def _registry_drop(run_id: str) -> None:
    reg = settings().runs_dir / REGISTRY_DIR
    if not reg.exists():
        return
    for p in reg.glob("env-*.json"):
        try:
            entry = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        runs = [r for r in entry.get("runs", []) if r.get("run_id") != run_id]
        if len(runs) != len(entry.get("runs", [])):
            if runs:
                entry["runs"] = runs
                p.write_text(json.dumps(entry, indent=2, default=str))
            else:
                p.unlink()


def archived() -> list[Path]:
    root = settings().runs_dir / ARCHIVE_DIR
    return (
        sorted(p for p in root.iterdir() if (p / "manifest.json").exists()) if root.exists() else []
    )
