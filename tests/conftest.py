"""Shared test setup: the persona judge (an LLM call per scored call) never runs in tests."""

import os

import pytest


@pytest.fixture(autouse=True)
def _no_llm_judge(monkeypatch):
    monkeypatch.setenv("GF_PERSONA_JUDGE", "0")
    yield


os.environ.setdefault("GF_PERSONA_JUDGE", "0")
