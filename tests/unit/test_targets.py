"""Agents under test (spec T9.1): records without secrets, secret resolution, redaction."""

import json

import pytest
import yaml

from gf import targets


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    d = tmp_path / "targets"
    d.mkdir()
    monkeypatch.setenv("TARGETS_DIR", str(d))
    monkeypatch.delenv("GF_TARGET", raising=False)
    (d / "reference.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "reference",
                "name": "Reference agent",
                "agent_name": "gf-support-agent",
                "secrets_from": "livekit_env",
                "reference": True,
                "metadata_template": {"call_id": "{call_id}", "record_dir": "{record_dir}"},
            }
        )
    )
    return d


def test_reference_target_reads_the_repo_livekit_variables(tdir, monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://ref.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIref")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s3cret")
    t = targets.load("reference")
    assert t.reference and t.credentials() == ("wss://ref.livekit.cloud", "APIref", "s3cret")
    assert t.version_label()  # the agent config hash
    r = t.redacted()
    assert "APIref" not in json.dumps(r) and "s3cret" not in json.dumps(r)
    assert r["server_host"] == "ref.livekit.cloud" and r["has_secrets"]
    assert "api_key" not in t.model_dump() and "s3cret" not in repr(t)


def test_add_target_keeps_secrets_out_of_the_record(tdir):
    t = targets.Target(
        id="acme",
        name="Acme support",
        server_url="wss://acme.livekit.cloud",
        agent_name="acme-agent",
        version="v12",
    )
    p = targets.save(t, api_key="APIacme", api_secret="topsecret")
    assert "APIacme" not in p.read_text() and "topsecret" not in p.read_text()
    secrets = yaml.safe_load((tdir / "secrets.yaml").read_text())
    assert secrets["acme"]["api_key"] == "APIacme"
    loaded = targets.load("acme")
    assert loaded.credentials() == ("wss://acme.livekit.cloud", "APIacme", "topsecret")
    assert loaded.version_label() == "v12" and not loaded.reference
    assert [x.id for x in targets.load_all()] == ["reference", "acme"]  # reference first
    meta = json.loads(loaded.dispatch_metadata(call_id="c1", sandbox_url="http://sb:8080"))
    assert meta == {"call_id": "c1", "sandbox_url": "http://sb:8080"}


def test_env_secrets_and_missing_secrets(tdir, monkeypatch):
    targets.save(targets.Target(id="beta-co", name="Beta", agent_name="beta"))
    with pytest.raises(targets.MissingSecrets):
        targets.load("beta-co").credentials()
    assert not targets.load("beta-co").has_secrets
    monkeypatch.setenv("GF_TARGET_BETA_CO_API_KEY", "k")
    monkeypatch.setenv("GF_TARGET_BETA_CO_API_SECRET", "s")
    monkeypatch.setenv("GF_TARGET_BETA_CO_URL", "wss://beta.example")
    t = targets.load("beta-co")
    assert t.credentials() == ("wss://beta.example", "k", "s")
    # a pasted secret in the record is ignored, never loaded as a credential
    (tdir / "pasted.yaml").write_text(
        yaml.safe_dump({"name": "P", "agent_name": "p", "api_key": "LEAK", "api_secret": "LEAK"})
    )
    assert targets.load("pasted").api_key == ""


def test_active_target_and_removal(tdir):
    assert targets.active_id() == "reference"
    targets.save(targets.Target(id="acme", name="Acme", agent_name="a"))
    targets.set_active("acme")
    assert targets.active_id() == "acme" and targets.active().id == "acme"
    with pytest.raises(FileNotFoundError):
        targets.set_active("nope")
    with pytest.raises(ValueError):
        targets.remove("reference")
    targets.remove("acme")
    assert not targets.exists("acme") and targets.active_id() == "reference"
    with pytest.raises(ValueError):
        targets.Target(id="Bad Id", name="x", agent_name="x")


def test_metadata_template_renders_unknown_fields_empty(tdir):
    t = targets.load("reference")
    meta = json.loads(t.dispatch_metadata(call_id="c9", record_dir="/tmp/r"))
    assert meta == {"call_id": "c9", "record_dir": "/tmp/r"}
    assert json.loads(t.dispatch_metadata())["call_id"] == ""


def test_external_target_changes_the_environment_tag_but_the_reference_does_not(tdir, monkeypatch):
    """T9.2: the reference agent keeps the tag it always had; an external agent is tagged by its
    id and version label instead of a config hash it does not own."""
    from gf import environment as E

    monkeypatch.setenv("LIVEKIT_URL", "wss://ref.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "k")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s")
    info = {"python": "3.12.0", "livekit-agents": "1.8.5"}
    base = E.env_tag(E.components(info=info))
    assert E.env_tag(E.components(info=info, target=targets.load("reference"))) == base
    targets.save(targets.Target(id="acme", name="Acme", agent_name="a", version="v1"))
    comp = E.components(info=info, target=targets.load("acme"))
    assert comp["agent_config_hash"] == "target:acme@v1" and comp["models"]["llm"] == "external"
    assert E.env_tag(comp) != base
    targets.save(targets.Target(id="acme", name="Acme", agent_name="a", version="v2"))
    assert E.env_tag(E.components(info=info, target=targets.load("acme"))) != E.env_tag(comp)


def test_run_batch_runs_every_call_against_the_target(tdir, tmp_path, monkeypatch):
    """T9.2: the batch resolves the target once, refuses to start without its secrets, hands
    it to every call and records it in the manifest and the stamp."""
    import asyncio
    import json

    from gf.config import ROOT
    from gf.runner import batch

    monkeypatch.setenv("RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("GF_MAX_COST_USD", "100")
    targets.save(targets.Target(id="acme", name="Acme", agent_name="acme-agent", version="v3"))
    session = str(ROOT / "sessions" / "refund-basic.yaml")
    with pytest.raises(targets.MissingSecrets):
        asyncio.run(batch.run_batch([session], repeat=1, run_id="t-none", target="acme"))
    targets.save(
        targets.Target(
            id="acme",
            name="Acme",
            agent_name="acme-agent",
            version="v3",
            server_url="wss://acme.livekit.cloud",
        ),
        api_key="k",
        api_secret="s",
    )
    seen = []

    async def fake_call(session, call_id, record_dir, *, attempt, variant, target):
        seen.append((target.id, target.agent_name, attempt))
        record_dir.mkdir(parents=True, exist_ok=True)
        return {
            "call_id": call_id,
            "session_id": session.id,
            "attempt": attempt,
            "ended_by": "caller",
        }

    monkeypatch.setattr(batch, "run_call", fake_call)
    monkeypatch.setattr(batch, "estimate_cost", lambda _d: 0.0, raising=False)
    man = asyncio.run(
        batch.run_batch([session], repeat=2, run_id="t-acme", target="acme", stagger_s=0)
    )
    assert seen == [("acme", "acme-agent", 1), ("acme", "acme-agent", 2)]
    assert man["target"]["id"] == "acme" and man["target"]["version"] == "v3"
    assert man["env_components"]["agent_config_hash"] == "target:acme@v3"
    on_disk = json.loads((tmp_path / "runs" / "t-acme" / "manifest.json").read_text())
    assert on_disk["target"]["reference"] is False and "api_key" not in json.dumps(on_disk)


def test_connection_test_verdicts_and_stored_result(tdir):
    """T9.3: the verdict names the first failed step; a target without secrets fails without
    touching the network; the result is stored next to the target."""
    import asyncio

    from gf import targets_check as C

    assert C.verdict(
        credentials=False, room_created=False, dispatched=False, joined_ms=None, spoke_ms=None
    ) == (False, "no_credentials")
    assert C.verdict(
        credentials=True, room_created=False, dispatched=False, joined_ms=None, spoke_ms=None
    ) == (False, "api_refused")
    assert C.verdict(
        credentials=True, room_created=True, dispatched=False, joined_ms=None, spoke_ms=None
    ) == (False, "dispatch_refused")
    assert C.verdict(
        credentials=True, room_created=True, dispatched=True, joined_ms=None, spoke_ms=None
    ) == (False, "never_joined")
    assert C.verdict(
        credentials=True, room_created=True, dispatched=True, joined_ms=900, spoke_ms=None
    ) == (False, "joined_silent")
    assert C.verdict(
        credentials=True, room_created=True, dispatched=True, joined_ms=900, spoke_ms=2100
    ) == (True, "ok")
    targets.save(targets.Target(id="acme", name="Acme", agent_name="a"))
    r = asyncio.run(C.connection_test(targets.load("acme"), timeout_s=1))
    assert r["ok"] is False and r["reason"] == "no_credentials" and "acme" in r["error"]
    assert C.last_result("acme")["reason"] == "no_credentials"
    assert C.last_result("reference") is None
