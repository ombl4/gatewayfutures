"""Archive runs: move run folders into runs/_archive/ (kept whole, out of every list) and
drop them from the environment registry; `restore` reverses it."""

from __future__ import annotations

import json
import re
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


NUMBERED = re.compile(r"^(.*-)(\d{3,})$")
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def next_run_id(parent: str) -> str:
    """`base-001` → `base-002`, the first number not used by a run or an archived run; a
    parent without a numbered suffix gets "" (the caller falls back to the dated id)."""
    m = NUMBERED.match(parent)
    if not m:
        return ""
    root = settings().runs_dir
    stem, digits = m.group(1), m.group(2)
    n = int(digits) + 1
    while True:
        cand = f"{stem}{n:0{len(digits)}d}"
        if not (root / cand).exists() and not (root / ARCHIVE_DIR / cand).exists():
            return cand
        n += 1


def rename(old: str, new: str) -> Path:
    """Move runs/<old> to runs/<new> and rewrite the id wherever the run is named: its
    manifest, summary, environment stamp and job file, the environment registry, and the
    `parent_run` of any run started from it. Call records are untouched (they never name
    the run)."""
    root = settings().runs_dir
    src, dst = root / old, root / new
    if not (src / "manifest.json").exists():
        raise ValueError(f"no run {old!r} under {root}")
    if not RUN_ID.match(new):
        raise ValueError(f"run id {new!r}: letters, digits, '.', '_' and '-' only")
    if dst.exists() or (root / ARCHIVE_DIR / new).exists():
        raise ValueError(f"a run named {new!r} already exists")
    man = json.loads((src / "manifest.json").read_text())
    if (src / "job.json").exists() and man.get("duration_s") is None:
        job = json.loads((src / "job.json").read_text())
        if job.get("pid"):
            raise ValueError(f"run {old!r} may still be running; wait for it to finish")
    shutil.move(str(src), str(dst))
    for name in ("manifest.json", "summary.json", "environment.json", "job.json"):
        f = dst / name
        if f.exists():
            text = f.read_text()
            text = text.replace(f'"run_id": "{old}"', f'"run_id": "{new}"')
            text = text.replace(f"/{old}/", f"/{new}/").replace(f'/{old}"', f'/{new}"')
            f.write_text(text)
    reg = root / REGISTRY_DIR
    if reg.exists():
        for f in reg.glob("env-*.json"):
            try:
                entry = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            hit = False
            for r in entry.get("runs", []):
                if r.get("run_id") == old:
                    r["run_id"] = new
                    hit = True
            if hit:
                f.write_text(json.dumps(entry, indent=2, default=str))
    for f in root.glob("*/manifest.json"):
        try:
            child = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if child.get("parent_run") == old:
            child["parent_run"] = new
            f.write_text(json.dumps(child, indent=2, default=str))
    return dst
