"""Small shared helpers."""

from __future__ import annotations

import time


def now_ms() -> int:
    """Epoch milliseconds. Comparable across processes on one host, which is all we need."""
    return time.time_ns() // 1_000_000
