from gf.agent.config import AgentConfig, agent_config
from gf.agent.tools import ALL_TOOLS, _format, _normalise_order_id
from gf.backend.app import TOOLS as BACKEND_TOOLS


def test_config_loads_with_stable_hash():
    a, b = AgentConfig.load(), AgentConfig.load()
    assert a.config_hash and a.config_hash == b.config_hash
    assert a.persona_name == "Ava" and a.greeting.startswith("Thanks for calling")
    assert a.description.startswith("Gateway Goods")


def test_config_tools_match_backend_and_tool_definitions():
    cfg = agent_config()
    assert set(cfg.tools) == set(BACKEND_TOOLS)
    assert {t.info.name for t in ALL_TOOLS} == set(cfg.tools)


def test_tool_schemas_have_descriptions_and_args():
    by_name = {t.info.name: t for t in ALL_TOOLS}
    assert "zip" in by_name["lookup_order"].info.description.lower()
    assert "confirm" in by_name["issue_refund"].info.description.lower()


def test_order_id_normalisation():
    assert _normalise_order_id("G W 4 8 2 1 3") == "GW-48213"
    assert _normalise_order_id("gw48213") == "GW-48213"
    assert _normalise_order_id("GW-48213") == "GW-48213"
    assert _normalise_order_id("GW-123") == "GW-123"  # left for the backend to reject


def test_tool_result_wording_makes_failure_unmistakable():
    assert _format(True, {"refund_id": "RF-1"}).startswith("SUCCESS")
    failed = _format(False, {"error": "not_delivered", "message": "Order is shipped"})
    assert failed.startswith("FAILED (not_delivered)") and "Do not claim it succeeded" in failed
