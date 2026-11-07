"""Sharing lakehouse tables with recipients outside the organisation, through Iceberg REST.

A share names a recipient (another organisation, a partner), the tables it may read, and when it ends.
The recipient gets one credential and reads the tables with any Iceberg REST client (pyiceberg, Spark,
Trino, Snowflake, DuckDB) from Cairn Catalog:

* the client exchanges the credential for a token at ``POST /v1/shares/oauth/tokens`` (OAuth 2.0 client
  credentials, as Iceberg's REST client does by itself). Tokens last ``CAIRN_SHARES_TOKEN_TTL`` (15
  minutes) and are renewed with the same credential while the share lasts;
* Cairn Catalog asks Cairn about every request the recipient makes (no caching), so the recipient sees
  only the shared tables and their namespaces, read-only, and nothing else in the catalog;
* revoking a share, or its expiry, stops it at once: the next request is refused, and so is the next
  renewal. Storage keys already handed out are the catalog's scoped keys for one table, read-only, and
  last at most the catalog's credential lifetime (``CAIRN_CATALOG_VEND_SECONDS``);
* a table to which a column mask or row filter applies is never shared: its files hold what the mask or
  filter hides. That is checked when the share is made and again on every request, so a mask added
  later stops the table from being read through the share;
* a table pinned to a region (data residency, ``cairn.governance.residency``) is shared only with a recipient
  whose ``region`` its pin allows, also checked on every request: a recipient with no region gets no pinned
  table.

Only people who may manage every table they share (``MANAGE`` on it) make shares; operators
(``MANAGE_SYSTEM_OPERATIONS``) see and revoke all of them. Only a hash of the credential is kept. Every
change is audited in the same transaction, and so is every token the recipient gets.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from typing import Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, select
from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

PREFIX = "share:"                       # the user a recipient is to Cairn Catalog: share:<id>
AUDIENCE = "lakehouse"
NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
TABLE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_$-]*(\.[A-Za-z0-9_][A-Za-z0-9_$-]*)+$")

lakehouse_shares = Table(
    "cairn_lakehouse_shares", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False, unique=True),
    Column("recipient", String(256), nullable=False),
    Column("tables", Text, nullable=False),               # JSON list of "namespace.table"
    Column("secret_hash", String(64), nullable=False),
    Column("created_by", String(256), nullable=False),
    Column("created_s", BigInteger, nullable=False),
    Column("expires_s", BigInteger, nullable=False),
    Column("revoked_s", BigInteger, nullable=True),
    Column("revoked_by", String(256), nullable=True),
    Column("last_used_s", BigInteger, nullable=True),
    Column("region", String(32), nullable=True),           # where the recipient keeps what it reads (residency)
)


class ShareError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _split(table: str) -> tuple[list[str], str]:
    parts = table.split(".")
    return parts[:-1], parts[-1]


class Shares:
    def __init__(self, engine, tokens, lake, audit=None, token_ttl: Optional[int] = None,
                 max_days: Optional[int] = None, catalog_url: str = "", token_url: str = ""):
        self.engine, self.tokens, self.lake, self.audit = engine, tokens, lake, audit
        self.token_ttl = token_ttl or int(os.environ.get("CAIRN_SHARES_TOKEN_TTL", "900"))
        if not 60 <= self.token_ttl <= 3600:
            raise ValueError("CAIRN_SHARES_TOKEN_TTL must be between 60 and 3600 seconds")
        self.max_days = max_days or int(os.environ.get("CAIRN_SHARES_MAX_DAYS", "365"))
        # What the recipient connects to: the catalog and the token endpoint as they reach them.
        self.catalog_url = catalog_url or os.environ.get("CAIRN_SHARES_CATALOG_URL", "")
        self.token_url = token_url or os.environ.get("CAIRN_SHARES_TOKEN_URL", "")
        self.residency = None                  # cairn.governance.residency.Residency, when rules are in force

    # ---- the share's own records -----------------------------------------------------------------
    def _row(self, conn, sid: str) -> Optional[dict]:
        row = conn.execute(select(lakehouse_shares).where(lakehouse_shares.c.id == sid)).first()
        return dict(row._mapping) if row else None

    @staticmethod
    def _public(row: dict) -> dict:
        now = int(time.time())
        state = "revoked" if row["revoked_s"] else ("expired" if now >= row["expires_s"] else "active")
        return {"id": row["id"], "name": row["name"], "recipient": row["recipient"], "tables": json.loads(row["tables"]),
                "created_by": row["created_by"], "created_at": row["created_s"], "expires_at": row["expires_s"],
                "revoked_at": row["revoked_s"], "revoked_by": row["revoked_by"], "last_used_at": row["last_used_s"],
                "region": row.get("region"), "state": state}

    def _may_leave(self, table: str, region: Optional[str]) -> bool:
        return self.residency is None or self.residency.transfer_allowed(".".join(_split(table)[0]), region)

    def _restricted(self, sid: str, urn: str) -> bool:
        return self.lake._restricted(f"urn:li:corpuser:{PREFIX}{sid}", [], urn)

    def create(self, actor: str, groups: list[str], name: str, recipient: str, tables: list[str],
               expires_days: int, region: Optional[str] = None) -> dict:
        if not NAME.match(name or ""):
            raise ShareError("name: lowercase letters, digits, '.', '_' and '-'")
        recipient = (recipient or "").strip()
        if not recipient or len(recipient) > 256:
            raise ShareError("recipient: who the share is for (an organisation or a contact), at most 256 characters")
        tables = sorted({t.strip() for t in tables or [] if t and t.strip()})
        if not tables or len(tables) > 500:
            raise ShareError("tables: between 1 and 500 tables, each namespace.table")
        bad = [t for t in tables if not TABLE.match(t)]
        if bad:
            raise ShareError(f"tables are named namespace.table: {', '.join(bad)}")
        if not 1 <= int(expires_days or 0) <= self.max_days:
            raise ShareError(f"expires_days: between 1 and {self.max_days}")
        from cairn.governance.residency import ResidencyError, valid_region

        try:
            region = valid_region(region)
        except ResidencyError as e:
            raise ShareError(str(e))
        pinned = [t for t in tables if not self._may_leave(t, region)]
        if pinned:
            where = f"a recipient in {region}" if region else "a recipient with no region"
            raise ShareError(f"{', '.join(pinned)} may not be shared with {where}: data residency", 409)
        sid = uuid.uuid4().hex
        from cairn.dataaccess.trino import MANAGE

        for t in tables:
            urn = self.lake.urn(*_split(t))
            if not self.lake.access.allowed(actor, groups, MANAGE, urn):
                raise ShareError(f"sharing {t} needs MANAGE on it", 403)
            if self._restricted(sid, urn):
                raise ShareError(f"{t} has a column mask or row filter: its files hold what they hide, so it is not shared",
                                 409)
        secret = secrets.token_urlsafe(32)
        now = int(time.time())
        row = {"id": sid, "name": name, "recipient": recipient, "tables": json.dumps(tables),
               "secret_hash": _hash(secret), "created_by": actor, "created_s": now,
               "expires_s": now + int(expires_days) * 86400, "revoked_s": None, "revoked_by": None, "last_used_s": None,
               "region": region}
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(lakehouse_shares.insert().values(**row))
                if self.audit is not None:
                    self.audit.record("shares", "share.created", actor, None,
                                      {"share": sid, "name": name, "recipient": recipient, "tables": tables,
                                       "region": region, "expires_at": row["expires_s"]}, conn=conn)
        except IntegrityError:
            raise ShareError(f"a share named {name} exists", 409)
        out = self._public(row)
        out["credential"] = f"{sid}:{secret}"
        out["connect"] = {"type": "rest", "uri": self.catalog_url, "oauth2-server-uri": self.token_url,
                          "credential": out["credential"], "scope": "catalog"}
        return out

    def list(self, actor: str, everyone: bool) -> list[dict]:
        q = select(lakehouse_shares).order_by(lakehouse_shares.c.created_s.desc())
        if not everyone:
            q = q.where(lakehouse_shares.c.created_by == actor)
        with self.engine.connect() as conn:
            return [self._public(dict(r._mapping)) for r in conn.execute(q)]

    def get(self, sid: str, actor: str, everyone: bool) -> dict:
        with self.engine.connect() as conn:
            row = self._row(conn, sid)
        if row is None or not (everyone or row["created_by"] == actor):
            raise ShareError("no such share", 404)
        return self._public(row)

    def revoke(self, sid: str, actor: str, everyone: bool) -> dict:
        now = int(time.time())
        with self.engine.begin() as conn:
            row = self._row(conn, sid)
            if row is None or not (everyone or row["created_by"] == actor):
                raise ShareError("no such share", 404)
            if row["revoked_s"] is None:
                conn.execute(lakehouse_shares.update().where(lakehouse_shares.c.id == sid)
                             .values(revoked_s=now, revoked_by=actor))
                row.update(revoked_s=now, revoked_by=actor)
                if self.audit is not None:
                    self.audit.record("shares", "share.revoked", actor, None, {"share": sid, "name": row["name"]}, conn=conn)
        return self._public(row)

    # ---- the recipient -------------------------------------------------------------------------
    def _active(self, row: Optional[dict]) -> bool:
        return row is not None and row["revoked_s"] is None and int(time.time()) < row["expires_s"]

    def token(self, client_id: str, client_secret: str) -> dict:
        """A token for the recipient, for at most the rest of the share's life."""
        with self.engine.begin() as conn:
            row = self._row(conn, client_id) if client_id else None
            if row is None or not hmac.compare_digest(row["secret_hash"], _hash(client_secret or "")):
                raise ShareError("unknown share credential", 401)
            if row["revoked_s"] is not None:
                raise ShareError("the share was revoked", 400)
            now = int(time.time())
            if now >= row["expires_s"]:
                raise ShareError("the share has expired", 400)
            ttl = max(60, min(self.token_ttl, row["expires_s"] - now))
            out = self.tokens.mint(f"urn:li:corpuser:{PREFIX}{row['id']}", ttl, audience=AUDIENCE,
                                   extra={"share": row["id"]})
            conn.execute(lakehouse_shares.update().where(lakehouse_shares.c.id == row["id"]).values(last_used_s=now))
            if self.audit is not None:
                self.audit.record("shares", "share.token_issued", f"urn:li:corpuser:{PREFIX}{row['id']}", None,
                                  {"share": row["id"], "name": row["name"], "token_id": out["token_id"],
                                   "expires_at": out["expires_at"]}, conn=conn)
        return out

    def decide(self, user: str, tables: list[dict]) -> dict:
        """Cairn Catalog's question for a recipient: the namespaces it may see (none: refused outright)
        and, per table, read-only access to the shared ones. Any error decides nothing."""
        from cairn.dataaccess.lakehouse import NOTHING

        nothing = {"schemas": False, "namespaces": [], "tables": [dict(NOTHING) for _ in tables]}
        sid = user.removeprefix(PREFIX)
        try:
            with self.engine.connect() as conn:
                row = self._row(conn, sid)
            if not self._active(row):
                return nothing
            shared = set(json.loads(row["tables"]))
            out = []
            for t in tables:
                ns, name = [str(x) for x in t.get("namespace") or []], str(t.get("name") or "")
                full = ".".join(ns + [name])
                if full in shared and self._may_leave(full, row.get("region")) \
                        and not self._restricted(sid, self.lake.urn(ns, name)):
                    out.append({"view": True, "read": True, "write": False, "manage": False, "restricted": False})
                else:
                    out.append(dict(NOTHING))
            namespaces = sorted({".".join(_split(t)[0]) for t in shared})
            return {"schemas": False, "namespaces": namespaces, "tables": out}
        except Exception as e:  # noqa: BLE001 - fail closed
            logger.warning("share decision failed for %s: %s", user, e)
            return nothing


def build_shares_router(shares: Shares, current_principal, authorize, groups_of):
    import base64
    from urllib.parse import unquote

    from fastapi import APIRouter, Body, Depends, Form, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/v1/shares", tags=["shares"])

    def operator(p) -> bool:
        return bool(authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "shares are made by people")
        return p

    def call(fn, *a):
        try:
            return fn(*a)
        except ShareError as e:
            raise HTTPException(e.status, str(e))

    @router.post("")
    def create(name: str = Body(...), recipient: str = Body(...), tables: list[str] = Body(...),
               expires_days: int = Body(30), region: Optional[str] = Body(None), p=Depends(current_principal)):
        """Shares tables with a recipient outside the organisation. The credential is shown once. ``region``:
        where the recipient keeps what it reads (needed for tables pinned to a region)."""
        person(p)
        return call(shares.create, p.urn, list(groups_of(p.urn)), name, recipient, tables, expires_days, region)

    @router.get("")
    def list_(p=Depends(current_principal)):
        return {"shares": shares.list(p.urn, operator(p))}

    @router.get("/{sid}")
    def get(sid: str, p=Depends(current_principal)):
        return call(shares.get, sid, p.urn, operator(p))

    @router.post("/{sid}/revoke")
    def revoke(sid: str, p=Depends(current_principal)):
        """Ends the share at once: the recipient's next request and next renewal are refused."""
        return call(shares.revoke, sid, p.urn, operator(p))

    @router.post("/oauth/tokens")
    def oauth_tokens(request: Request, grant_type: str = Form(""), client_id: str = Form(""),
                     client_secret: str = Form(""), scope: str = Form("")):
        """The recipient's Iceberg REST client gets its token here (OAuth 2.0 client credentials)."""
        def oauth_error(code: str, text: str, status: int = 400):
            headers = {"Cache-Control": "no-store"}
            if code == "invalid_client":
                status, headers["WWW-Authenticate"] = 401, 'Basic realm="shares"'
            return JSONResponse({"error": code, "error_description": text}, status_code=status, headers=headers)

        if grant_type != "client_credentials":
            return oauth_error("unsupported_grant_type", "share tokens come from client credentials")
        basic = request.headers.get("authorization", "")
        if not client_id and basic[:6].lower() == "basic ":
            try:
                client_id, _, client_secret = base64.b64decode(basic[6:]).decode().partition(":")
            except ValueError:
                return oauth_error("invalid_client", "malformed client authentication")
        try:
            out = shares.token(unquote(client_id), unquote(client_secret))
        except ShareError as e:
            return oauth_error("invalid_client" if e.status == 401 else "invalid_grant", str(e))
        return JSONResponse({"access_token": out["token"], "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                             "token_type": "Bearer", "expires_in": out["expires_in"]},
                            headers={"Cache-Control": "no-store"})

    return router
