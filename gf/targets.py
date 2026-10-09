"""Agents under test (spec Part 9, T9.1).

A *target* is one agent the platform can run practice sessions against. v1 knows one kind,
`livekit`: an agent reached by dispatching it into a room created in its own LiveKit
project. The record (`targets/<id>.yaml`) is committed and holds no secrets; the API key and
secret come, in this order, from `targets/secrets.yaml` (gitignored), from the environment
(`GF_TARGET_<ID>_API_KEY`, `GF_TARGET_<ID>_API_SECRET`, `GF_TARGET_<ID>_URL`) or, for the
reference agent only, from the repository's own `LIVEKIT_*` variables.

Nothing in this module prints or renders a secret: `redacted()` is what the UI and the CLI
show, and `model_dump()` leaves the secret fields out.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field, field_validator

from gf.config import ROOT, settings

SECRETS_FILE = "secrets.yaml"
ACTIVE_FILE = "active"
REFERENCE_ID = "reference"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DEFAULT_METADATA = {"call_id": "{call_id}", "sandbox_url": "{sandbox_url}"}


class MissingSecrets(RuntimeError):
    """The target has no API key or secret anywhere the platform looks."""


class _Blank(dict):
    def __missing__(self, key: str) -> str:
        return ""


class Target(BaseModel):
    id: str
    name: str
    kind: Literal["livekit"] = "livekit"
    server_url: str = ""  # wss://...; empty when the secrets source provides it
    agent_name: str
    # dispatch metadata the agent receives, rendered per call with {call_id}, {sandbox_url},
    # {record_dir}, {agent_variant}, {session_id}; unknown fields render empty
    metadata_template: dict[str, Any] = Field(default_factory=lambda: dict(DEFAULT_METADATA))
    version: str = ""  # a label the owner maintains; the reference agent uses its config hash
    notes: str = ""
    secrets_from: Literal["file", "env", "livekit_env"] = "file"
    reference: bool = False
    api_key: str = Field("", exclude=True, repr=False)
    api_secret: str = Field("", exclude=True, repr=False)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not ID_RE.match(v):
            raise ValueError("target id: lowercase letters, digits and dashes, 1-40 characters")
        return v

    # ---- what the rest of the platform asks a target for
    def credentials(self) -> tuple[str, str, str]:
        """(server_url, api_key, api_secret), or MissingSecrets."""
        if not (self.server_url and self.api_key and self.api_secret):
            missing = [
                n
                for n, v in (
                    ("server url", self.server_url),
                    ("api key", self.api_key),
                    ("api secret", self.api_secret),
                )
                if not v
            ]
            raise MissingSecrets(f"target {self.id!r}: no {', '.join(missing)} configured")
        return self.server_url, self.api_key, self.api_secret

    @property
    def has_secrets(self) -> bool:
        return bool(self.server_url and self.api_key and self.api_secret)

    @property
    def server_host(self) -> str:
        return urlparse(self.server_url).netloc if self.server_url else ""

    def version_label(self) -> str:
        if self.version:
            return self.version
        if self.reference:
            from gf.agent.config import agent_config

            return agent_config().config_hash
        return "unversioned"

    def dispatch_metadata(self, **fields: Any) -> str:
        """The JSON string sent with the dispatch, from the template and the call's fields."""
        values = _Blank({k: "" if v is None else str(v) for k, v in fields.items()})

        def render(v: Any) -> Any:
            if isinstance(v, str):
                return v.format_map(values)
            if isinstance(v, dict):
                return {k: render(x) for k, x in v.items()}
            if isinstance(v, list):
                return [render(x) for x in v]
            return v

        return json.dumps(render(self.metadata_template))

    def redacted(self) -> dict[str, Any]:
        """What the UI and the CLI show: everything but the secrets."""
        d = self.model_dump()
        d["server_host"] = self.server_host
        d["has_secrets"] = self.has_secrets
        d["version_label"] = self.version_label()
        return d


# ---- store


def targets_dir() -> Path:
    return Path(os.environ.get("TARGETS_DIR") or settings().targets_dir or ROOT / "targets")


def _read_secrets() -> dict[str, dict[str, str]]:
    p = targets_dir() / SECRETS_FILE
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text()) or {}
    return {str(k): {str(a): str(b) for a, b in (v or {}).items()} for k, v in data.items()}


def _write_secrets(data: dict[str, dict[str, str]]) -> None:
    d = targets_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / SECRETS_FILE
    p.write_text(
        "# Secrets of the agents under test. Never commit this file.\n"
        + yaml.safe_dump(data, sort_keys=True)
    )
    try:
        p.chmod(0o600)
    except OSError:
        pass


def resolve_secrets(t: Target) -> Target:
    """Fill api_key / api_secret (and the url when the secrets source carries it)."""
    env_id = t.id.upper().replace("-", "_")
    key = secret = url = ""
    if t.secrets_from == "file":
        row = _read_secrets().get(t.id, {})
        key, secret, url = (
            row.get("api_key", ""),
            row.get("api_secret", ""),
            row.get("server_url", ""),
        )
    if not key:
        key = os.environ.get(f"GF_TARGET_{env_id}_API_KEY", "")
        secret = os.environ.get(f"GF_TARGET_{env_id}_API_SECRET", "")
        url = url or os.environ.get(f"GF_TARGET_{env_id}_URL", "")
    if not key and t.secrets_from == "livekit_env":
        s = settings()
        key, secret, url = s.livekit_api_key, s.livekit_api_secret, url or s.livekit_url
    t.api_key, t.api_secret = key, secret
    if url and not t.server_url:
        t.server_url = url
    return t


def load(target_id: str) -> Target:
    p = targets_dir() / f"{target_id}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"no target {target_id!r} under {targets_dir()}")
    raw = yaml.safe_load(p.read_text()) or {}
    raw.setdefault("id", target_id)
    for k in ("api_key", "api_secret"):
        raw.pop(k, None)  # never from the record, even if someone pasted one there
    return resolve_secrets(Target(**raw))


def load_all() -> list[Target]:
    d = targets_dir()
    out = []
    for p in sorted(d.glob("*.yaml")) if d.exists() else []:
        if p.name == SECRETS_FILE:
            continue
        out.append(load(p.stem))
    out.sort(key=lambda t: (not t.reference, t.name.lower()))
    return out


def exists(target_id: str) -> bool:
    return (targets_dir() / f"{target_id}.yaml").exists()


def save(t: Target, *, api_key: str | None = None, api_secret: str | None = None) -> Path:
    """Write the record (no secrets) and, when given, the secrets to the secrets file."""
    d = targets_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{t.id}.yaml"
    p.write_text(
        "# An agent under test. Secrets live in secrets.yaml (gitignored) or the environment.\n"
        + yaml.safe_dump(t.model_dump(), sort_keys=False, allow_unicode=True)
    )
    if api_key is not None or api_secret is not None:
        data = _read_secrets()
        row = data.get(t.id, {})
        if api_key is not None:
            row["api_key"] = api_key
        if api_secret is not None:
            row["api_secret"] = api_secret
        if t.server_url:
            row["server_url"] = t.server_url
        data[t.id] = row
        _write_secrets(data)
    return p


def remove(target_id: str) -> None:
    if target_id == REFERENCE_ID:
        raise ValueError("the reference agent cannot be removed")
    p = targets_dir() / f"{target_id}.yaml"
    if p.exists():
        p.unlink()
    data = _read_secrets()
    if target_id in data:
        del data[target_id]
        _write_secrets(data)
    if active_id() == target_id:
        set_active(REFERENCE_ID)


def active_id() -> str:
    env = os.environ.get("GF_TARGET")
    if env:
        return env
    p = targets_dir() / ACTIVE_FILE
    if p.exists():
        v = p.read_text().strip()
        if v and exists(v):
            return v
    return REFERENCE_ID


def set_active(target_id: str) -> None:
    if not exists(target_id):
        raise FileNotFoundError(f"no target {target_id!r}")
    d = targets_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / ACTIVE_FILE).write_text(target_id + "\n")


def active() -> Target:
    tid = active_id()
    return load(tid) if exists(tid) else load(REFERENCE_ID)
