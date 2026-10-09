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
