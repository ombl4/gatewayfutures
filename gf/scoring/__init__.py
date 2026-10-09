"""Scoring: pure functions over a CallRecord. Deterministic checks decide pass/fail; every
check carries evidence (tool ids, times, turn numbers) that the UI can jump to."""

METHOD_VERSION = "score-v3"  # v3: number canon, gap attribution, fail bars, scripted faults
