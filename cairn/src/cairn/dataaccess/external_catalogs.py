"""External Iceberg catalogs, governed: another organisation's or a cloud provider's Iceberg REST catalog,
queried through Trino as a catalog of its own and governed like the lakehouse.

An operator registers the catalog (``POST /v1/external-catalogs``, ``MANAGE_SYSTEM_OPERATIONS``): its URI,
warehouse, how Cairn signs in to it and a **reference** to the credential, never the credential itself:

* ``scope:<scope>/<key>``: a value in a secret scope (``cairn.identity.secret_scopes``), read as the operator
  who registered the catalog (who must be able to read it), so revoking their access stops the catalog;
* ``env:CAIRN_EXTCAT_<NAME>``: an environment variable of Cairn's (only that prefix: Cairn's own secrets can
  never be pointed at).

Then Cairn:

* **adds it to Trino** with Trino's dynamic catalog management (``CREATE CATALOG <name> USING iceberg``, run
  as Cairn's catalog administrator ``cairn-catalogs``, the only identity Cairn lets create or drop catalogs).
  Trino never holds the external credential: it signs in to the external catalog with OAuth2 against
  **Cairn's token broker** (``POST /v1/external-catalogs/oauth/tokens``), with the catalog's name as client id
  and its own ``${ENV:CAIRN_TRINO_ACCESS_KEY}`` as client secret. Cairn checks the key, resolves the
  reference, exchanges it with the external identity provider (``auth: oauth2``, client credentials) or hands
  over the external bearer token (``auth: token``) and answers with the access token. Data files are read with
  the credentials the external catalog vends with each table.
* **catalogs its tables** (``POST /v1/external-catalogs/<name>/sync`` and on a schedule): namespaces, tables
  and schemas, read from the REST API (metadata only, never rows) as ``iceberg`` datasets
  ``<name>.<namespace>.<table>``, the names Trino's access control uses. Labels added in Cairn survive syncs.
* **governs it**: Trino asks Cairn about every table in it like any other (masks, row filters, grants), and
  the catalog is **read-only** unless registered ``writable``: every write, table or schema change is refused.
* **keeps Trino in step**: a catalog missing from Trino (a restarted coordinator) is added again.

Nothing here logs or returns a credential: errors from the external side carry the status code only.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Optional
from urllib.parse import quote as urlquote

from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, delete, select, update
from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

external_catalogs = Table(
    "cairn_external_catalogs", metadata,
    Column("name", String(64), primary_key=True),
    Column("uri", String(1024), nullable=False),
    Column("warehouse", String(512), nullable=True),
    Column("auth", String(16), nullable=False),                # oauth2, token or none
    Column("client_id", String(256), nullable=True),
    Column("credential_ref", String(512), nullable=True),      # a reference, never the value
    Column("token_url", String(1024), nullable=True),
    Column("oauth_scope", String(256), nullable=True),
    Column("store_endpoint", String(1024), nullable=True),
    Column("store_region", String(64), nullable=True),
    Column("writable", Boolean, nullable=False, default=False),
    Column("description", Text, nullable=True),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
    Column("trino_state", String(16), nullable=False),         # attached, failed, pending
    Column("trino_error", Text, nullable=True),
    Column("last_sync_ms", BigInteger, nullable=True),
    Column("last_sync", Text, nullable=True),                  # JSON: tables, columns, removed, error
)

NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
ENV_REF = re.compile(r"^env:(CAIRN_EXTCAT_[A-Z0-9_]{1,64})$")
SCOPE_REF = re.compile(r"^scope:([A-Za-z0-9][A-Za-z0-9._-]{0,127})/([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")
AUTHS = ("oauth2", "token", "none")
RESERVED = {"system", "lakehouse", "information_schema", "lake", "delta", "routines", "llm"}
# Cairn's catalog administrator in Trino: creates and drops external catalogs, and nothing else.
from cairn.dataaccess.trino import CATALOG_ADMIN  # noqa: E402 - the identity Trino lets create catalogs
MAX_TABLES = 20_000
SYNC_PREFIX = "ext:"


class ExternalCatalogError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _lit(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def iceberg_field_type(t: Any) -> tuple[str, str]:
    """An Iceberg schema type -> (the catalog's field type, its native type)."""
    if isinstance(t, dict):
        kind = str(t.get("type") or "")
        return {"struct": "record", "list": "array", "map": "map"}.get(kind, "string"), kind or "unknown"
    s = str(t).lower()
    if s == "boolean":
        return "boolean", s
    if s in ("int", "long", "float", "double") or s.startswith("decimal"):
        return "number", s
    if s == "date":
        return "date", s
    if s.startswith(("timestamp", "time")):
        return "time", s
    if s == "binary" or s.startswith("fixed"):
        return "bytes", s
    return "string", s


class ExternalCatalogs:
    def __init__(self, engine, graph, access, data_access=None, audit=None,
                 read_secret: Optional[Callable[[str, str, str], str]] = None,
                 broker_url: str = "", trino_connection: str = "", trino_key: str = "",
                 key_env: str = "CAIRN_TRINO_ACCESS_KEY", timeout: float = 30.0):
        self.engine, self.graph, self.access, self.data_access, self.audit = engine, graph, access, data_access, audit
        self.read_secret = read_secret                 # (actor, scope, key) -> value (secret scopes)
        self.broker_url = broker_url.rstrip("/")       # Cairn's API as Trino reaches it
        self.trino_connection = trino_connection       # a trino:// connection Cairn signs in to
        self.trino_key, self.key_env, self.timeout = trino_key, key_env, timeout
        self._cache: tuple[float, dict[str, dict]] = (0.0, {})
        self._tokens: dict[str, tuple[float, str, int]] = {}   # name -> (expires, token, expires_in)
        self._lock = threading.Lock()
        self.reserved = set(RESERVED)

    # ---- the registry ---------------------------------------------------------------------------
    def _row(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(external_catalogs).where(external_catalogs.c.name == name)).first()
        if r is None:
            raise ExternalCatalogError(f"no external catalog {name!r}", 404)
        return dict(r._mapping)

    @staticmethod
    def _public(r: dict) -> dict:
        out = {k: v for k, v in r.items() if k not in ("last_sync",)}
        out["last_sync"] = json.loads(r["last_sync"]) if r.get("last_sync") else None
        out["platform"] = "iceberg"
        return out

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(external_catalogs).order_by(external_catalogs.c.name)).all()
        return [self._public(dict(r._mapping)) for r in rows]

    def get(self, name: str) -> dict:
        return self._public(self._row(name))

    def mapping(self, catalog: str) -> Optional[dict]:
        """For Trino's access control: the registered catalog's naming and whether it may be written
        (None: not an external catalog). Read from the database at most every few seconds; a failure to
        read raises (Trino's question fails, so it is refused)."""
        at, cache = self._cache
        if time.time() - at > 5:
            with self.engine.connect() as conn:
                rows = conn.execute(select(external_catalogs.c.name, external_catalogs.c.writable)).all()
            cache = {r[0]: {"platform": "iceberg", "database": r[0], "writable": bool(r[1])} for r in rows}
            self._cache = (time.time(), cache)
        return cache.get(catalog)

    def _validate(self, body: dict) -> dict:
        allowed = {"name", "uri", "warehouse", "auth", "client_id", "credential_ref", "token_url", "scope",
                   "store_endpoint", "store_region", "writable", "description"}
        extra = set(body) - allowed
        if extra:
            raise ExternalCatalogError(f"unknown fields: {sorted(extra)} (give a credential_ref, never a credential)", 422)
        name = str(body.get("name") or "")
        if not NAME.match(name):
            raise ExternalCatalogError("name is a lowercase Trino catalog name (a-z, 0-9, _; at most 31)", 422)
        static = set(getattr(self.access, "catalogs", {}) or {}) | set(getattr(self.access, "ai_catalogs", set()) or set()) \
            | set(getattr(self.access, "routine_catalogs", set()) or set())
        if name in self.reserved or name in static:
            raise ExternalCatalogError(f"{name!r} is one of Cairn's own Trino catalogs", 409)
        uri = str(body.get("uri") or "").rstrip("/")
        if not re.match(r"^https?://[^\s'\"]+$", uri):
            raise ExternalCatalogError("uri is the catalog's http(s) address", 422)
        auth = str(body.get("auth") or ("oauth2" if body.get("client_id") else "token"))
        if auth not in AUTHS:
            raise ExternalCatalogError(f"auth is one of {list(AUTHS)}", 422)
        ref = body.get("credential_ref")
        if auth != "none":
            if not ref or not (ENV_REF.match(str(ref)) or SCOPE_REF.match(str(ref))):
                raise ExternalCatalogError("credential_ref is scope:<scope>/<key> or env:CAIRN_EXTCAT_<NAME>", 422)
        if auth == "oauth2" and not body.get("client_id"):
            raise ExternalCatalogError("auth oauth2 needs client_id", 422)
        for k in ("token_url", "store_endpoint"):
            if body.get(k) and not re.match(r"^https?://[^\s'\"]+$", str(body[k])):
                raise ExternalCatalogError(f"{k} is an http(s) address", 422)
        for k in ("warehouse", "scope", "store_region", "client_id"):
            if body.get(k) is not None and (not isinstance(body[k], str) or "'" in body[k] or len(body[k]) > 256):
                raise ExternalCatalogError(f"{k} is text (no quotes, at most 256 characters)", 422)
        return {"name": name, "uri": uri, "warehouse": body.get("warehouse") or None, "auth": auth,
                "client_id": body.get("client_id") or None, "credential_ref": ref if auth != "none" else None,
                "token_url": (body.get("token_url") or None), "oauth_scope": body.get("scope") or None,
                "store_endpoint": body.get("store_endpoint") or None, "store_region": body.get("store_region") or None,
                "writable": bool(body.get("writable", False)), "description": body.get("description") or None}

    def register(self, actor: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ExternalCatalogError("expected an object", 422)
        rec = self._validate(body)
        now = _now()
        with self.engine.begin() as conn:
            if conn.execute(select(external_catalogs.c.name).where(external_catalogs.c.name == rec["name"])).first():
                raise ExternalCatalogError(f"external catalog {rec['name']!r} exists", 409)
            conn.execute(external_catalogs.insert().values(**rec, created_by=actor, created_ms=now, updated_ms=now,
                                                           trino_state="pending"))
        self._cache = (0.0, {})
        try:
            self._token(rec["name"], fresh=True)       # the reference resolves and the catalog accepts it
        except ExternalCatalogError as e:
            with self.engine.begin() as conn:
                conn.execute(delete(external_catalogs).where(external_catalogs.c.name == rec["name"]))
            self._cache = (0.0, {})
            raise ExternalCatalogError(f"the catalog did not accept Cairn's sign-in: {e}", e.status if e.status != 404 else 422)
        self._audit("external_catalog.registered", actor, rec["name"],
                    {k: rec[k] for k in ("uri", "warehouse", "auth", "credential_ref", "writable")})
        out: dict[str, Any] = {}
        if self.trino_connection:
            out["trino"] = self.attach(rec["name"], actor)
        out["sync"] = self.sync(rec["name"], actor)
        return {**self.get(rec["name"]), **out}

    def delete(self, actor: str, name: str) -> dict:
        self._row(name)
        dropped = None
        if self.trino_connection:
            try:
                self._trino(f'DROP CATALOG IF EXISTS "{name}"')
                dropped = True
            except Exception as e:  # noqa: BLE001 - recorded; the registration still goes
                dropped = False
                logger.warning("external catalog %s could not be dropped from Trino: %s", name, e)
        removed = self._forget_tables(name)
        with self.engine.begin() as conn:
            conn.execute(delete(external_catalogs).where(external_catalogs.c.name == name))
        self._cache = (0.0, {})
        self._tokens.pop(name, None)
        self._audit("external_catalog.removed", actor, name, {"trino_dropped": dropped, "datasets_removed": removed})
        return {"name": name, "trino_dropped": dropped, "datasets_removed": removed}

    def set_writable(self, actor: str, name: str, writable: bool) -> dict:
        self._row(name)
        with self.engine.begin() as conn:
            conn.execute(update(external_catalogs).where(external_catalogs.c.name == name)
                         .values(writable=bool(writable), updated_ms=_now()))
        self._cache = (0.0, {})
        self._audit("external_catalog.changed", actor, name, {"writable": bool(writable)})
        return self.get(name)

    # ---- credentials: resolved inside Cairn, never returned but as an access token ------------------
    def _credential(self, r: dict) -> str:
        ref = str(r.get("credential_ref") or "")
        m = ENV_REF.match(ref)
        if m:
            value = os.environ.get(m.group(1), "")
            if not value:
                raise ExternalCatalogError(f"the credential reference {ref} is not set", 503)
            return value
        m = SCOPE_REF.match(ref)
        if m:
            if self.read_secret is None:
                raise ExternalCatalogError("secret scopes are not configured (CAIRN_SECRETS_KEY)", 503)
            try:
                return self.read_secret(r["created_by"], m.group(1), m.group(2))
            except Exception as e:  # noqa: BLE001 - the secret's own error names no value
                raise ExternalCatalogError(f"the credential reference {ref} cannot be read by "
                                           f"{r['created_by']}: {e}", 503)
        raise ExternalCatalogError("no credential reference", 503)

    def _http(self):
        import httpx

        return httpx.Client(timeout=self.timeout, follow_redirects=False)

    def _token(self, name: str, fresh: bool = False) -> tuple[Optional[str], int]:
        """(access token, seconds it lasts) for the catalog: cached until a minute before it ends."""
        with self._lock:
            hit = self._tokens.get(name)
            if hit and not fresh and hit[0] > time.time() + 60:
                return hit[1], max(60, int(hit[0] - time.time()))
        r = self._row(name)
        if r["auth"] == "none":
            return None, 3600
        secret = self._credential(r)
        if r["auth"] == "token":
            token, ttl = secret, 3600
        else:
            url = r["token_url"] or f"{r['uri']}/v1/oauth/tokens"
            form = {"grant_type": "client_credentials", "client_id": r["client_id"], "client_secret": secret,
                    "scope": r["oauth_scope"] or "catalog"}
            try:
                with self._http() as c:
                    resp = c.post(url, data=form, headers={"Accept": "application/json"})
            except Exception as e:  # noqa: BLE001 - the transport's message names no secret
                raise ExternalCatalogError(f"the token endpoint could not be reached ({type(e).__name__})", 502)
            if resp.status_code != 200:
                raise ExternalCatalogError(f"the token endpoint refused the client credentials (HTTP {resp.status_code})", 502)
            try:
                doc = resp.json()
                token, ttl = str(doc["access_token"]), int(doc.get("expires_in") or 3600)
            except Exception:  # noqa: BLE001
                raise ExternalCatalogError("the token endpoint answered without an access token", 502)
        if fresh:                                         # checked against the catalog itself
            try:
                with self._http() as c:
                    q = f"?warehouse={urlquote(r['warehouse'], safe='')}" if r["warehouse"] else ""
                    resp = c.get(f"{r['uri']}/v1/config{q}", headers={"Authorization": f"Bearer {token}"})
            except Exception as e:  # noqa: BLE001
                raise ExternalCatalogError(f"the catalog could not be reached ({type(e).__name__})", 502)
            if resp.status_code != 200:
                raise ExternalCatalogError(f"the catalog refused the token (HTTP {resp.status_code})", 502)
        with self._lock:
            self._tokens[name] = (time.time() + ttl, token, ttl)
        return token, ttl

    def broker(self, client_id: str, client_secret: str, grant_type: str) -> dict:
        """Trino's OAuth2 client credentials exchange for an external catalog: the client secret is Trino's
        access key; the answer is the external catalog's access token."""
        if not self.trino_key or not hmac.compare_digest(client_secret.encode(), self.trino_key.encode()):
            raise ExternalCatalogError("invalid_client", 401)
        if grant_type != "client_credentials":
            raise ExternalCatalogError("unsupported_grant_type", 400)
        if not NAME.match(client_id or ""):
            raise ExternalCatalogError("invalid_client", 401)
        try:
            token, ttl = self._token(client_id)
        except ExternalCatalogError as e:
            if e.status == 404:
                raise ExternalCatalogError("invalid_client", 401)
            logger.warning("external catalog %s: no token for Trino: %s", client_id, e)
            raise
        if token is None:
            raise ExternalCatalogError("this catalog takes no credentials", 400)
        return {"access_token": token, "token_type": "bearer", "expires_in": ttl,
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token"}

    # ---- Trino ------------------------------------------------------------------------------------
    def _trino(self, sql: str) -> list[list[Any]]:
        if self.data_access is None or not self.trino_connection:
            raise ExternalCatalogError("no Trino connection for external catalogs (CAIRN_EXTERNAL_CATALOGS_TRINO_CONNECTION)", 503)
        registry = self.data_access.registry
        definition = (registry.definitions() if registry is not None else {}).get(self.trino_connection)
        if not isinstance(definition, dict) or not str(definition.get("url", "")).startswith("trino://"):
            raise ExternalCatalogError(f"no trino:// connection {self.trino_connection!r}", 503)
        _, rows, _ = self.data_access.run_on_trino(definition, f"urn:li:corpuser:{CATALOG_ADMIN}", sql, 10_000)
        return rows

    def catalog_sql(self, r: dict) -> str:
        """The CREATE CATALOG statement: no credential in it, only Trino's own key by reference."""
        if not self.broker_url:
            raise ExternalCatalogError("CAIRN_EXTERNAL_CATALOGS_BROKER_URL (Cairn's API as Trino reaches it) is not set", 503)
        props = {"iceberg.catalog.type": "rest", "iceberg.rest-catalog.uri": r["uri"],
                 "iceberg.rest-catalog.vended-credentials-enabled": "true", "fs.s3.enabled": "true",
                 "s3.region": r["store_region"] or "us-east-1", "s3.path-style-access": "true"}
        if r["warehouse"]:
            props["iceberg.rest-catalog.warehouse"] = r["warehouse"]
        if r["store_endpoint"]:
            props["s3.endpoint"] = r["store_endpoint"]
        if r["auth"] == "none":
            props["iceberg.rest-catalog.security"] = "NONE"
        else:
            props.update({"iceberg.rest-catalog.security": "OAUTH2",
                          "iceberg.rest-catalog.oauth2.server-uri": f"{self.broker_url}/v1/external-catalogs/oauth/tokens",
                          "iceberg.rest-catalog.oauth2.credential": f"{r['name']}:${{ENV:{self.key_env}}}",
                          "iceberg.rest-catalog.oauth2.scope": "catalog"})
        body = ", ".join(f'"{k}" = {_lit(v)}' for k, v in props.items())
        return f'CREATE CATALOG "{r["name"]}" USING iceberg WITH ({body})'

    def _set_state(self, name: str, state: str, error: Optional[str]) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(external_catalogs).where(external_catalogs.c.name == name)
                         .values(trino_state=state, trino_error=(error or None) and error[:2000], updated_ms=_now()))

    def attach(self, name: str, actor: str) -> dict:
        """Adds (or adds again) the catalog to Trino."""
        r = self._row(name)
        sql = self.catalog_sql(r)
        try:
            present = {str(x[0]) for x in self._trino("SHOW CATALOGS")}
            if name in present:
                self._trino(f'DROP CATALOG IF EXISTS "{name}"')
            self._trino(sql)
        except Exception as e:  # noqa: BLE001 - recorded on the catalog; a half-made one is dropped
            msg = str(e)[:500]
            try:
                self._trino(f'DROP CATALOG IF EXISTS "{name}"')
            except Exception:  # noqa: BLE001
                pass
            self._set_state(name, "failed", msg)
            self._audit("external_catalog.trino_failed", actor, name, {"error": msg})
            return {"state": "failed", "error": msg}
        self._set_state(name, "attached", None)
        self._audit("external_catalog.attached", actor, name, {})
        return {"state": "attached"}

    def reconcile(self, actor: str = f"urn:li:corpuser:{CATALOG_ADMIN}") -> dict:
        """Adds registered catalogs Trino does not have (after a coordinator restart)."""
        names = [r["name"] for r in self.list()]
        if not names or not self.trino_connection:
            return {"added": []}
        present = {str(x[0]) for x in self._trino("SHOW CATALOGS")}
        added = []
        for n in names:
            if n not in present:
                if self.attach(n, actor).get("state") == "attached":
                    added.append(n)
        return {"added": added}

    # ---- the catalog: namespaces, tables and schemas ------------------------------------------------
    def _rest(self, r: dict):
        token, _ = self._token(r["name"])
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        c = self._http()
        q = f"?warehouse={urlquote(r['warehouse'], safe='')}" if r["warehouse"] else ""
        resp = c.get(f"{r['uri']}/v1/config{q}", headers=headers)
        if resp.status_code != 200:
            c.close()
            raise ExternalCatalogError(f"the catalog's config: HTTP {resp.status_code}", 502)
        cfg = resp.json() or {}
        prefix = (cfg.get("overrides") or {}).get("prefix") or (cfg.get("defaults") or {}).get("prefix") or ""
        base = f"{r['uri']}/v1/" + (f"{urlquote(prefix, safe='')}/" if prefix else "")

        def get(path: str, params: Optional[dict] = None) -> dict:
            out = c.get(base + path, headers=headers, params=params)
            if out.status_code != 200:
                raise ExternalCatalogError(f"the catalog answered HTTP {out.status_code} for {path.split('?')[0]}", 502)
            return out.json()
        return c, get

    @staticmethod
    def _pages(get, path: str, key: str, params: Optional[dict] = None) -> list:
        out, token = [], None
        for _ in range(1000):
            p = dict(params or {})
            if token:
                p["pageToken"] = token
            doc = get(path, p or None)
            out += doc.get(key) or []
            token = doc.get("next-page-token")
            if not token:
                return out
        return out

    def sync(self, name: str, actor: str) -> dict:
        """Reads the external catalog's namespaces, tables and schemas into Cairn's catalog."""
        from types import SimpleNamespace

        from cairn.common.proposals import make_mcp
        from cairn.dataaccess.trino_catalog import TrinoCatalogSync, trino_catalog_tables
        from cairn.sdk.source import Dataset, Field, to_mcps

        r = self._row(name)
        started = time.time()
        try:
            client, get = self._rest(r)
            try:
                namespaces: list[list[str]] = []
                todo: list[Optional[list[str]]] = [None]
                while todo:
                    parent = todo.pop()
                    params = {"parent": "\x1f".join(parent)} if parent else None
                    for ns in self._pages(get, "namespaces", "namespaces", params):
                        if isinstance(ns, list) and ns and ns not in namespaces and (parent is None or len(ns) > len(parent)):
                            namespaces.append(ns)
                            todo.append(ns)
                    if len(namespaces) > MAX_TABLES:
                        raise ExternalCatalogError("too many namespaces", 413)
                tables: list[tuple[list[str], str]] = []
                for ns in namespaces:
                    enc = urlquote("\x1f".join(ns), safe="")
                    for ident in self._pages(get, f"namespaces/{enc}/tables", "identifiers"):
                        tables.append((ns, str(ident.get("name"))))
                    if len(tables) > MAX_TABLES:
                        raise ExternalCatalogError(f"more than {MAX_TABLES} tables", 413)
                loaded = []
                for ns, table in tables:
                    enc = urlquote("\x1f".join(ns), safe="")
                    doc = get(f"namespaces/{enc}/tables/{urlquote(table, safe='')}")
                    loaded.append((ns, table, doc.get("metadata") or {}))
            finally:
                client.close()
        except ExternalCatalogError as e:
            self._record_sync(name, {"error": str(e)})
            raise
        except Exception as e:  # noqa: BLE001 - transport errors name no secret
            self._record_sync(name, {"error": type(e).__name__})
            raise ExternalCatalogError(f"the catalog could not be read ({type(e).__name__})", 502)
        mcps: list[dict] = []
        urns: set[str] = set()
        columns = 0
        keeper = SimpleNamespace(graph=self.graph)
        for ns, table, meta in loaded:
            schema_id = meta.get("current-schema-id")
            schema = next((s for s in meta.get("schemas") or [] if s.get("schema-id") == schema_id),
                          (meta.get("schemas") or [{}])[-1] if meta.get("schemas") else {})
            fields = []
            for f in schema.get("fields") or []:
                ftype, native = iceberg_field_type(f.get("type"))
                fields.append(Field(path=str(f.get("name")), type=ftype, native_type=native,
                                    nullable=not f.get("required", False), description=f.get("doc") or None))
            columns += len(fields)
            schema_name = ".".join(ns)
            platform, dname, env = self.access.dataset_name(name, schema_name, table)
            props = {"trino_catalog": name, "trino_schema": schema_name, "trino_table": table,
                     "external_catalog": name, "iceberg_location": str(meta.get("location") or "")}
            ds = Dataset(name=dname, fields=fields, subtype="Table", properties=props,
                         description=(meta.get("properties") or {}).get("comment") or None)
            out = [TrinoCatalogSync._keep_dbt_docs(keeper, m) for m in to_mcps(platform, [ds], env)]
            urns.add(out[0]["entityUrn"])
            mcps += out
        t = trino_catalog_tables
        key = SYNC_PREFIX + name
        with self.engine.connect() as conn:
            before = {x[0] for x in conn.execute(select(t.c.urn).where(t.c.connection == key))}
        gone = sorted(before - urns)
        mcps += [make_mcp(u, "status", {"removed": True}) for u in gone]
        if mcps:
            self.graph.emit(mcps)
        with self.engine.begin() as conn:
            conn.execute(delete(t).where(t.c.connection == key))
            if urns:
                conn.execute(t.insert(), [{"connection": key, "urn": u} for u in sorted(urns)])
        result = {"catalog": name, "namespaces": len(namespaces), "tables": len(urns), "columns": columns,
                  "removed": len(gone), "seconds": round(time.time() - started, 2)}
        self._record_sync(name, result)
        self._audit("external_catalog.synced", actor, name, result)
        return result

    def _record_sync(self, name: str, result: dict) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(external_catalogs).where(external_catalogs.c.name == name)
                         .values(last_sync_ms=_now(), last_sync=json.dumps(result)))

    def _forget_tables(self, name: str) -> int:
        from cairn.common.proposals import make_mcp
        from cairn.dataaccess.trino_catalog import trino_catalog_tables as t

        key = SYNC_PREFIX + name
        with self.engine.connect() as conn:
            urns = [x[0] for x in conn.execute(select(t.c.urn).where(t.c.connection == key))]
        if urns:
            self.graph.emit([make_mcp(u, "status", {"removed": True}) for u in urns])
        with self.engine.begin() as conn:
            conn.execute(delete(t).where(t.c.connection == key))
        return len(urns)

    # ---- the schedule -----------------------------------------------------------------------------
    def run_due(self, sync_every_s: float, reconcile_every_s: float = 60.0) -> None:
        from cairn.dataaccess.trino_catalog import trino_catalog_runs as runs

        now = _now()
        claims = [("ext-reconcile", reconcile_every_s)] if self.trino_connection else []
        if sync_every_s > 0:
            claims += [(SYNC_PREFIX + r["name"], sync_every_s) for r in self.list()]
        for key, every in claims:
            with self.engine.begin() as conn:
                row = conn.execute(select(runs.c.next_ms).where(runs.c.connection == key)).first()
                nxt = now + int(every * 1000)
                if row is None:
                    conn.execute(runs.insert().values(connection=key, next_ms=nxt, last_ms=now))
                elif row[0] > now or conn.execute(update(runs).where((runs.c.connection == key) & (runs.c.next_ms == row[0]))
                                                  .values(next_ms=nxt, last_ms=now)).rowcount != 1:
                    continue
            try:
                if key == "ext-reconcile":
                    out = self.reconcile()
                    if out["added"]:
                        logger.info("external catalogs added to Trino again: %s", out["added"])
                else:
                    self.sync(key.removeprefix(SYNC_PREFIX), f"urn:li:corpuser:{CATALOG_ADMIN}")
            except Exception as e:  # noqa: BLE001 - one catalog's failure never stops the others
                logger.warning("external catalog %s: %s", key, e)

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            try:
                self.audit.record("external_catalogs", action, actor, f"urn:li:externalCatalog:{name}", details)
            except Exception as e:  # noqa: BLE001
                logger.warning("audit failed for %s: %s", action, e)


def build_external_catalogs_router(svc: ExternalCatalogs, current_principal, authorize):
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/external-catalogs", tags=["external-catalogs"])

    def call(fn, *a):
        try:
            return fn(*a)
        except ExternalCatalogError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/oauth/tokens", include_in_schema=False)
    async def tokens(request: Request):
        """Trino's OAuth2 client credentials for an external catalog (client secret: Trino's access key)."""
        import base64
        from urllib.parse import parse_qs, unquote

        form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
        cid, secret = form.get("client_id", ""), form.get("client_secret", "")
        basic = request.headers.get("authorization", "")
        if basic.lower().startswith("basic "):
            try:
                u, _, pw = base64.b64decode(basic[6:]).decode().partition(":")
                cid, secret = unquote(u), unquote(pw)
            except ValueError:
                pass
        try:
            out = await run_in_threadpool(svc.broker, cid, secret, form.get("grant_type", ""))
        except ExternalCatalogError as e:
            err = str(e) if str(e) in ("invalid_client", "unsupported_grant_type") else "temporarily_unavailable"
            return JSONResponse({"error": err}, e.status if err != "temporarily_unavailable" else 503,
                                headers={"Cache-Control": "no-store"})
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @router.get("")
    def list_(p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return {"catalogs": svc.list()}

    @router.post("")
    def register(body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return call(svc.register, p.urn, body)

    @router.get("/{name}")
    def get(name: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return call(svc.get, name)

    @router.post("/{name}/sync")
    def sync(name: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_INGESTION", None)
        return call(svc.sync, name, p.urn)

    @router.post("/{name}/attach")
    def attach(name: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return call(svc.attach, name, p.urn)

    @router.patch("/{name}")
    def change(name: str, writable: bool = Body(..., embed=True), p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return call(svc.set_writable, p.urn, name, writable)

    @router.delete("/{name}")
    def remove(name: str, p=Depends(current_principal)):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return call(svc.delete, p.urn, name)

    return router

