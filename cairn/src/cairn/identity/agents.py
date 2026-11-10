"""AI agents: registered identities that act on behalf of a person, within scopes.

An agent never has permissions of its own. Every request it makes runs as a person: the agent's
owner (agent keys) or the person who signed in through it (OAuth). What it may do is that person's
permissions **narrowed** by the agent's scopes:

* ``read``: read the catalog (every GET, and the POSTs that change nothing);
* ``query_data``: preview tables, run SQL, ask questions of data (masked as for the person);
* ``propose``: create things that wait for a person's review (documentation drafts, workflows,
  access requests, discussions, steward runs);
* ``write``: change metadata directly;
* ``iac:propose``: propose infrastructure-as-code change requests (``/v1/iac/changes``) for a person to approve;
  an agent never approves or applies one.

Administration (``/admin``, licences, other agents) is never reachable with an agent credential.

Credentials:

* **Agent keys** ``ca_<id>.<secret>``: made by an admin for an automation, acting as its owner.
* **OAuth 2.1** for MCP clients (Claude Desktop, Claude Code, IDEs): dynamic client registration,
  authorization code with PKCE (S256) through the person's normal sign-in and a consent page, then
  access tokens ``cat_…`` (one hour) and rotating refresh tokens ``car_…`` (30 days).

An agent linked to an AI system in the register only works while that system is approved. Every
audit entry made through an agent names it.

Control (the agent hub): administrators can pause every agent at once; each agent has request
quotas (per minute and per day) and an autonomy level: ``suggest`` (everything it proposes waits for
review) or ``auto`` (its suggestions at or above a confidence apply at once, when the person it acts
for may make that change).
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import BigInteger, Boolean, Column, Index, Integer, String, Table, delete, select, update

from cairn.common.db import JSONText, metadata

SCOPES = ("read", "query_data", "propose", "write", "model", "memory", "iac:propose")
DEFAULT_OAUTH_SCOPES = ("read", "query_data", "propose")
SCOPE_TEXT = {
    "read": "Read the catalog: assets, lineage, quality, definitions",
    "query_data": "Preview tables and run read-only queries, masked as for you",
    "propose": "Propose changes for review (documentation, requests, discussions)",
    "write": "Change metadata directly (tags, owners, descriptions)",
    "model": "Call language models through Cairn's AI gateway, metered to you",
    "memory": "Remember and recall what it learns about you, which you can see and delete",
    "iac:propose": "Propose changes to Cairn's resources as code, which a person must approve before they apply",
}
KEY_PREFIX, ACCESS_PREFIX, REFRESH_PREFIX = "ca_", "cat_", "car_"
ACCESS_TTL_MS, REFRESH_TTL_MS, CODE_TTL_MS = 3600_000, 30 * 86400_000, 600_000
MAX_ACCESS_TTL_MS = 4 * 3600_000

agent_clients = Table(
    "cairn_agent_clients", metadata,
    Column("id", String(40), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("kind", String(8), nullable=False),                  # key | oauth
    Column("owner", String(512), nullable=True),                # key agents act as their owner
    Column("scopes", JSONText, nullable=False),
    Column("redirect_uris", JSONText, nullable=False),
    Column("ai_system", String(40), nullable=True),             # register entry it must stay approved in
    Column("key_hash", String(64), nullable=True, unique=True),
    Column("enabled", Boolean, nullable=False),
    Column("details", JSONText, nullable=False),                # client_uri, software, description
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("last_used_ms", BigInteger, nullable=True),
)
agent_tokens = Table(
    "cairn_agent_tokens", metadata,
    Column("hash", String(64), primary_key=True),
    Column("kind", String(8), nullable=False),                  # code | access | refresh
    Column("client_id", String(40), nullable=False, index=True),
    Column("actor", String(512), nullable=False),
    Column("scopes", JSONText, nullable=False),
    Column("expires_ms", BigInteger, nullable=False),
    Column("revoked", Boolean, nullable=False),
    Column("details", JSONText, nullable=False),                # code: challenge, redirect_uri, resource
    Column("created_ms", BigInteger, nullable=False),
)
Index("ix_cairn_agent_tokens_actor", agent_tokens.c.actor)
agent_settings = Table(
    "cairn_agent_settings", metadata,
    Column("key", String(32), primary_key=True),                # "global"
    Column("value", JSONText, nullable=False),
)
agent_usage = Table(
    "cairn_agent_usage", metadata,
    Column("agent", String(40), primary_key=True),
    Column("day", String(10), primary_key=True),                # UTC date
    Column("requests", Integer, nullable=False),
)
DEFAULT_POLICY = {"autonomy": "suggest", "auto_accept_at": 0.9, "per_minute": 120, "daily_requests": 20_000}

# The agent behind the current request (audit attribution); set where the principal is resolved.
current_agent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("cairn_agent", default=None)


class AgentError(ValueError):
    def __init__(self, message: str, status: int = 422, oauth_error: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.oauth_error = oauth_error


class AgentsPaused(Exception):
    pass


@dataclass(frozen=True)
class AgentGrant:
    """What a valid agent credential resolves to."""
    actor: str
    agent: str
    scopes: tuple[str, ...]
    run: Optional[str] = None          # access tokens: the run (``run_…``) or OAuth session (``tok_…``) they belong to
    intent: Any = None                 # the declared intent (cairn.identity.intents.Intent), if any
    identity: str = "person"           # who ``actor`` is: a person, or a share's recipient (``share-recipient``)
    sponsor: Optional[str] = None      # share-recipient runs: the provider who attached the agent (decides models)


def _now() -> int:
    return int(time.time() * 1000)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _check_scopes(scopes: list[str]) -> list[str]:
    bad = sorted(set(scopes) - set(SCOPES))
    if bad:
        raise AgentError(f"unknown scopes {bad}; scopes are {list(SCOPES)}")
    return [s for s in SCOPES if s in scopes]


def valid_redirect(uri: str) -> bool:
    """https anywhere, http only to the loopback interface (native and CLI clients), or a private-use
    scheme (``com.example.app:/cb``); never a fragment."""
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    if u.fragment or not u.scheme:
        return False
    if u.scheme == "https":
        return bool(u.netloc)
    if u.scheme == "http":
        return u.hostname in ("localhost", "127.0.0.1", "::1")
    return "." in u.scheme and u.scheme not in ("javascript", "data", "file")


class AgentIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    owner: Optional[str] = Field(default=None, pattern=r"^urn:li:corpuser:")
    ai_system: Optional[str] = None
    description: str = Field(default="", max_length=2000)

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: list[str]) -> list[str]:
        return _check_scopes(v)


class AgentPatch(BaseModel):
    scopes: Optional[list[str]] = None
    enabled: Optional[bool] = None
    ai_system: Optional[str] = None
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    autonomy: Optional[Literal["suggest", "auto"]] = None
    auto_accept_at: Optional[float] = Field(default=None, ge=0.5, le=1.0)
    per_minute: Optional[int] = Field(default=None, ge=1, le=10_000)
    daily_requests: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    intent: Optional[dict] = None          # a key agent's declared intent (cairn.identity.intents); {} clears it

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return None if v is None else _check_scopes(v)

    @field_validator("intent")
    @classmethod
    def _intent(cls, v: Optional[dict]) -> Optional[dict]:
        from cairn.identity.intents import parse_intent

        return None if v is None else ({} if v == {} else parse_intent(v).as_dict())


class ClientRegistration(BaseModel):
    """RFC 7591 dynamic client registration request (the fields Cairn uses)."""
    client_name: str = Field(default="AI agent", max_length=200)
    redirect_uris: list[str] = Field(min_length=1, max_length=10)
    client_uri: Optional[str] = Field(default=None, max_length=500)
    software_id: Optional[str] = Field(default=None, max_length=200)
    software_version: Optional[str] = Field(default=None, max_length=50)
    scope: Optional[str] = None
    token_endpoint_auth_method: Literal["none"] = "none"
    grant_types: list[str] = Field(default_factory=lambda: ["authorization_code", "refresh_token"])
    response_types: list[str] = Field(default_factory=lambda: ["code"])


class AgentService:
    def __init__(self, engine, audit=None, ai_system_state: Optional[Callable[[str], Optional[str]]] = None,
                 registration: Literal["open", "admin", "off"] = "open"):
        """``ai_system_state(id)``: the register state of an AI system (``APPROVED``…), or None."""
        self.engine = engine
        self.audit = audit
        self.ai_system_state = ai_system_state
        self.registration = registration
        self._touched: dict[str, int] = {}

    # ---- clients -------------------------------------------------------------------------
    @staticmethod
    def _policy(details: Optional[dict]) -> dict:
        return {**DEFAULT_POLICY, **((details or {}).get("policy") or {})}

    def policy(self, agent_id: str) -> dict:
        with self.engine.connect() as conn:
            d = conn.execute(select(agent_clients.c.details).where(agent_clients.c.id == agent_id)).scalar()
        return self._policy(d)

    @classmethod
    def _public(cls, r) -> dict:
        return {"id": r.id, "name": r.name, "kind": r.kind, "owner": r.owner, "scopes": list(r.scopes),
                **cls._policy(r.details),
                "redirect_uris": list(r.redirect_uris), "ai_system": r.ai_system, "enabled": r.enabled,
                "created_by": r.created_by, "created_ms": r.created_ms, "last_used_ms": r.last_used_ms,
                "intent": (r.details or {}).get("intent"),
                **{k: v for k, v in (r.details or {}).items() if k in ("client_uri", "software_id", "description")}}

    def get(self, agent_id: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(agent_clients).where(agent_clients.c.id == agent_id)).first()
        if r is None:
            raise AgentError("no such agent", 404, "invalid_client")
        return self._public(r)

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(agent_clients).order_by(agent_clients.c.created_ms)).all()
        return [self._public(r) for r in rows]

    def _record(self, action: str, actor: str, agent_id: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("agents", action, actor, agent_id, details)

    def create_key_agent(self, spec: AgentIn, actor: str) -> dict:
        """An agent for an automation; its key (shown once) acts as ``spec.owner`` (default: the creator)."""
        agent_id = f"agt_{uuid.uuid4().hex[:12]}"
        key = f"{KEY_PREFIX}{agent_id}.{secrets.token_urlsafe(32)}"
        with self.engine.begin() as conn:
            conn.execute(agent_clients.insert().values(
                id=agent_id, name=spec.name, kind="key", owner=spec.owner or actor, scopes=spec.scopes,
                redirect_uris=[], ai_system=spec.ai_system, key_hash=_hash(key), enabled=True,
                details={"description": spec.description}, created_by=actor, created_ms=_now()))
        self._record("agent.created", actor, agent_id, {"name": spec.name, "scopes": spec.scopes,
                                                        "owner": spec.owner or actor})
        return {**self.get(agent_id), "key": key}

    def create_built_agent(self, name: str, owner: str, scopes: list[str], ai_system: Optional[str],
                           description: str, actor: str) -> dict:
        """The identity of an agent built and run in Cairn (``cairn.ai.custom_agents``): no key or token; it acts
        only for whoever runs it, narrowed to ``scopes``, under the same pause, quotas and AI system checks."""
        agent_id = f"agt_{uuid.uuid4().hex[:12]}"
        with self.engine.begin() as conn:
            conn.execute(agent_clients.insert().values(
                id=agent_id, name=name, kind="built", owner=owner, scopes=_check_scopes(scopes), redirect_uris=[],
                ai_system=ai_system, key_hash=None, enabled=True, details={"description": description},
                created_by=actor, created_ms=_now()))
        self._record("agent.created", actor, agent_id, {"name": name, "scopes": scopes, "kind": "built"})
        return self.get(agent_id)

    def issue_run_token(self, agent_id: str, actor: str, ttl_ms: int = 900_000, intent: Any = None,
                        scopes: Optional[list[str]] = None, identity: str = "person",
                        sponsor: Optional[str] = None) -> str:
        """A short-lived access token for one run of a Cairn-hosted agent (built or deployed app) on behalf of
        ``actor``: their rights narrowed to the agent's scopes (and to ``scopes`` when given, never wider), and to
        ``intent`` (an :class:`~cairn.identity.intents.Intent`) when one is declared. ``identity`` says who ``actor``
        is: ``person``, or ``share-recipient`` with the providing ``sponsor``. No refresh token; revoke it when the
        run ends."""
        if identity not in ("person", "share-recipient") or (identity == "share-recipient" and not sponsor):
            raise AgentError("identity: person, or share-recipient with its sponsor")
        with self.engine.begin() as conn:
            client = conn.execute(select(agent_clients).where(agent_clients.c.id == agent_id)).first()
            if client is None or client.kind not in ("built", "app") or not client.enabled:
                raise AgentError("no such agent", 404)
            granted = [s for s in client.scopes if scopes is None or s in scopes]
            token, now = ACCESS_PREFIX + secrets.token_urlsafe(32), _now()
            details: dict = {"run": True, **({"intent": intent.as_dict()} if intent is not None else {})}
            if identity != "person":
                details.update(identity=identity, sponsor=sponsor)
            conn.execute(agent_tokens.insert().values(
                hash=_hash(token), kind="access", client_id=agent_id, actor=actor, scopes=granted,
                expires_ms=now + ttl_ms, revoked=False, created_ms=now, details=details))
        return token

    def system_agent(self, agent_id: str, name: str, scopes: list[str], description: str) -> str:
        """The identity of one of Cairn's own runtimes (code sandboxes): a built agent with a fixed id, made once.
        Like any agent it holds no key, obeys the pause switch, and acts only for the person it runs for."""
        with self.engine.connect() as conn:
            if conn.execute(select(agent_clients.c.id).where(agent_clients.c.id == agent_id)).first():
                return agent_id
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(agent_clients.insert().values(
                    id=agent_id, name=name, kind="built", owner="urn:li:corpuser:__cairn__",
                    scopes=_check_scopes(scopes), redirect_uris=[], ai_system=None, key_hash=None, enabled=True,
                    details={"description": description, "system": True}, created_by="urn:li:corpuser:__cairn__",
                    created_ms=_now()))
        except IntegrityError:                     # another replica made it first
            pass
        return agent_id

    def create_app_agent(self, name: str, owner: str, scopes: list[str], description: str, actor: str) -> dict:
        """The identity of a code-first agent app deployed to Cairn (``cairn.ai.agent_apps``): like a built agent,
        it has no key; each run gets a short-lived token for the person who asked (:meth:`issue_run_token`)."""
        agent_id = f"agt_{uuid.uuid4().hex[:12]}"
        with self.engine.begin() as conn:
            conn.execute(agent_clients.insert().values(
                id=agent_id, name=name, kind="app", owner=owner, scopes=_check_scopes(scopes), redirect_uris=[],
                ai_system=None, key_hash=None, enabled=True, details={"description": description},
                created_by=actor, created_ms=_now()))
        self._record("agent.created", actor, agent_id, {"name": name, "scopes": scopes, "kind": "app"})
        return self.get(agent_id)

    def runnable(self, agent_id: str) -> Optional[str]:
        """Why the agent may not act now (paused, disabled, its AI system not approved), or None."""
        if self.paused():
            return "AI agents are paused by an administrator"
        with self.engine.connect() as conn:
            client = conn.execute(select(agent_clients).where(agent_clients.c.id == agent_id)).first()
        if client is None or not client.enabled:
            return "this agent is disabled"
        if client.ai_system and (self.ai_system_state is None or self.ai_system_state(client.ai_system) != "APPROVED"):
            return f"its AI system {client.ai_system} is not approved"
        return None

    def set_scopes(self, agent_id: str, scopes: list[str], ai_system: Optional[str], actor: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(agent_clients).where(agent_clients.c.id == agent_id)
                         .values(scopes=_check_scopes(scopes), ai_system=ai_system))
        self._record("agent.updated", actor, agent_id, {"scopes": scopes, "ai_system": ai_system})

    def register_client(self, req: ClientRegistration) -> dict:
        """RFC 7591: a public client (PKCE, no secret). With registration ``admin`` it starts disabled
        until an administrator enables it."""
        if self.registration == "off":
            raise AgentError("dynamic client registration is turned off; ask an administrator", 403,
                             "access_denied")
        bad = [u for u in req.redirect_uris if not valid_redirect(u)]
        if bad:
            raise AgentError(f"redirect_uris must be https, loopback http or a private-use scheme: {bad}",
                             400, "invalid_redirect_uri")
        if "authorization_code" not in req.grant_types or req.response_types != ["code"]:
            raise AgentError("only the authorization code flow is supported", 400, "invalid_client_metadata")
        wanted = [s for s in (req.scope or "").split() if s]
        scopes = _check_scopes(wanted) if wanted else list(DEFAULT_OAUTH_SCOPES)
        agent_id = f"agt_{uuid.uuid4().hex[:12]}"
        enabled = self.registration == "open"
        with self.engine.begin() as conn:
            conn.execute(agent_clients.insert().values(
                id=agent_id, name=req.client_name, kind="oauth", owner=None, scopes=scopes,
                redirect_uris=req.redirect_uris, ai_system=None, key_hash=None, enabled=enabled,
                details={"client_uri": req.client_uri, "software_id": req.software_id,
                         "software_version": req.software_version},
                created_by="dynamic-registration", created_ms=_now()))
        self._record("agent.registered", "dynamic-registration", agent_id,
                     {"name": req.client_name, "redirect_uris": req.redirect_uris, "enabled": enabled})
        return {"client_id": agent_id, "client_name": req.client_name, "redirect_uris": req.redirect_uris,
                "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"], "scope": " ".join(scopes), "client_id_issued_at": _now() // 1000}

    def update(self, agent_id: str, patch: AgentPatch, actor: str) -> dict:
        self.get(agent_id)
        values = {k: v for k, v in patch.model_dump().items() if v is not None}
        policy = {k: values.pop(k) for k in list(values) if k in DEFAULT_POLICY}
        intent = values.pop("intent", None)
        if intent is not None and self.get(agent_id)["kind"] != "key":
            raise AgentError("only key agents carry an intent of their own; runs of built agents and agent apps "
                             "declare theirs")
        if policy or intent is not None:
            with self.engine.connect() as conn:
                details = conn.execute(select(agent_clients.c.details).where(agent_clients.c.id == agent_id)).scalar()
            details = {**(details or {})}
            if policy:
                details["policy"] = {**(details.get("policy") or {}), **policy}
            if intent is not None:
                details.pop("intent", None)
                if intent:
                    details["intent"] = intent
            values["details"] = details
        if values:
            with self.engine.begin() as conn:
                conn.execute(update(agent_clients).where(agent_clients.c.id == agent_id).values(**values))
            if values.get("enabled") is False:
                self._revoke(agent_id)
        self._record("agent.updated", actor, agent_id, {**{k: v for k, v in values.items() if k != "details"},
                                                        **policy, **({"intent": intent or None}
                                                                     if intent is not None else {})})
        return self.get(agent_id)

    def rotate_key(self, agent_id: str, actor: str) -> dict:
        a = self.get(agent_id)
        if a["kind"] != "key":
            raise AgentError("only key agents have a key; OAuth clients sign in again instead")
        key = f"{KEY_PREFIX}{agent_id}.{secrets.token_urlsafe(32)}"
        with self.engine.begin() as conn:
            conn.execute(update(agent_clients).where(agent_clients.c.id == agent_id).values(key_hash=_hash(key)))
        self._record("agent.rotated", actor, agent_id, {})
        return {**a, "key": key}

    def delete(self, agent_id: str, actor: str) -> None:
        self.get(agent_id)
        with self.engine.begin() as conn:
            conn.execute(delete(agent_tokens).where(agent_tokens.c.client_id == agent_id))
            conn.execute(delete(agent_clients).where(agent_clients.c.id == agent_id))
        self._record("agent.deleted", actor, agent_id, {})

    def _revoke(self, agent_id: str, actor: Optional[str] = None) -> int:
        q = update(agent_tokens).where(agent_tokens.c.client_id == agent_id, agent_tokens.c.revoked.is_(False))
        if actor:
            q = q.where(agent_tokens.c.actor == actor)
        with self.engine.begin() as conn:
            return conn.execute(q.values(revoked=True)).rowcount

    # ---- the hub: pause, quotas, usage ---------------------------------------------------------
    def settings(self) -> dict:
        with self.engine.connect() as conn:
            v = conn.execute(select(agent_settings.c.value).where(agent_settings.c.key == "global")).scalar()
        return {"paused": False, **(v or {})}

    def pause(self, paused: bool, actor: str) -> dict:
        value = {"paused": paused, "changed_by": actor, "changed_ms": _now()}
        with self.engine.begin() as conn:
            if conn.execute(update(agent_settings).where(agent_settings.c.key == "global").values(value=value)).rowcount == 0:
                conn.execute(agent_settings.insert().values(key="global", value=value))
        self._paused_cache = (0, paused)
        self._record("agents.paused" if paused else "agents.resumed", actor, "all", {})
        return self.settings()

    def paused(self) -> bool:
        now = time.monotonic()
        at, value = getattr(self, "_paused_cache", (0, False))
        if now - at > 5:                       # cheap enough per request, fresh within seconds everywhere
            value = bool(self.settings().get("paused"))
            self._paused_cache = (now, value)
        return value

    def usage_today(self) -> dict[str, int]:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.engine.connect() as conn:
            return {r.agent: r.requests for r in conn.execute(select(agent_usage).where(agent_usage.c.day == day))}

    def admit(self, agent_id: str) -> None:
        """Count one request for the agent; AgentError 429 when over its per-minute or daily quota."""
        policy, minute = self.policy(agent_id), int(time.time() // 60)
        window = getattr(self, "_minute", {})
        key = (agent_id, minute)
        window = {k: v for k, v in window.items() if k[1] == minute}
        window[key] = window.get(key, 0) + 1
        self._minute = window
        if window[key] > int(policy["per_minute"]):
            raise AgentError(f"this agent is over its limit of {policy['per_minute']} requests a minute", 429,
                             "rate_limited")
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.engine.begin() as conn:
            q = update(agent_usage).where(agent_usage.c.agent == agent_id, agent_usage.c.day == day)
            if conn.execute(q.values(requests=agent_usage.c.requests + 1)).rowcount == 0:
                conn.execute(agent_usage.insert().values(agent=agent_id, day=day, requests=1))
            used = conn.execute(select(agent_usage.c.requests).where(agent_usage.c.agent == agent_id,
                                                                     agent_usage.c.day == day)).scalar()
        if used > int(policy["daily_requests"]):
            raise AgentError(f"this agent used its {policy['daily_requests']} requests for today", 429, "rate_limited")

    # ---- a person's connected agents --------------------------------------------------------
    def connected(self, actor: str) -> list[dict]:
        """Agents acting for ``actor``: OAuth clients they signed in to, and key agents they own."""
        now = _now()
        with self.engine.connect() as conn:
            grants = conn.execute(select(agent_tokens.c.client_id, agent_tokens.c.scopes, agent_tokens.c.created_ms)
                                  .where(agent_tokens.c.actor == actor, agent_tokens.c.kind == "refresh",
                                         agent_tokens.c.revoked.is_(False), agent_tokens.c.expires_ms > now)).all()
            owned = conn.execute(select(agent_clients).where(agent_clients.c.owner == actor)).all()
        seen: dict[str, dict] = {}
        for g in grants:
            try:
                a = self.get(g.client_id)
            except AgentError:
                continue
            item = seen.setdefault(a["id"], {**a, "granted_scopes": list(g.scopes), "since_ms": g.created_ms})
            item["since_ms"] = min(item["since_ms"], g.created_ms)
        for r in owned:
            seen.setdefault(r.id, {**self._public(r), "granted_scopes": list(r.scopes), "since_ms": r.created_ms})
        return list(seen.values())

    def ensure_client(self, client_id: str, name: str, redirect_uris: list[str], scopes: list[str],
                      details: Optional[dict] = None) -> dict:
        """An OAuth client the operator configures (not dynamically registered): created, or brought in line with
        the configuration (its name, redirect URIs and scopes), with a fixed id. With no redirect URI it is
        disabled, so a client whose configuration was removed stops working. An administrator's own disable is
        kept."""
        bad = [u for u in redirect_uris if not valid_redirect(u)]
        if bad:
            raise AgentError(f"redirect_uris must be https, loopback http or a private-use scheme: {bad}")
        scopes = _check_scopes(scopes)
        event: Optional[tuple[str, dict]] = None
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_clients).where(agent_clients.c.id == client_id)).first()
            if row is None:
                if not redirect_uris:
                    return {}
                conn.execute(agent_clients.insert().values(
                    id=client_id, name=name, kind="oauth", owner=None, scopes=scopes, redirect_uris=redirect_uris,
                    ai_system=None, key_hash=None, enabled=True, details={**(details or {}), "configured": True},
                    created_by="configuration", created_ms=_now()))
                event = ("agent.registered", {"name": name, "redirect_uris": redirect_uris, "enabled": True})
            elif row.kind != "oauth" or row.created_by != "configuration":
                raise AgentError(f"agent id {client_id} is taken by another agent", 409)
            else:
                merged = {**(row.details or {}), **(details or {}), "configured": True}
                changes: dict[str, Any] = {"name": name, "redirect_uris": redirect_uris, "scopes": scopes,
                                           "details": merged}
                if not redirect_uris:
                    changes.update(enabled=False, details={**merged, "disabled_by_configuration": True})
                elif merged.pop("disabled_by_configuration", False):   # configured again: back on (an
                    changes["enabled"] = True                          # administrator's own disable is kept)
                if any(getattr(row, k) != v for k, v in changes.items()):
                    conn.execute(update(agent_clients).where(agent_clients.c.id == client_id).values(**changes))
                    event = ("agent.updated", {"redirect_uris": redirect_uris, "scopes": scopes})
        if event is not None:
            self._record(event[0], "configuration", client_id, event[1])
        if not redirect_uris:
            self._revoke(client_id)
        return self.get(client_id)

    def disconnect(self, agent_id: str, actor: str) -> int:
        n = self._revoke(agent_id, actor)
        self._record("agent.disconnected", actor, agent_id, {"tokens": n})
        return n

    # ---- OAuth: authorization code with PKCE ------------------------------------------------
    def check_authorization(self, client_id: str, redirect_uri: str, code_challenge: str,
                            method: str, scope: Optional[str]) -> tuple[dict, list[str]]:
        """Validate an authorization request; returns the client and the scopes it may be granted."""
        try:
            client = self.get(client_id)
        except AgentError:
            raise AgentError("unknown client", 400, "invalid_client")
        if client["kind"] != "oauth":
            raise AgentError("this agent uses a key, not sign-in", 400, "unauthorized_client")
        if redirect_uri not in client["redirect_uris"]:
            raise AgentError("redirect_uri is not registered for this client", 400, "invalid_request")
        if not client["enabled"]:
            raise AgentError("an administrator has not enabled this agent yet", 403, "access_denied")
        if method != "S256" or not (43 <= len(code_challenge) <= 128):
            raise AgentError("PKCE with code_challenge_method S256 is required", 400, "invalid_request")
        wanted = [s for s in (scope or "").split() if s] or client["scopes"]
        _check_scopes(wanted)
        return client, [s for s in wanted if s in client["scopes"]]

    def issue_code(self, client_id: str, actor: str, scopes: list[str], redirect_uri: str,
                   code_challenge: str, resource: Optional[str]) -> str:
        code = secrets.token_urlsafe(32)
        with self.engine.begin() as conn:
            conn.execute(agent_tokens.insert().values(
                hash=_hash(code), kind="code", client_id=client_id, actor=actor, scopes=scopes,
                expires_ms=_now() + CODE_TTL_MS, revoked=False, created_ms=_now(),
                details={"challenge": code_challenge, "redirect_uri": redirect_uri, "resource": resource}))
        self._record("agent.authorized", actor, client_id, {"scopes": scopes})
        return code

    def _tokens(self, conn, client_id: str, actor: str, scopes: list, resource: Optional[str]) -> dict:
        access, refresh, now = ACCESS_PREFIX + secrets.token_urlsafe(32), REFRESH_PREFIX + secrets.token_urlsafe(32), _now()
        details = conn.execute(select(agent_clients.c.details).where(agent_clients.c.id == client_id)).scalar() or {}
        access_ttl = min(int(details.get("access_ttl_ms") or ACCESS_TTL_MS), MAX_ACCESS_TTL_MS)   # configured clients
        for tok, kind, ttl in ((access, "access", access_ttl), (refresh, "refresh", REFRESH_TTL_MS)):
            conn.execute(agent_tokens.insert().values(
                hash=_hash(tok), kind=kind, client_id=client_id, actor=actor, scopes=list(scopes),
                expires_ms=now + ttl, revoked=False, created_ms=now, details={"resource": resource}))
        return {"access_token": access, "token_type": "Bearer", "expires_in": access_ttl // 1000,
                "refresh_token": refresh, "scope": " ".join(scopes)}

    def exchange_code(self, code: str, client_id: str, redirect_uri: str, verifier: str) -> dict:
        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_tokens).where(agent_tokens.c.hash == _hash(code),
                                                          agent_tokens.c.kind == "code")).first()
            if row is None or row.client_id != client_id or (row.expires_ms < now and not row.revoked):
                raise AgentError("the authorization code is invalid or expired", 400, "invalid_grant")
            if not row.revoked:
                if row.details.get("redirect_uri") != redirect_uri:
                    raise AgentError("redirect_uri does not match the authorization request", 400, "invalid_grant")
                digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if not secrets.compare_digest(digest, row.details.get("challenge") or ""):
                    raise AgentError("code_verifier does not match the code challenge", 400, "invalid_grant")
                conn.execute(update(agent_tokens).where(agent_tokens.c.hash == row.hash).values(revoked=True))
                return self._tokens(conn, client_id, row.actor, row.scopes, row.details.get("resource"))
        # The code was used before: someone replayed it. Revoke everything it produced, then refuse.
        with self.engine.begin() as conn:
            conn.execute(update(agent_tokens).where(agent_tokens.c.client_id == row.client_id,
                                                    agent_tokens.c.actor == row.actor,
                                                    agent_tokens.c.created_ms >= row.created_ms).values(revoked=True))
        self._record("agent.code_replayed", row.actor, row.client_id, {})
        raise AgentError("the authorization code was already used; its tokens are revoked", 400, "invalid_grant")

    def refresh(self, token: str, client_id: str) -> dict:
        """Rotating refresh: the old refresh token stops working; reuse of a spent one revokes the grant."""
        now = _now()
        with self.engine.begin() as conn:
            row = conn.execute(select(agent_tokens).where(agent_tokens.c.hash == _hash(token),
                                                          agent_tokens.c.kind == "refresh")).first()
            if row is None or row.client_id != client_id:
                raise AgentError("unknown refresh token", 400, "invalid_grant")
            reused = row.revoked
            if not reused and row.expires_ms < now:
                raise AgentError("refresh token expired; sign in again", 400, "invalid_grant")
            if not reused:
                client = conn.execute(select(agent_clients).where(agent_clients.c.id == client_id)).first()
                if client is None or not client.enabled:
                    raise AgentError("this agent is disabled", 400, "invalid_grant")
                conn.execute(update(agent_tokens).where(agent_tokens.c.hash == row.hash).values(revoked=True))
                scopes = [s for s in row.scopes if s in client.scopes]       # an admin may have narrowed it
                return self._tokens(conn, client_id, row.actor, scopes, row.details.get("resource"))
        self._revoke(client_id, row.actor)        # a spent refresh token came back: end the whole grant
        self._record("agent.refresh_reused", row.actor, client_id, {})
        raise AgentError("refresh token already used; sign in again", 400, "invalid_grant")

    def revoke_token(self, token: str) -> None:
        """RFC 7009: unknown tokens are not an error."""
        with self.engine.begin() as conn:
            conn.execute(update(agent_tokens).where(agent_tokens.c.hash == _hash(token)).values(revoked=True))

    # ---- resolving a credential -----------------------------------------------------------------
    def resolve(self, token: str) -> Optional[AgentGrant]:
        """The grant behind an agent key or access token, or None when it is not valid now. Raises
        :class:`AgentsPaused` while an administrator has paused every agent."""
        if self.paused():
            raise AgentsPaused("AI agents are paused by an administrator")
        now = _now()
        with self.engine.connect() as conn:
            if token.startswith(KEY_PREFIX):
                client = conn.execute(select(agent_clients).where(agent_clients.c.key_hash == _hash(token))).first()
                if client is None or client.kind != "key":
                    return None
                actor, scopes, run = client.owner, list(client.scopes), None
                declared = (client.details or {}).get("intent")
                identity, sponsor = "person", None
            elif token.startswith(ACCESS_PREFIX):
                row = conn.execute(select(agent_tokens).where(agent_tokens.c.hash == _hash(token),
                                                              agent_tokens.c.kind == "access")).first()
                if row is None or row.revoked or row.expires_ms < now:
                    return None
                client = conn.execute(select(agent_clients).where(agent_clients.c.id == row.client_id)).first()
                if client is None:
                    return None
                actor, scopes = row.actor, [s for s in row.scopes if s in client.scopes]
                run = ("run_" if (row.details or {}).get("run") else "tok_") + row.hash[:16]
                declared = (row.details or {}).get("intent")
                identity = (row.details or {}).get("identity") or "person"
                sponsor = (row.details or {}).get("sponsor")
            else:
                return None
        if not client.enabled or not actor:
            return None
        if client.ai_system and self.ai_system_state is not None and self.ai_system_state(client.ai_system) != "APPROVED":
            return None
        from cairn.identity.intents import IntentError, parse_intent

        try:
            intent = parse_intent(declared)
        except IntentError:
            return None                          # an intent that cannot be read: the credential does not work
        self._touch(client.id, now)
        return AgentGrant(actor, client.id, tuple(scopes), run, intent, identity, sponsor)

    def _touch(self, agent_id: str, now: int) -> None:
        if now - self._touched.get(agent_id, 0) < 60_000:       # at most once a minute per agent
            return
        self._touched[agent_id] = now
        with self.engine.begin() as conn:
            conn.execute(update(agent_clients).where(agent_clients.c.id == agent_id).values(last_used_ms=now))


# ---- what a scope allows over HTTP ---------------------------------------------------------------
QUERY_DATA_PATHS = ("/data/preview", "/data/query", "/data/ask", "/context/metrics/{name}/query",
                    "/v1/sandboxes/runs",                       # code in a sandbox may read data as the person
                    "/data/sheets/query", "/data/sheets/metric", "/data/sheets/ask")
# Exactly these create something that waits for a person (deciding on it is a write).
PROPOSE_ROUTES = {"/ai/docs/generate", "/governance/workflows", "/marketplace/access-requests", "/collab/threads",
                  "/collab/threads/{thread_id}/comments", "/ai/steward/run"}
NEVER_PATHS = ("/admin", "/oauth", "/me/agents", "/v1/me", "/v1/me/agent-memories",   # not even to read (the person's own)
               "/v1/people")                                    # the directory: who holds what is for administrators
MODEL_PATHS = ("/v1/ai/gateway",)                              # the AI gateway: scope "model", reads included
MEMORY_PATHS = ("/v1/agent-memory",)                           # agent memory (cairn.ai.agent_memory): scope "memory"
READ_ONLY_PATHS = ("/governance/policies", "/governance/ai-systems", "/governance/personas", "/governance/purposes")
# Infrastructure-as-code change requests (cairn.iac.changes): agents plan (a read) and propose; people decide and apply.
IAC_PROPOSE_ROUTES = {("POST", "/v1/iac/changes"), ("POST", "/v1/iac/changes/{cid}/commit"),
                      ("DELETE", "/v1/iac/changes/{cid}")}
IAC_PERSON_ROUTES = {"/v1/iac/changes/draft", "/v1/iac/changes/{cid}/approve", "/v1/iac/changes/{cid}/reject",
                     "/v1/iac/changes/{cid}/apply"}


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def required_scope(method: str, path: str, read_posts: set[str]) -> Optional[str]:
    """The scope an agent needs for ``method path`` (the route template, e.g. ``/x/{id}``); None when
    no agent credential may do it."""
    reading = method in ("GET", "HEAD", "OPTIONS")
    if _under(path, NEVER_PATHS) or (_under(path, READ_ONLY_PATHS) and not reading):
        return None
    if path in IAC_PERSON_ROUTES:
        return None
    if (method, path) in IAC_PROPOSE_ROUTES:
        return "iac:propose"
    if method == "POST" and path == "/v1/iac/changes/plan":
        return "read"
    if _under(path, MODEL_PATHS):
        return "model"
    if _under(path, MEMORY_PATHS):
        return "memory"
    if path in QUERY_DATA_PATHS:
        return "query_data"
    if reading or path in read_posts:
        return "read"
    if method == "POST" and path in PROPOSE_ROUTES:
        return "propose"
    return "write"


def tool_scope(tool: str) -> str:
    """The scope an agent needs for an MCP tool."""
    if tool.startswith("suggest_") or tool in ("generate_documentation", "request_access", "start_discussion"):
        return "propose"
    if tool in ("iac_propose", "iac_open_pr"):
        return "iac:propose"
    if tool == "iac_apply":
        return "never"                                   # a person applies what they approved; no agent scope does
    return {"ask_data": "query_data", "query_metric": "query_data", "run_python": "query_data",
            "run_flow": "write"}.get(tool, "read")


def describe(scopes: list[str]) -> list[dict[str, Any]]:
    return [{"scope": s, "text": SCOPE_TEXT[s]} for s in scopes]
