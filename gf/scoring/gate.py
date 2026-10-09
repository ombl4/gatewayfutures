"""`gf gate <run_id>`: the CI decision over a scored run.

Fails on (a) a zero-tolerance check firing on any valid call, (b) too many invalid
simulations (the harness, not the agent, is then the problem), or (c) a pass rate whose
95% Wilson interval cannot reach `min_pass`. A drop inside the interval is never a failure
here: that is "within noise", to be confirmed by a rerun, not blocked on.
"""

from __future__ import annotations

import json
from typing import Any

from gf.config import settings

ZERO_TOLERANCE = ("claims.claimed_without_acting", "tools.wrong_write")


def gate_run(run_id: str, *, min_pass: float = 0.0, max_invalid: float = 0.2) -> dict[str, Any]:
    run_dir = settings().runs_dir / run_id
    summary_path = run_dir / "summary.json"
    if not summary_path.exists():
        from gf.scoring.score import score_run

        score_run(run_id)
    summ = json.loads(summary_path.read_text())
    manifest = json.loads((run_dir / "manifest.json").read_text())
    attempts = summ.get("attempts", [])
    o = summ.get("overall", {})
    reasons: list[str] = []
    for a in attempts:
        if not a.get("valid"):
            continue
        for cid in ZERO_TOLERANCE:
            if cid in (a.get("hard_fails") or []):
                reasons.append(
                    f"{cid} on {a['session_id']} attempt {a['attempt']}: {a.get('failure_reason', '')}"
                )
    n_all = len(attempts)
    invalid = summ.get("invalid", 0)
    if n_all and invalid / n_all > max_invalid:
        reasons.append(
            f"{invalid}/{n_all} calls invalid ({invalid / n_all:.0%} > {max_invalid:.0%}): fix the simulator before trusting the rate"
        )
    if min_pass and o.get("n") and o.get("ci_high", 0) < min_pass:
        reasons.append(
            f"pass rate {o['rate']:.0%} (interval {o['ci_low']:.0%}–{o['ci_high']:.0%}) cannot reach {min_pass:.0%}"
        )
    if manifest.get("partial"):
        reasons.append(f"run is partial: {manifest.get('stopped_reason')}")
    return {
        "run_id": run_id,
        "ok": not reasons,
        "reasons": reasons,
        "n": o.get("n", 0),
        "passed": o.get("passed", 0),
        "ci_low": o.get("ci_low", 0.0),
        "ci_high": o.get("ci_high", 0.0),
        "invalid": invalid,
        "cost_usd": float((summ.get("cost") or {}).get("usd") or 0.0),
    }
