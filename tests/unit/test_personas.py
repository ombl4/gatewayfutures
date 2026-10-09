"""Persona library: references resolve, matrix derivation keeps the scenario, ids stay stable."""

from pathlib import Path

from gf.config import ROOT
from gf.sessions.personas import GROUPS, derive, groups, load_personas
from gf.sessions.schema import Session


def test_library_is_three_even_groups():
    by_group = groups()
    assert set(by_group) == set(GROUPS)
    assert {len(v) for v in by_group.values()} == {4}
    assert len(load_personas()) == 12


def test_derive_keeps_facts_goal_and_expectations_and_changes_only_the_caller(tmp_path):
    base = Session.load(ROOT / "sessions" / "refund-basic.yaml")
    assert base.id == "7c994c348001"  # the published runs' id: new fields must not move it
    d = derive(base, "street-impatient")
    assert d.id != base.id and d.base_session == base.id
    assert d.caller.persona.name == base.caller.persona.name  # who stays with the session
    assert (d.caller.facts, d.caller.goal, d.expected) == (
        base.caller.facts,
        base.caller.goal,
        base.expected,
    )
    assert d.caller.conditions.noise == "street@10dB" and d.caller.persona_ref == "street-impatient"
    path = tmp_path / "d.yaml"
    path.write_text(d.dump())
    assert Session.load(path).id == d.id


def test_persona_ref_fills_only_what_the_session_leaves_unset(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text(
        "title: via ref\ncaller:\n  persona: {name: Maria Lopez}\n  persona_ref: elderly-slow-line\n"
        '  pace: 0.7\n  facts: {order_id: GW-48213, zip: "94110"}\n  goal: refund\n'
        "fixtures: orders_basic\nexpected: {outcome: refunded}\n"
    )
    s = Session.load(p)
    assert s.caller.pace == 0.7  # the session's own value wins
    assert s.caller.conditions.phone_line is True and s.caller.voice == "aura-2-athena-en"
    assert Path(s.source_path) == p
