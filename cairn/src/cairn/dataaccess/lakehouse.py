"""The lakehouse for engines that read files themselves (Spark, pyiceberg, DuckDB), under Cairn's policies.

Trino enforces Cairn's masks and row filters inside each query. Engines that read a table's data files
directly cannot, so Cairn Catalog asks Cairn about every table they touch, as the person or pipeline user
they sign in as, and refuses what they may not do:

* ``POST /v1/lakehouse/token`` gives a signed-in person, or a pipeline user (its personal access token), a
  short-lived token for Cairn Catalog (audience ``lakehouse``, signed with the key Trino's tokens use,
  published at ``/v1/trino/jwks.json``).
* ``POST /v1/lakehouse/changes`` (Cairn Catalog only): the tables a commit changed, whoever wrote them, recorded
  as dataset changes (``cairn.pipelines.changes``).
* ``POST /v1/lakehouse/decide`` (Cairn Catalog only: a Cairn service token) answers, for a user and a list
  of tables, what the user may do with each: ``view``, ``read``, ``write``, ``manage``, and whether
  reading it is ``restricted`` (a column mask or a row filter applies to the user). A restricted table
  is never handed to such an engine: its files hold what the mask or filter would hide, so the catalog
  refuses it and the user queries it through Trino, which applies them. Any error decides nothing
  (everything false): the catalog then refuses.

Long jobs keep their sessions without a long-lived token: a session's tokens last an hour
(``CAIRN_LAKEHOUSE_TOKEN_TTL``) and the engine renews them at ``POST /v1/lakehouse/oauth/tokens``
with the session credential (OAuth client credentials) or the current token (token exchange), which
Iceberg's REST client does by itself before a token expires, on the driver and on every executor.
Each renewal checks again that the session was not ended (``POST /v1/lakehouse/sessions/revoke``)
and the user is not suspended, and a session ends after ``CAIRN_LAKEHOUSE_MAX_SESSION`` (7 days)
whatever happens. Only a hash of the session credential is kept. Renewals are audited.

Tables are the datasets Trino names for the lakehouse catalog (``CAIRN_LAKEHOUSE_TRINO_CATALOG``,
default ``lakehouse``), so one policy governs a table in Trino and in Spark alike.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
import uuid
from typing import Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, select
from starlette.requests import Request  # a module global: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata
from cairn.dataaccess.trino import MANAGE, READ, SYSTEM, VIEW, WRITE, TrinoAccess

logger = logging.getLogger(__name__)

AUDIENCE = "lakehouse"
NOTHING = {"view": False, "read": False, "write": False, "manage": False, "restricted": True}
TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token"
JWT = "urn:ietf:params:oauth:token-type:jwt"

# A lakehouse session: a person or pipeline user signed in to Cairn Catalog from an engine, with a
# session credential (only its hash is kept) the engine renews its token with.
lakehouse_sessions = Table(
    "cairn_lakehouse_sessions", metadata,
    Column("sid", String(64), primary_key=True),
    Column("user", String(256), nullable=False, index=True),
    Column("secret_hash", String(64), nullable=False),
    Column("started_s", BigInteger, nullable=False),
    Column("ended_s", BigInteger, nullable=True),
    Column("ended_by", String(256), nullable=True),
)


class SessionRefused(Exception):
    """A lakehouse session cannot be renewed; ``error`` is the OAuth error code."""

    def __init__(self, message: str, error: str = "invalid_grant"):
        super().__init__(message)
        self.error = error


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        v = int(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a number of seconds")
    if not low <= v <= high:
        raise ValueError(f"{name} must be between {low} and {high} seconds")
    return v


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class LakehouseSessions:
    """Lakehouse sessions: a token for an hour, renewed with the session credential (OAuth client
    credentials) or the current token (token exchange) while the user may keep the session."""

    def __init__(self, tokens, engine, audit=None, user_status: Optional[Callable[[str], str]] = None,
                 ttl: Optional[int] = None, max_session: Optional[int] = None):
        self.tokens, self.engine, self.audit = tokens, engine, audit
        self.user_status = user_status  # urn -> "ACTIVE" | "SUSPENDED" | ...
        self.ttl = ttl if ttl is not None else _int_env("CAIRN_LAKEHOUSE_TOKEN_TTL", 3600, 300, 43200)
        self.max_session = (max_session if max_session is not None
                            else _int_env("CAIRN_LAKEHOUSE_MAX_SESSION", 7 * 86400, 3600, 30 * 86400))

    def issue(self, actor: str, ttl: Optional[int] = None) -> dict:
        """A new session for ``actor``: its first token and the credential that renews it."""
        if not actor.startswith("urn:li:corpuser:"):
            raise ValueError("lakehouse sessions are for Cairn users")
        if actor.removeprefix("urn:li:corpuser:").startswith("share:"):
            raise ValueError("share: names external recipients, who sign in with their share's credential")
        now = int(time.time())
        sid, secret = uuid.uuid4().hex, secrets.token_urlsafe(32)
        ttl = min(int(ttl or self.ttl), self.max_session)
        out = self.tokens.mint(actor, ttl, audience=AUDIENCE, extra={"sid": sid, "sst": now})
        with self.engine.begin() as conn:
            conn.execute(lakehouse_sessions.insert().values(
                sid=sid, user=actor.removeprefix("urn:li:corpuser:"), secret_hash=_hash(secret), started_s=now))
            if self.audit is not None:
                self.audit.record("lakehouse", "lakehouse.token_issued", actor, None,
                                  {"session": sid, "token_id": out["token_id"], "expires_at": out["expires_at"]}, conn=conn)
        return {**out, "session": sid, "credential": f"{sid}:{secret}", "session_expires_at": now + self.max_session}

    def _session(self, sid: str) -> Optional[dict]:
        with self.engine.connect() as conn:
            row = conn.execute(select(lakehouse_sessions).where(lakehouse_sessions.c.sid == sid)).first()
        return dict(row._mapping) if row else None

    def _renew(self, row: Optional[dict], how: str) -> dict:
        if row is None:
            raise SessionRefused("no such lakehouse session")
        if row["ended_s"] is not None:
            raise SessionRefused("the lakehouse session was ended (revoked)")
        now = int(time.time())
        if now >= row["started_s"] + self.max_session:
            raise SessionRefused("the session reached its maximum length; sign in again")
        urn = f"urn:li:corpuser:{row['user']}"
        try:
            status = self.user_status(urn) if self.user_status else "ACTIVE"
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("lakehouse: cannot check %s for a renewal: %s", urn, e)
            raise SessionRefused("Cairn cannot check the user right now", "temporarily_unavailable")
        if (status or "ACTIVE").upper() == "SUSPENDED":
            raise SessionRefused("the user is suspended")
        ttl = max(60, min(self.ttl, row["started_s"] + self.max_session - now))
        out = self.tokens.mint(urn, ttl, audience=AUDIENCE, extra={"sid": row["sid"], "sst": row["started_s"]})
        if self.audit is not None:
            self.audit.record("lakehouse", "lakehouse.token_refreshed", urn, None,
                              {"session": row["sid"], "with": how, "token_id": out["token_id"],
                               "expires_at": out["expires_at"]})
        return out

    def refresh(self, subject_token: str) -> dict:
        """Token exchange: a new token for the session ``subject_token`` belongs to."""
        try:
            claims = self.tokens.verify(subject_token, AUDIENCE, leeway=0)
        except ValueError as e:
            raise SessionRefused(str(e))
        if not claims.get("sid"):
            raise SessionRefused("this token cannot be renewed (no session); get a new one")
        row = self._session(claims["sid"])
        if row is not None and row["user"] != claims["sub"]:
            raise SessionRefused("the token does not belong to this session")
        return self._renew(row, "token")

    def with_credential(self, client_id: str, client_secret: str) -> dict:
        """Client credentials: a new token for the session the credential belongs to."""
        row = self._session(client_id) if client_id else None
        if row is None or not hmac.compare_digest(row["secret_hash"], _hash(client_secret or "")):
            raise SessionRefused("unknown session credential", "invalid_client")
        return self._renew(row, "credential")

    def lineage_user(self, credential: str) -> str:
        """The user a session credential (``<session>:<secret>``) reports lineage as: valid exactly as
        long as the session may be renewed (not ended, not past its maximum, the user not suspended),
        so a job's lineage outlives any one token and stops when its session is ended."""
        sid, _, secret = credential.partition(":")
        row = self._session(sid) if sid and secret else None
        if row is None or not hmac.compare_digest(row["secret_hash"], _hash(secret)):
            raise SessionRefused("unknown session credential", "invalid_client")
        if row["ended_s"] is not None:
            raise SessionRefused("the lakehouse session was ended (revoked)")
        if int(time.time()) >= row["started_s"] + self.max_session:
            raise SessionRefused("the session reached its maximum length")
        urn = f"urn:li:corpuser:{row['user']}"
        try:
            status = self.user_status(urn) if self.user_status else "ACTIVE"
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("lakehouse: cannot check %s for lineage: %s", urn, e)
            raise SessionRefused("Cairn cannot check the user right now", "temporarily_unavailable")
        if (status or "ACTIVE").upper() == "SUSPENDED":
            raise SessionRefused("the user is suspended")
        return urn

    def revoke(self, user: str, actor: str) -> dict:
        """Ends every open lakehouse session of ``user``: none is renewed again (tokens already issued
        last at most CAIRN_LAKEHOUSE_TOKEN_TTL)."""
        user = user.removeprefix("urn:li:corpuser:")
        if not user:
            raise ValueError("name a user")
        now = int(time.time())
        with self.engine.begin() as conn:
            ended = conn.execute(lakehouse_sessions.update()
                                 .where(lakehouse_sessions.c.user == user, lakehouse_sessions.c.ended_s.is_(None))
                                 .values(ended_s=now, ended_by=actor)).rowcount
            if self.audit is not None:
                self.audit.record("lakehouse", "lakehouse.sessions_revoked", actor, f"urn:li:corpuser:{user}",
                                  {"sessions": ended}, conn=conn)
        return {"user": user, "sessions_ended": ended, "tokens_last_at_most": self.ttl}


class LakehouseAccess:
    def __init__(self, access: TrinoAccess, groups_of: Callable[[str], list[str]], trino_catalog: str = "lakehouse",
                 catalog_uris: tuple[str, ...] = ()):
        self.access, self.groups_of, self.trino_catalog = access, groups_of, trino_catalog
        # Cairn Catalog's addresses, as engines name it in OpenLineage (``CAIRN_LAKEHOUSE_CATALOG_URIS``).
        self.catalog_uris = tuple(u.rstrip("/") for u in catalog_uris if u.strip())
        self.shares = None   # external recipients (cairn.dataaccess.shares), decided by their share alone

    def remap_lineage(self, event: dict) -> dict:
        """Engines that read files (Spark) name lakehouse tables by their storage paths in OpenLineage, and
        add a ``symlinks`` facet naming the table in the catalog. Tables of Cairn Catalog are renamed to
        the datasets Trino and the catalog crawler name, so lineage from every engine meets on one dataset."""
        if not isinstance(event, dict) or not self.catalog_uris:
            return event
        alias: dict[tuple[str, str], tuple[str, str]] = {}
        for side in ("inputs", "outputs"):
            for d in event.get(side) or []:
                if not isinstance(d, dict):
                    continue
                for ident in ((d.get("facets") or {}).get("symlinks") or {}).get("identifiers") or []:
                    ns = str(ident.get("namespace", "")).rstrip("/")
                    parts = str(ident.get("name", "")).split(".")
                    if ident.get("type") == "TABLE" and ns in self.catalog_uris and len(parts) >= 2:
                        platform, name, _ = self.access.dataset_name(self.trino_catalog, ".".join(parts[:-1]), parts[-1])
                        alias[(d.get("namespace"), d.get("name"))] = (f"{platform}://", name)
                        break
        if not alias:
            return event

        def one(d: dict) -> dict:
            hit = alias.get((d.get("namespace"), d.get("name")))
            return {**d, "namespace": hit[0], "name": hit[1]} if hit else d

        ev = dict(event)
        for side in ("inputs", "outputs"):
            out = []
            for d in event.get(side) or []:
                if not isinstance(d, dict):
                    out.append(d)
                    continue
                nd = one(d)
                cl = (d.get("facets") or {}).get("columnLineage")
                if isinstance(cl, dict) and isinstance(cl.get("fields"), dict):
                    fields = {k: {**v, "inputFields": [one(i) if isinstance(i, dict) else i for i in v.get("inputFields") or []]}
                              if isinstance(v, dict) else v for k, v in cl["fields"].items()}
                    nd = {**nd, "facets": {**(d.get("facets") or {}), "columnLineage": {**cl, "fields": fields}}}
                out.append(nd)
            ev[side] = out
        return ev

    def urn(self, namespace: list[str], name: str) -> str:
        return self.access.urn(self.trino_catalog, ".".join(namespace), name)

    def decide(self, user: str, tables: list[dict]) -> dict:
        from cairn.dataaccess.shares import PREFIX

        if user.removeprefix("urn:li:corpuser:").startswith(PREFIX):
            if self.shares is None:
                return {"schemas": False, "namespaces": [], "tables": [dict(NOTHING) for _ in tables]}
            return self.shares.decide(user.removeprefix("urn:li:corpuser:"), tables)
        actor = user if user.startswith("urn:li:") else f"urn:li:corpuser:{user}"
        try:
            groups = list(self.groups_of(actor))
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("lakehouse: groups of %s unavailable: %s", actor, e)
            return {"schemas": False, "tables": [dict(NOTHING) for _ in tables]}
        allowed = lambda priv, urn: self.access.allowed(actor, groups, priv, urn)  # noqa: E731
        try:
            schemas = allowed(SYSTEM, None) or allowed(MANAGE, None)
        except Exception:  # noqa: BLE001
            schemas = False
        out = []
        from cairn.governance.pdp import deny_audit_batch

        with deny_audit_batch():                  # every refusal still audited, in one commit for the whole job
            self._decide_tables(actor, groups, tables, allowed, out)
        return {"schemas": bool(schemas), "tables": out}

    def _decide_tables(self, actor: str, groups: list[str], tables: list[dict], allowed, out: list) -> None:
        for t in tables:
            try:
                namespace = list(t.get("namespace") or [])
                urn = self.urn(namespace, str(t.get("name") or ""))
                if not self.access._resident({"catalogName": self.trino_catalog, "schemaName": ".".join(namespace)}):
                    # Pinned to a region this deployment is not allowed to process it in: seen, never read.
                    out.append({**NOTHING, "view": allowed(VIEW, urn)})
                    continue
                r = {"view": allowed(VIEW, urn), "read": allowed(READ, urn), "write": allowed(WRITE, urn),
                     "manage": allowed(MANAGE, urn), "restricted": False}
                if r["read"]:
                    r["restricted"] = self._restricted(actor, groups, urn)
                out.append(r)
            except Exception as e:  # noqa: BLE001 - fail closed
                logger.warning("lakehouse decision failed for %s on %s: %s", actor, t, e)
                out.append(dict(NOTHING))

    def _restricted(self, actor: str, groups: list[str], urn: str) -> bool:
        d = self.access.decide(actor, groups, READ, urn)
        if any(o.type == "row_filter" for o in d.obligations):
            return True
        return bool(self.access.plan_for(actor, urn).active)


def build_lakehouse_router(lake: LakehouseAccess, sessions: LakehouseSessions, current_principal, audit=None,
                           authorize=None, changes=None):
    import base64
    from urllib.parse import unquote

    from fastapi import APIRouter, Body, Depends, Form, HTTPException
    from fastapi.responses import JSONResponse

    from cairn.dataaccess.trino import CRAWLER

    router = APIRouter(prefix="/v1/lakehouse", tags=["lakehouse"])

    @router.post("/token")
    def token(ttl_seconds: Optional[int] = Body(None, embed=True), p=Depends(current_principal)):
        """A token for Cairn Catalog (Spark, pyiceberg) as the caller: a person, or a pipeline user. It is
        renewed by token exchange at /v1/lakehouse/oauth/tokens for as long as the session may last."""
        if p.is_service or p.via in ("agent", "extension") or p.urn == f"urn:li:corpuser:{CRAWLER}":
            raise HTTPException(403, "lakehouse tokens are for people and pipeline users")
        try:
            return sessions.issue(p.urn, ttl_seconds)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.post("/oauth/tokens")
    def oauth_tokens(request: Request, grant_type: str = Form(""), subject_token: str = Form(""),
                     subject_token_type: str = Form(""), client_id: str = Form(""), client_secret: str = Form(""),
                     scope: str = Form(""), requested_token_type: str = Form("")):
        """How Iceberg's REST client gets and renews its token: OAuth 2.0 client credentials (the session
        credential) or token exchange (RFC 8693, the current token). The session is checked each time."""
        def oauth_error(code: str, text: str, status: int = 400):
            headers = {"Cache-Control": "no-store"}
            if code == "invalid_client":
                status, headers["WWW-Authenticate"] = 401, 'Basic realm="lakehouse"'
            return JSONResponse({"error": code, "error_description": text}, status_code=status, headers=headers)

        if requested_token_type not in ("", ACCESS_TOKEN):
            return oauth_error("invalid_request", "only access tokens are issued")
        try:
            if grant_type == "client_credentials":
                basic = request.headers.get("authorization", "")
                if not client_id and basic[:6].lower() == "basic ":
                    try:
                        client_id, _, client_secret = base64.b64decode(basic[6:]).decode().partition(":")
                    except ValueError:
                        return oauth_error("invalid_client", "malformed client authentication")
                out = sessions.with_credential(unquote(client_id), unquote(client_secret))
            elif grant_type == TOKEN_EXCHANGE:
                if subject_token_type not in (ACCESS_TOKEN, JWT) or not subject_token:
                    return oauth_error("invalid_request", "subject_token must be a lakehouse access token")
                out = sessions.refresh(subject_token)
            else:
                return oauth_error("unsupported_grant_type", "lakehouse tokens come from client credentials or token exchange")
        except SessionRefused as e:
            return oauth_error(e.error, str(e), 503 if e.error == "temporarily_unavailable" else 400)
        return JSONResponse({"access_token": out["token"], "issued_token_type": ACCESS_TOKEN,
                             "token_type": "Bearer", "expires_in": out["expires_in"]},
                            headers={"Cache-Control": "no-store"})

    @router.post("/sessions/revoke")
    def revoke(user: str = Body(..., embed=True), p=Depends(current_principal)):
        """Ends a user's lakehouse sessions: none is renewed again."""
        if authorize is None:
            raise HTTPException(403, "revoking sessions needs MANAGE_SYSTEM_OPERATIONS")
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        try:
            return sessions.revoke(user, p.urn)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.post("/decide")
    def decide(user: str = Body(...), tables: list[dict] = Body(default_factory=list), p=Depends(current_principal)):
        """Cairn Catalog asks what ``user`` may do with each table."""
        if not p.is_service:
            raise HTTPException(403, "only Cairn Catalog asks for lakehouse decisions")
        if len(tables) > 1000:
            raise HTTPException(413, "at most 1000 tables per decision")
        return lake.decide(user, tables)

    @router.post("/changes")
    def changed(tables: list[dict] = Body(..., embed=True), p=Depends(current_principal)):
        """Cairn Catalog: these tables were committed to (by anyone: Trino, Spark, pyiceberg). Recorded as
        dataset changes, so derived tables and materialized views on them refresh, and orchestrators see it."""
        if not p.is_service:
            raise HTTPException(403, "only Cairn Catalog reports table changes")
        if len(tables) > 1000:
            raise HTTPException(413, "at most 1000 tables per report")
        urns = []
        for t in tables:
            ns, name = t.get("namespace"), t.get("name")
            if not isinstance(ns, list) or not ns or not all(isinstance(x, str) for x in ns) or not isinstance(name, str):
                raise HTTPException(422, "tables: [{namespace: [..], name}]")
            urns.append(lake.urn(ns, name))
        if changes is not None and urns:
            changes.record(sorted(set(urns)), "catalog", "cairn-catalog")
        return {"recorded": len(set(urns))}

    return router
