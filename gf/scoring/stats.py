"""Pass-rate statistics: Wilson interval, pass^k / pass@k, flaky."""

from __future__ import annotations

from math import sqrt

Z95 = 1.959964


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float, float]:
    """(point, low, high) for k successes in n trials, clipped to [0, 1]."""
    if n <= 0:
        return (0.0, 0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(p, 4), round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4))


def pass_summary(outcomes: list[bool]) -> dict:
    n, k = len(outcomes), sum(outcomes)
    point, lo, hi = wilson(k, n)
    return {
        "n": n,
        "passed": k,
        "rate": point,
        "ci_low": lo,
        "ci_high": hi,
        "pass_all": bool(n) and k == n,  # pass^k
        "pass_any": k > 0,  # pass@k
        "flaky": 0 < k < n,
    }


def intervals_overlap(a: dict, b: dict) -> bool:
    return not (a["ci_high"] < b["ci_low"] or b["ci_high"] < a["ci_low"])
