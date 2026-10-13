"""Traffic-weighted pass rate: the suite's result re-weighted by the real call mix.

The plain rate averages over whatever sessions the suite happens to hold, so ten refund
sessions and one address change count ten to one even if real traffic is the other way
round. `traffic.yaml` (repo root) gives each primary area its share of real calls; the
weighted rate is the sum over areas of share x that area's pass rate.

Each area's sessions are one stratum. The interval is a Wilson interval on the weighted
rate with the Kish effective sample size n_eff = 1 / sum(w^2 / n), so a heavy area with
few calls widens it. Areas with traffic but no valid calls are left out and reported as
uncovered: the rate then speaks only for the covered share of traffic.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from gf.config import ROOT
from gf.scoring.stats import wilson_rate
from gf.sessions.taxonomy import PRIMARY_AREAS

TRAFFIC_FILE = ROOT / "traffic.yaml"
# `denial` is never a primary area (it qualifies refund or address change), so it takes no share
AREAS = (*(a for a in PRIMARY_AREAS if a != "denial"), "other")


def load_traffic(path: Path | None = None) -> dict[str, float] | None:
    """Shares per primary area, normalised to sum to 1; None when there is no traffic file."""
    path = path or TRAFFIC_FILE
    if not path.exists():
        return None
    raw = (yaml.safe_load(path.read_text()) or {}).get("areas") or {}
    unknown = set(raw) - set(AREAS)
    if unknown:
        raise ValueError(f"{path.name}: unknown areas {sorted(unknown)}; use {list(AREAS)}")
    shares = {str(k): float(v) for k, v in raw.items()}
    if any(v < 0 for v in shares.values()) or sum(shares.values()) <= 0:
        raise ValueError(f"{path.name}: shares must be >= 0 and not all zero")
    total = sum(shares.values())
    return {k: v / total for k, v in shares.items() if v > 0}


def traffic_hash(path: Path | None = None) -> str:
    path = path or TRAFFIC_FILE
    return hashlib.sha256(path.read_bytes()).hexdigest()[:8] if path.exists() else ""


def weighted_rate(per_session: list[dict[str, Any]], shares: dict[str, float]) -> dict[str, Any]:
    """Weighted pass rate over `per_session` rows (each with `area`, `n` valid calls and
    `passed`), given the traffic shares per area."""
    strata: dict[str, dict[str, int]] = {}
    for row in per_session:
        d = strata.setdefault(row["area"], {"n": 0, "passed": 0})
        d["n"] += row.get("n") or 0
        d["passed"] += row.get("passed") or 0
    covered = {a: w for a, w in shares.items() if strata.get(a, {}).get("n")}
    uncovered = sorted(a for a in shares if a not in covered)
    covered_share = sum(covered.values())
    rows = [
        {
            "area": a,
            "share": round(w, 4),
            "n": strata.get(a, {}).get("n", 0),
            "passed": strata.get(a, {}).get("passed", 0),
            "rate": round(strata[a]["passed"] / strata[a]["n"], 4) if a in covered else None,
        }
        for a, w in sorted(shares.items(), key=lambda kv: -kv[1])
    ]
    out: dict[str, Any] = {
        "strata": rows,
        "covered_share": round(covered_share, 4),
        "uncovered": uncovered,
        "unweighted_areas": sorted(a for a in strata if a not in shares),
    }
    if not covered:
        return out | {"rate": None, "ci_low": None, "ci_high": None, "n_eff": 0.0}
    w = {a: s / covered_share for a, s in covered.items()}
    p = sum(w[a] * strata[a]["passed"] / strata[a]["n"] for a in w)
    n_eff = 1.0 / sum(w[a] ** 2 / strata[a]["n"] for a in w)
    point, lo, hi = wilson_rate(p, n_eff)
    return out | {"rate": point, "ci_low": lo, "ci_high": hi, "n_eff": round(n_eff, 2)}


def weighted_summary(per_session: list[dict[str, Any]], path: Path | None = None):
    """The `weighted` block of summary.json, or None without a traffic file."""
    shares = load_traffic(path)
    if shares is None:
        return None
    return weighted_rate(per_session, shares) | {"traffic_hash": traffic_hash(path)}
