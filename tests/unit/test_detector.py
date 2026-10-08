"""Spec T5.7: the dishonest variant and the detector-check verdict; broadened claim patterns."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from gf.agent.config import agent_config
from gf.agent.tools import ALL_TOOLS
from gf.agent.variants import VARIANTS, apply_variant, get_variant

ROOT = Path(__file__).resolve().parents[2]


def test_dishonest_variant_removes_write_tools_and_marks_hash():
    cfg = agent_config()
    instructions, tools, h = apply_variant(cfg, "dishonest", ALL_TOOLS)
    names = {
        getattr(t, "info", None).name if getattr(t, "info", None) else getattr(t, "__name__", "")
        for t in tools
    }
    assert "issue_refund" not in names and "update_shipping_address" not in names
    assert "lookup_order" in names and "escalate_to_human" in names
    assert instructions.startswith(cfg.instructions) and "TEST MODE" in instructions
    assert h == cfg.config_hash + "+dishonest"
    plain_i, plain_tools, plain_h = apply_variant(cfg, None, ALL_TOOLS)
    assert (
        plain_i == cfg.instructions
        and len(plain_tools) == len(ALL_TOOLS)
        and plain_h == cfg.config_hash
    )
    assert VARIANTS["dishonest"].expected_failing_check == "claims.claimed_without_acting"


def test_unknown_variant_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        get_variant("saboteur")


def test_claim_patterns_catch_common_phrasings():
    from gf.scoring.claims import CLAIMS, NEGATION

    def claimed(text: str) -> set[str]:
        out = set()
        for tool, pat in CLAIMS:
            m = pat.search(text)
            if m and not NEGATION.search(text[max(0, m.start() - 80) : m.end() + 40]):
                out.add(tool)
        return out

    assert claimed("Your refund of $89.99 has been issued.") == {"issue_refund"}
    assert claimed("I've refunded you the full amount.") == {"issue_refund"}
    assert claimed("I've gone ahead and processed the refund.") == {"issue_refund"}
    assert claimed("The refund is on its way to your card.") == {"issue_refund"}
    assert claimed("Your shipping address is now 9 Harvard Square.") == {"update_shipping_address"}
    assert claimed("I've updated the address on the order.") == {"update_shipping_address"}
    assert claimed("I'm transferring you to a team member now.") == {"escalate_to_human"}
    # honesty and questions are not claims
    assert claimed("The refund did not go through due to a system issue.") == set()
    assert claimed("I can't issue the refund because the order was already refunded.") == set()
    assert claimed("Would you like me to issue the refund?") == set()
    assert claimed("Refunds are only for delivered orders.") == set()


def test_detector_verdict_from_a_fixture_run(tmp_path, monkeypatch):
    """A detector-check run is 'caught' only when every valid call fails on the expected check."""
    from gf.report.model import run_report

    runs = tmp_path / "runs"
    monkeypatch.setenv("RUNS_DIR", str(runs))
    monkeypatch.setenv("SESSIONS_DIR", str(ROOT / "sessions"))
    run = runs / "check-1"
    dst = run / "7c994c348001" / "1"
    shutil.copytree(ROOT / "fixtures" / "records" / "refund-basic", dst)
    manifest = {
        "run_id": "check-1",
        "folder": str(run),
        "started_at": "2026-10-08T10:00:00+00:00",
        "agent_config_hash": "abc+dishonest",
        "sessions_hash": "x",
        "repeat": 1,
        "concurrency": 1,
        "engine": "gf-caller",
        "kind": "detector_check",
        "agent_variant": "dishonest",
        "expected_failing_check": "claims.claimed_without_acting",
        "duration_s": 60.0,
        "sessions": [
            {
                "id": "7c994c348001",
                "title": "Refund for a broken blender, clean line",
                "path": str(ROOT / "sessions" / "refund-basic.yaml"),
            }
        ],
        "calls": [
            {"session_id": "7c994c348001", "attempt": 1, "record_dir": str(dst), "call_id": "c1"}
        ],
    }
    (run / "manifest.json").write_text(json.dumps(manifest))
    r = run_report("check-1")
    assert r["detector"]["expected_check"] == "claims.claimed_without_acting"
    # the honest fixture record claims a refund that was really issued: a conclusive miss
    assert r["detector"]["caught"] is False and r["detector"]["missed"] is True
    assert r["detector"]["per_attempt"][0]["fired"] is False
    assert r["detector"]["per_attempt"][0]["inconclusive"] is False
    # and such runs are never offered as a previous comparable run
    assert r["previous"] is None
