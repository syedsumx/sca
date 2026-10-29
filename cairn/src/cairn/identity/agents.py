"""AI agents: registered identities that act on behalf of a person, within scopes.

An agent never has permissions of its own. Every request it makes runs as a person: the agent's
owner (agent keys) or the person who signed in through it (OAuth). What it may do is that person's
permissions **narrowed** by the agent's scopes:

* ``read``: read the catalog (every GET, and the POSTs that change nothing);
* ``query_data``: preview tables, run SQL, ask questions of data (masked as for the person);
* ``propose``: create things that wait for a person's review (documentation drafts, workflows,
  access requests, discussions, steward runs);
* ``write``: change metadata directly.

Administration (``/admin``, licences, other agents) is never reachable with an agent credential.

Credentials:

* **Agent keys** ``ca_<id>.<secret>``: made by an admin for an automation, acting as its owner.
* **OAuth 2.1** for MCP clients (Claude Desktop, Claude Code, IDEs): dynamic client registration,
  authorization code with PKCE (S256) through the person's normal sign-in and a consent page, then
  access tokens ``cat_…`` (one hour) and rotating refresh tokens ``car_…`` (30 days).

An agent linked to an AI system in the register only works while that system is approved. Every
audit entry made through an agent names it.
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
from sqlalchemy import BigInteger, Boolean, Column, Index, String, Table, delete, select, update

from cairn.common.db import JSONText, metadata

SCOPES = ("read", "query_data", "propose", "write")
DEFAULT_OAUTH_SCOPES = ("read", "query_data", "propose")
SCOPE_TEXT = {
    "read": "Read the catalog: assets, lineage, quality, definitions",
    "query_data": "Preview tables and run read-only queries, masked as for you",
    "propose": "Propose changes for review (documentation, requests, discussions)",
    "write": "Change metadata directly (tags, owners, descriptions)",
}
KEY_PREFIX, ACCESS_PREFIX, REFRESH_PREFIX = "ca_", "cat_", "car_"
ACCESS_TTL_MS, REFRESH_TTL_MS, CODE_TTL_MS = 3600_000, 30 * 86400_000, 600_000

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

# The agent behind the current request (audit attribution); set where the principal is resolved.
current_agent: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("cairn_agent", default=None)


class AgentError(ValueError):
    def __init__(self, message: str, status: int = 422, oauth_error: str = "invalid_request"):
        super().__init__(message)
        self.status = status
        self.oauth_error = oauth_error


@dataclass(frozen=True)
class AgentGrant:
    """What a valid agent credential resolves to."""
    actor: str
    agent: str
    scopes: tuple[str, ...]


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

    @field_validator("scopes")
    @classmethod
    def _scopes(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        return None if v is None else _check_scopes(v)


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
    def _public(r) -> dict:
        return {"id": r.id, "name": r.name, "kind": r.kind, "owner": r.owner, "scopes": list(r.scopes),
                "redirect_uris": list(r.redirect_uris), "ai_system": r.ai_system, "enabled": r.enabled,
                "created_by": r.created_by, "created_ms": r.created_ms, "last_used_ms": r.last_used_ms,
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
        if values:
            with self.engine.begin() as conn:
                conn.execute(update(agent_clients).where(agent_clients.c.id == agent_id).values(**values))
            if values.get("enabled") is False:
                self._revoke(agent_id)
        self._record("agent.updated", actor, agent_id, values)
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
        for tok, kind, ttl in ((access, "access", ACCESS_TTL_MS), (refresh, "refresh", REFRESH_TTL_MS)):
            conn.execute(agent_tokens.insert().values(
                hash=_hash(tok), kind=kind, client_id=client_id, actor=actor, scopes=list(scopes),
                expires_ms=now + ttl, revoked=False, created_ms=now, details={"resource": resource}))
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL_MS // 1000,
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
        """The grant behind an agent key or access token, or None when it is not valid now."""
        now = _now()
        with self.engine.connect() as conn:
            if token.startswith(KEY_PREFIX):
                client = conn.execute(select(agent_clients).where(agent_clients.c.key_hash == _hash(token))).first()
                if client is None or client.kind != "key":
                    return None
                actor, scopes = client.owner, list(client.scopes)
            elif token.startswith(ACCESS_PREFIX):
                row = conn.execute(select(agent_tokens).where(agent_tokens.c.hash == _hash(token),
                                                              agent_tokens.c.kind == "access")).first()
                if row is None or row.revoked or row.expires_ms < now:
                    return None
                client = conn.execute(select(agent_clients).where(agent_clients.c.id == row.client_id)).first()
                if client is None:
                    return None
                actor, scopes = row.actor, [s for s in row.scopes if s in client.scopes]
            else:
                return None
        if not client.enabled or not actor:
            return None
        if client.ai_system and self.ai_system_state is not None and self.ai_system_state(client.ai_system) != "APPROVED":
            return None
        self._touch(client.id, now)
        return AgentGrant(actor, client.id, tuple(scopes))

    def _touch(self, agent_id: str, now: int) -> None:
        if now - self._touched.get(agent_id, 0) < 60_000:       # at most once a minute per agent
            return
        self._touched[agent_id] = now
        with self.engine.begin() as conn:
            conn.execute(update(agent_clients).where(agent_clients.c.id == agent_id).values(last_used_ms=now))


# ---- what a scope allows over HTTP ---------------------------------------------------------------
QUERY_DATA_PATHS = ("/data/preview", "/data/query", "/data/ask")
# Exactly these create something that waits for a person (deciding on it is a write).
PROPOSE_ROUTES = {"/ai/docs/generate", "/governance/workflows", "/marketplace/access-requests", "/collab/threads",
                  "/collab/threads/{thread_id}/comments", "/ai/steward/run"}
NEVER_PATHS = ("/admin", "/oauth", "/me/agents")              # not even to read
READ_ONLY_PATHS = ("/governance/policies", "/governance/ai-systems", "/governance/personas", "/governance/purposes")


def _under(path: str, prefixes: tuple[str, ...]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def required_scope(method: str, path: str, read_posts: set[str]) -> Optional[str]:
    """The scope an agent needs for ``method path`` (the route template, e.g. ``/x/{id}``); None when
    no agent credential may do it."""
    reading = method in ("GET", "HEAD", "OPTIONS")
    if _under(path, NEVER_PATHS) or (_under(path, READ_ONLY_PATHS) and not reading):
        return None
    if path in QUERY_DATA_PATHS:
        return "query_data"
    if reading or path in read_posts:
        return "read"
    if method == "POST" and path in PROPOSE_ROUTES:
        return "propose"
    return "write"


def tool_scope(tool: str) -> str:
    """The scope an agent needs for an MCP tool."""
    return {"ask_data": "query_data", "generate_documentation": "propose"}.get(tool, "read")


def describe(scopes: list[str]) -> list[dict[str, Any]]:
    return [{"scope": s, "text": SCOPE_TEXT[s]} for s in scopes]
