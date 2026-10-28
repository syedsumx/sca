"""Extensions: packaged add-ons with a declared, enforced footprint.

An extension is a manifest (``cairn-extension.yaml``)::

    name: owner-coverage
    version: 1.2.0
    publisher: Acme Data Platform
    description: Nags owners of uncertified tables.
    permissions: [VIEW_ENTITY_PAGE, EDIT_ENTITY_DOCS]      # what its token may do, nothing more
    scope: {attr: resource.domains, op: contains, value: urn:li:domain:finance}    # optional
    events:                                                 # signed webhooks from the notification hub
      - url: https://owner-coverage.acme.internal/events
        types: [quality.incident.raised, contract.broken]
    ui:                                                     # sections of the workspace, in a sandboxed frame
      - label: Owner coverage
        url: https://owner-coverage.acme.internal/ui
        placement: workspace                                # or asset: a tab on each asset page (?urn=…)

Installing it (admins) returns a scoped API token ``cx_<name>.<secret>`` and an event signing secret,
each shown once. The token acts as ``urn:li:corpuser:cairn-ext-<name>`` and is allowed exactly the
manifest's permissions, on the resources its scope matches; administration privileges can never be
granted. Events carry ``Cairn-Signature: t=<unix>,v1=<hex HMAC-SHA256 of "<t>.<body>">``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, delete, select, update

from cairn.common.db import JSONText, metadata
from cairn.governance.abac import evaluate_condition, validate_condition

PREFIX = "cx_"
ACTOR_PREFIX = "urn:li:corpuser:cairn-ext-"
# Privileges an extension may hold: reading the catalog and editing the metadata a person could.
GRANTABLE = {
    "VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE", "VIEW_DATASET_USAGE", "VIEW_ENTITY", "SEARCH",
    "EDIT_ENTITY_DOCS", "EDIT_ENTITY_TAGS", "EDIT_ENTITY_GLOSSARY_TERMS", "EDIT_ENTITY_OWNERS",
    "EDIT_ENTITY_DOMAINS", "EDIT_ENTITY_PROPERTIES", "EDIT_DATASET_COL_DESCRIPTION", "EDIT_DATASET_COL_TAGS",
    "EDIT_DATASET_COL_GLOSSARY_TERMS", "VIEW_QUALITY", "EDIT_QUALITY_MONITORS", "PROPOSE_ENTITY_DOCS",
    "EDIT_ENTITY_DOC_LINKS", "EDIT_ENTITY_INCIDENTS", "EDIT_ENTITY_ASSERTIONS", "EDIT_LINEAGE",
    "VIEW_DATASET_OPERATIONS", "VIEW_ENTITY_QUERIES",
}

extensions = Table(
    "cairn_extensions", metadata,
    Column("name", String(64), primary_key=True),
    Column("version", String(32), nullable=False),
    Column("manifest", JSONText, nullable=False),
    Column("token_hash", String(64), nullable=False, unique=True),
    Column("signing_secret", Text, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("installed_by", String(512), nullable=False),
    Column("installed_ms", BigInteger, nullable=False),
)


class ExtensionError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class EventHook(BaseModel):
    url: str = Field(pattern=r"^https?://", max_length=1000)
    types: list[str] = Field(default_factory=list, description="notification types; empty = all")
    domains: list[str] = Field(default_factory=list)

    @field_validator("types")
    @classmethod
    def _known(cls, v: list[str]) -> list[str]:
        from cairn.notify.hub import EVENT_TYPES

        if bad := sorted(set(v) - set(EVENT_TYPES)):
            raise ValueError(f"unknown event types {', '.join(bad)} (known: {', '.join(EVENT_TYPES)})")
        return v


class UiSection(BaseModel):
    label: str = Field(min_length=1, max_length=40)
    url: str = Field(pattern=r"^https?://", max_length=1000)
    placement: Literal["workspace", "asset"] = "workspace"


class Manifest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{1,40}$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.-]+)?$")
    publisher: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    permissions: list[str] = Field(default_factory=list)
    scope: Optional[dict[str, Any]] = None
    events: list[EventHook] = Field(default_factory=list, max_length=10)
    ui: list[UiSection] = Field(default_factory=list, max_length=5)

    @field_validator("permissions")
    @classmethod
    def _grantable(cls, v: list[str]) -> list[str]:
        if bad := sorted(set(v) - GRANTABLE):
            raise ValueError(f"extensions cannot be granted {', '.join(bad)}")
        return sorted(set(v))

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: Optional[dict]) -> Optional[dict]:
        if v is not None:
            validate_condition(v)
        return v


def actor_of(name: str) -> str:
    return f"{ACTOR_PREFIX}{name}"


def sign(secret: str, body: bytes, ts: Optional[int] = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_signature(secret: str, body: bytes, header: str, tolerance_s: int = 300) -> bool:
    """For extensions: check a ``Cairn-Signature`` header (and that it is recent)."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (KeyError, ValueError):
        return False
    if abs(time.time() - ts) > tolerance_s:
        return False
    return hmac.compare_digest(sign(secret, body, ts), header)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class ExtensionService:
    def __init__(self, engine, attributes=None, audit=None):
        self.engine = engine
        self.attributes = attributes
        self.audit = audit
        self._cache: Optional[tuple[float, dict[str, dict]]] = None

    def _rows(self) -> dict[str, dict]:
        if self._cache and self._cache[0] > time.monotonic():
            return self._cache[1]
        with self.engine.connect() as conn:
            rows = {r.name: dict(r._mapping) for r in conn.execute(select(extensions))}
        self._cache = (time.monotonic() + 30, rows)
        return rows

    def _changed(self) -> None:
        self._cache = None

    # ---- lifecycle --------------------------------------------------------------------
    def install(self, manifest: Manifest, actor: str) -> dict:
        """Install or upgrade. A new install returns its token and signing secret (shown once); an
        upgrade keeps them."""
        existing = self._rows().get(manifest.name)
        doc = manifest.model_dump(mode="json")
        out: dict[str, Any] = {"name": manifest.name, "version": manifest.version, "actor": actor_of(manifest.name)}
        with self.engine.begin() as conn:
            if existing:
                conn.execute(update(extensions).where(extensions.c.name == manifest.name).values(
                    version=manifest.version, manifest=doc, installed_by=actor, installed_ms=int(time.time() * 1000)))
                out["upgraded_from"] = existing["version"]
            else:
                token = f"{PREFIX}{manifest.name}.{secrets.token_urlsafe(32)}"
                secret = secrets.token_hex(32)
                conn.execute(extensions.insert().values(
                    name=manifest.name, version=manifest.version, manifest=doc, token_hash=_hash(token),
                    signing_secret=secret, enabled=True, installed_by=actor, installed_ms=int(time.time() * 1000)))
                out |= {"token": token, "signing_secret": secret}
        self._changed()
        if self.audit:
            self.audit.record("extension", "extension.upgraded" if existing else "extension.installed", actor,
                              manifest.name, {"version": manifest.version, "permissions": manifest.permissions,
                                              "scope": manifest.scope, "events": [e.url for e in manifest.events]})
        return out

    def rotate(self, name: str, actor: str) -> dict:
        self._get(name)
        token = f"{PREFIX}{name}.{secrets.token_urlsafe(32)}"
        secret = secrets.token_hex(32)
        with self.engine.begin() as conn:
            conn.execute(update(extensions).where(extensions.c.name == name).values(
                token_hash=_hash(token), signing_secret=secret))
        self._changed()
        if self.audit:
            self.audit.record("extension", "extension.rotated", actor, name, {})
        return {"name": name, "token": token, "signing_secret": secret}

    def set_enabled(self, name: str, enabled: bool, actor: str) -> dict:
        self._get(name)
        with self.engine.begin() as conn:
            conn.execute(update(extensions).where(extensions.c.name == name).values(enabled=enabled))
        self._changed()
        if self.audit:
            self.audit.record("extension", "extension.enabled" if enabled else "extension.disabled", actor, name, {})
        return self.describe(name)

    def remove(self, name: str, actor: str) -> None:
        self._get(name)
        with self.engine.begin() as conn:
            conn.execute(delete(extensions).where(extensions.c.name == name))
        self._changed()
        if self.audit:
            self.audit.record("extension", "extension.removed", actor, name, {})

    def _get(self, name: str) -> dict:
        row = self._rows().get(name)
        if row is None:
            raise ExtensionError(f"no extension {name!r}", 404)
        return row

    def describe(self, name: str) -> dict:
        r = self._get(name)
        return {"name": r["name"], "version": r["version"], "enabled": r["enabled"], "actor": actor_of(r["name"]),
                "manifest": r["manifest"], "installed_by": r["installed_by"], "installed_ms": r["installed_ms"]}

    def list(self) -> list[dict]:
        return [self.describe(n) for n in sorted(self._rows())]

    # ---- the token --------------------------------------------------------------------
    def actor_for_token(self, token: str) -> Optional[str]:
        """The extension's actor for a valid token of an enabled extension, else None."""
        if not token.startswith(PREFIX):
            return None
        h = _hash(token)
        for r in self._rows().values():
            if hmac.compare_digest(r["token_hash"], h):
                return actor_of(r["name"]) if r["enabled"] else None
        return None

    def allows(self, actor: str, privilege: str, resource_urn: Optional[str]) -> bool:
        if not actor.startswith(ACTOR_PREFIX):
            return False
        row = self._rows().get(actor[len(ACTOR_PREFIX):])
        if row is None or not row["enabled"]:
            return False
        m = row["manifest"]
        if privilege not in m.get("permissions", []):
            return False
        if m.get("scope"):
            if not resource_urn or self.attributes is None:
                return False                                   # a scoped extension acts on resources only
            return evaluate_condition(m["scope"], {"resource": self.attributes.resource(resource_urn)})
        return True

    # ---- events and the workspace -----------------------------------------------------
    def routes(self) -> list:
        """Notification routes for every enabled extension's event hooks."""
        from cairn.notify.hub import Route

        out = []
        for r in self._rows().values():
            if not r["enabled"]:
                continue
            for i, hook in enumerate(r["manifest"].get("events") or []):
                out.append(Route(id=f"ext-{r['name']}-{i}"[:63], event_types=hook.get("types") or [],
                                 domains=hook.get("domains") or [], channel="webhook", target=hook["url"]))
        return out

    def signing_secret_for_route(self, route_id: str) -> Optional[str]:
        if not route_id.startswith("ext-"):
            return None
        for _ in (0, 1):                        # an extension installed through another process: reload once
            for r in self._rows().values():
                if route_id.startswith(f"ext-{r['name']}-"):
                    return r["signing_secret"]
            self._changed()
        return None

    def ui_sections(self) -> list[dict]:
        out = []
        for r in sorted(self._rows().values(), key=lambda r: r["name"]):
            if r["enabled"]:
                for s in r["manifest"].get("ui") or []:
                    out.append({"extension": r["name"], "publisher": r["manifest"]["publisher"], **s})
        return out
