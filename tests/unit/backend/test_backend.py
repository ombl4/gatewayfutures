"""Mock backend: seeding, rules, log, state, faults. No network."""

import pytest
from fastapi.testclient import TestClient

from gf.backend import app as backend


@pytest.fixture
def client():
    backend._calls.clear()
    backend._initial.clear()
    return TestClient(backend.app)


def tool(client, call_id, name, **args):
    return client.post(f"/tools/{name}", json=args, headers={"X-GF-Call-Id": call_id})


# ---- T1.1 seeding and isolation ----


def test_seed_loads_fixture_and_isolates_calls(client):
    assert client.post("/calls/a/seed", json={}).status_code == 200
    assert client.post("/calls/b/seed", json={}).status_code == 200
    r = tool(client, "a", "issue_refund", order_id="GW-48213", amount=89.99, reason="broken")
    assert r.status_code == 200
    assert client.get("/calls/a/state").json()["after"]["orders"]["GW-48213"]["refunded"] is True
    assert client.get("/calls/b/state").json()["after"]["orders"]["GW-48213"]["refunded"] is False


def test_unknown_call_id_is_auto_seeded(client):
    r = tool(client, "fresh", "lookup_order", order_id="GW-48213", zip="94110")
    assert r.status_code == 200
    assert r.json()["customer_name"] == "Maria Lopez"


def test_missing_call_id_header_is_400(client):
    assert client.post("/tools/lookup_order", json={}).status_code == 400


# ---- T1.2 rules ----


def test_lookup_requires_matching_zip(client):
    ok = tool(client, "c", "lookup_order", order_id="GW-48213", zip="94110")
    bad = tool(client, "c", "lookup_order", order_id="GW-48213", zip="00000")
    assert ok.status_code == 200 and ok.json()["status"] == "delivered"
    assert bad.status_code == 404 and bad.json()["error"] == "order_not_found"


@pytest.mark.parametrize(
    "order_id,amount,error",
    [
        ("GW-48377", 10.0, "not_delivered"),
        ("GW-48911", 15.0, "already_refunded"),
        ("GW-48213", 90.00, "amount_exceeds_total"),
        ("GW-99999", 1.0, "order_not_found"),
    ],
)
def test_refund_rules(client, order_id, amount, error):
    r = tool(client, "c", "issue_refund", order_id=order_id, amount=amount, reason="x")
    assert r.status_code in (404, 409)
    assert r.json()["error"] == error


def test_refund_once_per_order(client):
    first = tool(client, "c", "issue_refund", order_id="GW-48213", amount=50, reason="partial")
    second = tool(client, "c", "issue_refund", order_id="GW-48213", amount=10, reason="again")
    assert first.status_code == 200 and first.json()["refund_id"] == "RF-0001"
    assert second.json()["error"] == "already_refunded"


def test_address_change_only_while_processing(client):
    ok = tool(
        client, "c", "update_shipping_address", order_id="GW-48502", address="1 Main St, Boston"
    )
    late = tool(
        client, "c", "update_shipping_address", order_id="GW-48377", address="1 Main St, Boston"
    )
    assert ok.status_code == 200 and ok.json()["shipping_address"] == "1 Main St, Boston"
    assert late.json()["error"] == "already_shipped"


def test_escalation_creates_ticket_and_ends_call(client):
    r = tool(client, "c", "escalate_to_human", reason="policy", summary="caller wants manager")
    assert r.status_code == 200 and r.json()["ticket_id"] == "TK-0001"
    state = client.get("/calls/c/state").json()["after"]
    assert state["ended"] is True and len(state["tickets"]) == 1


def test_argument_problems_are_rejected_and_logged(client):
    r = tool(client, "c", "issue_refund", order_id="48213", amount=-5, reason="")
    assert r.status_code == 422
    entry = client.get("/calls/c/log").json()[-1]
    assert entry["ok"] is False
    assert any("GW-#####" in p for p in entry["arg_problems"])
    assert any("positive" in p for p in entry["arg_problems"])
    assert any("reason" in p for p in entry["arg_problems"])


# ---- T1.3 log and state ----


def test_log_records_every_request_in_order_with_timing(client):
    tool(client, "c", "lookup_order", order_id="GW-48213", zip="94110")
    tool(client, "c", "issue_refund", order_id="GW-48213", amount=89.99, reason="broken")
    log = client.get("/calls/c/log").json()
    assert [e["tool"] for e in log] == ["lookup_order", "issue_refund"]
    assert [e["id"] for e in log] == [1, 2]
    assert log[0]["ts_ms"] <= log[1]["ts_ms"]
    assert all(e["duration_ms"] >= 0 and e["ok"] for e in log)
    assert log[1]["args"]["amount"] == 89.99 and log[1]["response"]["status"] == "issued"


def test_state_has_before_and_after(client):
    client.post("/calls/c/seed", json={})
    tool(client, "c", "issue_refund", order_id="GW-48213", amount=89.99, reason="broken")
    state = client.get("/calls/c/state").json()
    assert state["before"]["refunds"] == {}
    assert state["after"]["refunds"]["GW-48213"]["amount"] == 89.99


def test_delete_call(client):
    client.post("/calls/c/seed", json={})
    assert client.delete("/calls/c").status_code == 200
    assert client.get("/health").json()["calls"] == 0


# ---- T1.4 faults ----


def test_fault_error_500_on_nth_call(client):
    faults = [{"tool": "issue_refund", "type": "error_500", "nth": 1}]
    client.post("/calls/c/seed", json={"faults": faults})
    r1 = tool(client, "c", "issue_refund", order_id="GW-48213", amount=89.99, reason="x")
    r2 = tool(client, "c", "issue_refund", order_id="GW-48213", amount=89.99, reason="x")
    assert r1.status_code == 500 and r1.json()["error"] == "internal_error"
    assert r2.status_code == 200  # the fault applied to the first call only
    log = client.get("/calls/c/log").json()
    assert log[0]["fault"] == "error_500" or log[0]["ok"] is False
    assert log[0]["ok"] is False and log[1]["ok"] is True


def test_fault_reject(client):
    client.post("/calls/c/seed", json={"faults": [{"tool": "issue_refund", "type": "reject"}]})
    r = tool(client, "c", "issue_refund", order_id="GW-48213", amount=89.99, reason="x")
    assert r.status_code == 409 and r.json()["error"] == "rejected_by_policy"
    assert client.get("/calls/c/state").json()["after"]["refunds"] == {}


def test_fault_latency_is_logged(client):
    faults = [{"tool": "lookup_order", "type": "latency_ms", "value": 50}]
    client.post("/calls/c/seed", json={"faults": faults})
    r = tool(client, "c", "lookup_order", order_id="GW-48213", zip="94110")
    assert r.status_code == 200
    entry = client.get("/calls/c/log").json()[0]
    assert entry["fault"] == "latency_ms:50" and entry["duration_ms"] >= 50
