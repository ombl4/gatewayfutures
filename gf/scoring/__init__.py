"""Scoring: pure functions over a CallRecord. Deterministic checks decide pass/fail; every
check carries evidence (tool ids, times, turn numbers) that the UI can jump to."""

METHOD_VERSION = (
    "score-v2+claims-regex-v2"  # v2: hearing digit rule, rejection wording, hand-off intent
)
