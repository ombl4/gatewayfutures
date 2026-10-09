"""Provider registry and page (spec T4.6a)."""

from gf import providers as reg


def test_registry_has_one_runnable_provider_and_it_is_current():
    rows = reg.providers()
    runnable = [r for r in rows if r["runnable"]]
    current = [r for r in rows if r["current"]]
    assert len(runnable) == 1 and len(current) == 1
    assert runnable[0]["key"] == current[0]["key"] == reg.current_key() == "livekit"


def test_every_provider_states_its_three_obligations():
    for p in reg.REGISTRY:
        assert p.reach and p.events and p.config_hash and p.module, p.key
    assert reg.get("pipecat").kind == reg.DESIGNED
    assert reg.get("nope") is None
