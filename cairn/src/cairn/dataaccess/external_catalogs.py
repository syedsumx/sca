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

**Warehouse presets** (``preset``, L151) name the cloud warehouses' catalogs, so their tables are read in place
(zero copy) and catalogued under the warehouse's platform:

* ``snowflake_open_catalog``: Snowflake Open Catalog (managed Apache Polaris); ``uri``
  ``https://<org>-<account>.snowflakecomputing.com/polaris/api/catalog``, ``warehouse`` the Open Catalog catalog,
  OAuth2 client credentials (a service connection's client id and secret), scope ``PRINCIPAL_ROLE:ALL`` by default;
* ``snowflake_horizon``: Snowflake-managed Iceberg tables through Horizon's Iceberg REST endpoint; ``uri``
  ``https://<account>.snowflakecomputing.com/polaris/api/catalog``, ``warehouse`` the database, OAuth2 with a
  programmatic access token as the client secret (no client id) and scope ``session:role:<ROLE>``, or a bearer token;
* ``databricks_unity``: Unity Catalog's Iceberg REST endpoint (managed Iceberg and UniForm tables); ``uri``
  ``https://<workspace>/api/2.1/unity-catalog/iceberg-rest``, ``warehouse`` the Unity Catalog catalog, a personal
  access token (``auth: token``) or OAuth M2M (``auth: oauth2``: the service principal's client id and secret,
  token URL ``https://<workspace>/oidc/v1/token`` and scope ``all-apis`` by default);
* ``snowflake_federated``: live queries on Snowflake through Trino's Snowflake connector (``CREATE CATALOG …
  USING snowflake``); ``uri`` ``https://<account>.snowflakecomputing.com``, ``database``, ``warehouse`` (the
  virtual warehouse that runs the queries, on Snowflake's bill), ``role``, ``client_id`` the Snowflake user and
  ``credential_ref`` its password. A JDBC connector cannot ask Cairn's broker, so the user and password reach Trino
  as a **credential file** Cairn writes for it (``credential-provider.type=FILE``): in a directory Trino mounts
  (``CAIRN_EXTERNAL_CATALOGS_CREDENTIALS_DIR``, seen by Trino at ``…_TRINO_CREDENTIALS_DIR``) or a Kubernetes Secret
  Trino mounts (``CAIRN_EXTERNAL_CATALOGS_CREDENTIALS_SECRET``); never in the ``CREATE CATALOG`` statement (which
  Trino records with the query) nor in Trino's catalog store. With neither target set, it is refused.
* ``iceberg`` (the default): any other Iceberg REST catalog, catalogued as ``iceberg``.

A registration may name an ``owner`` (``urn:li:corpuser:…`` or ``urn:li:corpGroup:…``) and a ``domain``
(``urn:li:domain:…``): every table catalogued from it gets them in Cairn Core.

Nothing here logs or returns a credential: errors from the external side carry the status code only.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
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
    Column("preset", String(32), nullable=True),               # PRESETS (None: iceberg)
    Column("platform", String(32), nullable=True),             # the platform its tables are catalogued under
    Column("owner", String(512), nullable=True),               # a person or group: the tables' owner in Core
    Column("domain", String(512), nullable=True),              # urn:li:domain:…
    Column("database_name", String(256), nullable=True),       # snowflake_federated: the database
    Column("role_name", String(256), nullable=True),           # snowflake_federated: the role
)

NAME = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
ENV_REF = re.compile(r"^env:(CAIRN_EXTCAT_[A-Z0-9_]{1,64})$")
SCOPE_REF = re.compile(r"^scope:([A-Za-z0-9][A-Za-z0-9._-]{0,127})/([A-Za-z0-9][A-Za-z0-9._-]{0,127})$")
AUTHS = ("oauth2", "token", "none", "password")
# Warehouse presets (L151): the platform the tables are catalogued under, the Trino connector, the sign-ins the
# warehouse takes (the first is the default) and the defaults it implies.
PRESETS: dict[str, dict[str, Any]] = {
    "iceberg": {"platform": "iceberg", "connector": "iceberg", "auths": ("token", "oauth2", "none")},
    "snowflake_open_catalog": {"platform": "snowflake", "connector": "iceberg", "auths": ("oauth2",),
                               "scope": "PRINCIPAL_ROLE:ALL", "warehouse": "the Open Catalog catalog's name"},
    "snowflake_horizon": {"platform": "snowflake", "connector": "iceberg", "auths": ("oauth2", "token"),
                          "warehouse": "the Snowflake database", "no_client_id": True},
    "databricks_unity": {"platform": "databricks", "connector": "iceberg", "auths": ("token", "oauth2"),
                         "scope": "all-apis", "warehouse": "the Unity Catalog catalog", "token_path": "/oidc/v1/token"},
    "snowflake_federated": {"platform": "snowflake", "connector": "snowflake", "auths": ("password",)},
}
SNOWFLAKE_HOST = re.compile(r"^https://([A-Za-z0-9][A-Za-z0-9._-]{0,200})\.snowflakecomputing\.(com|cn)/?$")
SNOWFLAKE_NAME = re.compile(r"^[A-Za-z0-9_$.-]{1,255}$")      # Snowflake database, warehouse and role names
PRINCIPAL = re.compile(r"^urn:li:(corpuser|corpGroup):[^\s,()']{1,256}$")
DOMAIN = re.compile(r"^urn:li:domain:[^\s,()']{1,256}$")
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


def properties_text(values: dict[str, str]) -> str:
    """A Java properties file (what Trino's FILE credential provider reads): values escaped, so a password with
    ``\\``, a newline or non-ASCII characters comes through as it is."""
    def esc(v: str) -> str:
        out = []
        for i, ch in enumerate(str(v)):
            if ch == "\\":
                out.append("\\\\")
            elif ch in "\n\r\t\f":
                out.append({"\n": "\\n", "\r": "\\r", "\t": "\\t", "\f": "\\f"}[ch])
            elif ch == " " and i == 0:
                out.append("\\ ")
            elif ord(ch) < 0x20 or ord(ch) > 0x7e:
                out.append("".join(f"\\u{u:04x}" for u in _utf16(ch)))
            else:
                out.append(ch)
        return "".join(out)
    return "".join(f"{k}={esc(v)}\n" for k, v in values.items())


def _utf16(ch: str) -> list[int]:
    b = ch.encode("utf-16-be")
    return [int.from_bytes(b[i:i + 2], "big") for i in range(0, len(b), 2)]


def rest_prefix(prefix: str) -> str:
    """The catalog's ``prefix`` (from ``/v1/config``) as the path segments it names. The Iceberg REST spec and its
    clients put it in the path as given: Polaris and Open Catalog give one segment (``snow``), Unity Catalog several
    (``catalogs/<name>``), so slashes are kept and only characters a path cannot carry are escaped. A prefix that
    would climb out of ``/v1/`` (``..``) or carry a query is refused."""
    parts = [p for p in str(prefix).strip("/").split("/")]
    if not parts or any(p in ("", ".", "..") for p in parts) or any(ch in prefix for ch in "?#\\"):
        raise ExternalCatalogError(f"the catalog's config gave an unusable prefix: {prefix!r}", 502)
    return "/".join(urlquote(p, safe="") for p in parts)


class CredentialFiles:
    """Credential files Trino reads (``connection-credential-file``), in a directory Cairn and Trino share: Cairn
    writes ``<dir>/<catalog>.properties``, Trino sees it under ``trino_dir``."""

    def __init__(self, directory: str, trino_dir: str = "", mode: int = 0o640):
        self.directory, self.trino_dir, self.mode = directory, (trino_dir or directory).rstrip("/"), mode

    def path(self, name: str) -> str:
        return f"{self.trino_dir}/{name}.properties"

    def write(self, name: str, values: dict[str, str]) -> None:
        os.makedirs(self.directory, exist_ok=True)
        final = os.path.join(self.directory, f"{name}.properties")
        # A new file of its own (exclusive, never through a link someone placed in the shared directory), removed
        # if anything fails before it replaces the old one: no copy of the credential is left behind.
        tmp = f"{final}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), self.mode)
        try:
            try:
                os.fchmod(fd, self.mode)                 # the umask never widens or narrows it
                os.write(fd, properties_text(values).encode())
            finally:
                os.close(fd)
            os.replace(tmp, final)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def remove(self, name: str) -> None:
        try:
            os.remove(os.path.join(self.directory, f"{name}.properties"))
        except FileNotFoundError:
            pass


class CredentialSecret:
    """Credential files as keys of one Kubernetes Secret that Trino's pods mount (``<catalog>.properties``), written
    with Cairn's service account (``get``/``patch`` on that Secret only, ``create`` for the first one). The kubelet
    brings a change to the pods within about a minute; until then Trino refuses the catalog and Cairn adds it again
    on its next reconcile."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, secret: str, namespace: str = "", trino_dir: str = "/etc/trino/federation", api: str = "",
                 token_file: str = "", ca_file: str = ""):
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api = (api or (f"https://{host}:{port}" if host else "")).rstrip("/")
        if not namespace:
            try:
                namespace = open(f"{self.SA}/namespace").read().strip()
            except OSError:
                namespace = ""
        self.secret, self.namespace, self.trino_dir = secret, namespace, trino_dir.rstrip("/")
        self.token_file = token_file or f"{self.SA}/token"
        self.verify: Any = ca_file or (f"{self.SA}/ca.crt" if os.path.exists(f"{self.SA}/ca.crt") else True)

    def path(self, name: str) -> str:
        return f"{self.trino_dir}/{name}.properties"

    def _call(self, method: str, body: Any = None, ctype: str = "application/json"):
        import httpx

        if not self.api or not self.namespace:
            raise ExternalCatalogError("credential Secret: not in a Kubernetes cluster (no API server or namespace)", 503)
        try:
            token = open(self.token_file).read().strip()
        except OSError as e:
            raise ExternalCatalogError(f"credential Secret: the service account token: {type(e).__name__}", 503)
        base = f"/api/v1/namespaces/{self.namespace}/secrets"
        path = base if method == "POST" else f"{base}/{self.secret}"
        try:
            with httpx.Client(base_url=self.api, verify=self.verify, timeout=30,
                              headers={"Authorization": f"Bearer {token}", "Content-Type": ctype}) as c:
                return c.request(method, path, content=None if body is None else json.dumps(body))
        except httpx.HTTPError as e:                     # the transport's message names no value
            raise ExternalCatalogError(f"credential Secret: the Kubernetes API could not be reached ({type(e).__name__})", 502)

    def _patch(self, data: dict[str, Optional[str]]) -> None:
        r = self._call("PATCH", {"data": data}, "application/merge-patch+json")
        if r.status_code == 404:
            r = self._call("POST", {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                                    "metadata": {"name": self.secret, "namespace": self.namespace,
                                                 "labels": {"app.kubernetes.io/managed-by": "cairn"}},
                                    "data": {k: v for k, v in data.items() if v is not None}})
        if r.status_code >= 300:                          # the status only: the body may echo what was sent
            raise ExternalCatalogError(f"credential Secret {self.secret}: HTTP {r.status_code}", 502)

    def write(self, name: str, values: dict[str, str]) -> None:
        import base64

        self._patch({f"{name}.properties": base64.b64encode(properties_text(values).encode()).decode()})

    def remove(self, name: str) -> None:
        r = self._call("PATCH", {"data": {f"{name}.properties": None}}, "application/merge-patch+json")
        if r.status_code >= 300 and r.status_code != 404:
            raise ExternalCatalogError(f"credential Secret {self.secret}: HTTP {r.status_code}", 502)


def credential_sink_from_env() -> Optional[Any]:
    """Where Snowflake (JDBC) credentials go for Trino: a shared directory or a Kubernetes Secret; None: nowhere
    (live Snowflake catalogs are refused)."""
    d = os.environ.get("CAIRN_EXTERNAL_CATALOGS_CREDENTIALS_DIR", "")
    if d:
        mode = int(os.environ.get("CAIRN_EXTERNAL_CATALOGS_CREDENTIALS_MODE", "0640"), 8)
        return CredentialFiles(d, os.environ.get("CAIRN_EXTERNAL_CATALOGS_TRINO_CREDENTIALS_DIR", ""), mode)
    secret = os.environ.get("CAIRN_EXTERNAL_CATALOGS_CREDENTIALS_SECRET", "")
    if secret:
        return CredentialSecret(secret, os.environ.get("CAIRN_EXTERNAL_CATALOGS_CREDENTIALS_NAMESPACE", ""),
                                os.environ.get("CAIRN_EXTERNAL_CATALOGS_TRINO_CREDENTIALS_DIR", "") or "/etc/trino/federation")
    return None


class ExternalCatalogs:
    def __init__(self, engine, graph, access, data_access=None, audit=None,
                 read_secret: Optional[Callable[[str, str, str], str]] = None,
                 broker_url: str = "", trino_connection: str = "", trino_key: str = "",
                 key_env: str = "CAIRN_TRINO_ACCESS_KEY", timeout: float = 30.0, credentials: Optional[Any] = None):
        self.engine, self.graph, self.access, self.data_access, self.audit = engine, graph, access, data_access, audit
        self.credentials = credentials                 # CredentialFiles or CredentialSecret: JDBC sign-ins for Trino
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
        out["preset"] = r.get("preset") or "iceberg"
        out["platform"] = r.get("platform") or "iceberg"
        out["connector"] = PRESETS.get(out["preset"], PRESETS["iceberg"])["connector"]
        out["database"], out["role"] = out.pop("database_name", None), out.pop("role_name", None)
        return out

    @staticmethod
    def _connector(r: dict) -> str:
        return PRESETS.get(r.get("preset") or "iceberg", PRESETS["iceberg"])["connector"]

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
                rows = conn.execute(select(external_catalogs.c.name, external_catalogs.c.writable,
                                           external_catalogs.c.platform)).all()
            cache = {r[0]: {"platform": r[2] or "iceberg", "database": r[0], "writable": bool(r[1])} for r in rows}
            self._cache = (time.time(), cache)
        return cache.get(catalog)

    def _validate(self, body: dict) -> dict:
        allowed = {"name", "uri", "warehouse", "auth", "client_id", "credential_ref", "token_url", "scope",
                   "store_endpoint", "store_region", "writable", "description", "preset", "owner", "domain",
                   "database", "role"}
        extra = set(body) - allowed
        if extra:
            raise ExternalCatalogError(f"unknown fields: {sorted(extra)} (give a credential_ref, never a credential)", 422)
        preset_name = str(body.get("preset") or "iceberg")
        preset = PRESETS.get(preset_name)
        if preset is None:
            raise ExternalCatalogError(f"preset is one of {list(PRESETS)}", 422)
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
        auths = preset["auths"]
        default = "oauth2" if body.get("client_id") and "oauth2" in auths else auths[0]
        auth = str(body.get("auth") or default)
        if auth not in auths:
            raise ExternalCatalogError(f"auth for preset {preset_name} is one of {list(auths)}", 422)
        ref = body.get("credential_ref")
        if auth != "none":
            if not ref or not (ENV_REF.match(str(ref)) or SCOPE_REF.match(str(ref))):
                raise ExternalCatalogError("credential_ref is scope:<scope>/<key> or env:CAIRN_EXTCAT_<NAME>", 422)
        if auth in ("oauth2", "password") and not body.get("client_id") and not preset.get("no_client_id"):
            raise ExternalCatalogError(f"auth {auth} needs client_id" + (" (the Snowflake user)" if auth == "password" else ""), 422)
        for k in ("token_url", "store_endpoint"):
            if body.get(k) and not re.match(r"^https?://[^\s'\"]+$", str(body[k])):
                raise ExternalCatalogError(f"{k} is an http(s) address", 422)
        for k in ("warehouse", "scope", "store_region", "client_id", "database", "role"):
            if body.get(k) is not None and (not isinstance(body[k], str) or "'" in body[k] or len(body[k]) > 256):
                raise ExternalCatalogError(f"{k} is text (no quotes, at most 256 characters)", 422)
        if preset.get("warehouse") and not body.get("warehouse"):
            raise ExternalCatalogError(f"preset {preset_name} needs warehouse: {preset['warehouse']}", 422)
        scope = body.get("scope") or preset.get("scope")
        if preset_name == "snowflake_horizon" and auth == "oauth2" and not scope:
            raise ExternalCatalogError("snowflake_horizon with oauth2 needs scope session:role:<ROLE>", 422)
        if preset["connector"] == "snowflake":
            if not SNOWFLAKE_HOST.match(uri + "/"):
                raise ExternalCatalogError("uri is the account's https://<account>.snowflakecomputing.com", 422)
            for k in ("database", "warehouse", "role"):
                if body.get(k) and not SNOWFLAKE_NAME.match(str(body[k])):
                    raise ExternalCatalogError(f"{k} is a Snowflake name (letters, digits, _ $ . -)", 422)
            if not body.get("database"):
                raise ExternalCatalogError("preset snowflake_federated needs database", 422)
            if self.credentials is None:
                raise ExternalCatalogError("live Snowflake catalogs need where Trino reads their credentials: "
                                           "CAIRN_EXTERNAL_CATALOGS_CREDENTIALS_DIR or _CREDENTIALS_SECRET", 503)
        elif body.get("database") or body.get("role"):
            raise ExternalCatalogError("database and role are for preset snowflake_federated", 422)
        owner, domain = body.get("owner") or None, body.get("domain") or None
        if owner is not None and not PRINCIPAL.match(str(owner)):
            raise ExternalCatalogError("owner is urn:li:corpuser:… or urn:li:corpGroup:…", 422)
        if domain is not None and not DOMAIN.match(str(domain)):
            raise ExternalCatalogError("domain is urn:li:domain:…", 422)
        return {"name": name, "uri": uri, "warehouse": body.get("warehouse") or None, "auth": auth,
                "client_id": body.get("client_id") or None, "credential_ref": ref if auth != "none" else None,
                "token_url": body.get("token_url") or None, "oauth_scope": body.get("scope") or None,
                "store_endpoint": body.get("store_endpoint") or None, "store_region": body.get("store_region") or None,
                "writable": bool(body.get("writable", False)), "description": body.get("description") or None,
                "preset": preset_name, "platform": preset["platform"], "owner": owner, "domain": domain,
                "database_name": body.get("database") or None, "role_name": body.get("role") or None}

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
        federated = PRESETS[rec["preset"]]["connector"] != "iceberg"
        try:
            if federated:                              # the reference resolves (Snowflake checks it on first use)
                self._credential(self._row(rec["name"]))
            else:
                self._token(rec["name"], fresh=True)   # the reference resolves and the catalog accepts it
        except ExternalCatalogError as e:
            with self.engine.begin() as conn:
                conn.execute(delete(external_catalogs).where(external_catalogs.c.name == rec["name"]))
            self._cache = (0.0, {})
            raise ExternalCatalogError(f"the catalog did not accept Cairn's sign-in: {e}", e.status if e.status != 404 else 422)
        self._audit("external_catalog.registered", actor, rec["name"],
                    {k: rec[k] for k in ("uri", "warehouse", "auth", "credential_ref", "writable", "preset", "platform")})
        out: dict[str, Any] = {}
        if self.trino_connection:
            out["trino"] = self.attach(rec["name"], actor)
        if federated:                                  # catalogued through Trino: only once Trino has it
            try:
                out["sync"] = self.sync(rec["name"], actor)
            except ExternalCatalogError as e:
                out["sync"] = {"error": str(e)}
        else:
            out["sync"] = self.sync(rec["name"], actor)
        return {**self.get(rec["name"]), **out}

    def delete(self, actor: str, name: str) -> dict:
        r = self._row(name)
        dropped = None
        if self.trino_connection:
            try:
                self._trino(f'DROP CATALOG IF EXISTS "{name}"')
                dropped = True
            except Exception as e:  # noqa: BLE001 - recorded; the registration still goes
                dropped = False
                logger.warning("external catalog %s could not be dropped from Trino: %s", name, e)
        if self._connector(r) != "iceberg" and self.credentials is not None:
            try:
                self.credentials.remove(name)            # Trino no longer has a sign-in for it
            except Exception as e:  # noqa: BLE001 - recorded; the registration still goes
                logger.warning("external catalog %s: its credential file could not be removed: %s", name, e)
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

    @staticmethod
    def oauth_endpoint(r: dict) -> tuple[str, str]:
        """(token URL, scope) of an oauth2 catalog: as registered, else the preset's (Unity Catalog: the workspace's
        ``/oidc/v1/token`` and ``all-apis``; Open Catalog: ``PRINCIPAL_ROLE:ALL``), else the catalog's own endpoint."""
        preset = PRESETS.get(r.get("preset") or "iceberg", PRESETS["iceberg"])
        url = r.get("token_url")
        if not url and preset.get("token_path"):
            m = re.match(r"^(https?://[^/]+)", r["uri"])
            url = (m.group(1) if m else r["uri"]) + preset["token_path"]
        return url or f"{r['uri']}/v1/oauth/tokens", r.get("oauth_scope") or preset.get("scope") or "catalog"

    def _token(self, name: str, fresh: bool = False) -> tuple[Optional[str], int]:
        """(access token, seconds it lasts) for the catalog: cached until a minute before it ends."""
        with self._lock:
            hit = self._tokens.get(name)
            if hit and not fresh and hit[0] > time.time() + 60:
                return hit[1], max(60, int(hit[0] - time.time()))
        r = self._row(name)
        if self._connector(r) != "iceberg":              # a JDBC catalog: Trino reads its credential file
            raise ExternalCatalogError(f"{name} takes no tokens", 404)
        if r["auth"] == "none":
            return None, 3600
        secret = self._credential(r)
        if r["auth"] == "token":
            token, ttl = secret, 3600
        else:
            url, scope = self.oauth_endpoint(r)
            form = {"grant_type": "client_credentials", "client_secret": secret, "scope": scope}
            if r["client_id"]:                           # Horizon: a programmatic access token is the secret alone
                form["client_id"] = r["client_id"]
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

    def snowflake_properties(self, r: dict) -> dict[str, str]:
        """Trino's Snowflake connector for a ``snowflake_federated`` catalog: the account, database, virtual
        warehouse and role, and the credential file Cairn writes for it (never the user or password)."""
        if self.credentials is None:
            raise ExternalCatalogError("no credential file target for live Snowflake catalogs "
                                       "(CAIRN_EXTERNAL_CATALOGS_CREDENTIALS_DIR or _CREDENTIALS_SECRET)", 503)
        m = SNOWFLAKE_HOST.match(r["uri"] + "/")
        if m is None:
            raise ExternalCatalogError("uri is the account's https://<account>.snowflakecomputing.com", 422)
        props = {"connection-url": f"jdbc:snowflake://{m.group(1)}.snowflakecomputing.{m.group(2)}",
                 "snowflake.account": m.group(1), "snowflake.database": r["database_name"],
                 "credential-provider.type": "FILE", "connection-credential-file": self.credentials.path(r["name"])}
        if r.get("warehouse"):
            props["snowflake.warehouse"] = r["warehouse"]
        if r.get("role_name"):
            props["snowflake.role"] = r["role_name"]
        return props

    def _write_credentials(self, r: dict) -> None:
        """The Snowflake user and password, resolved now as the registering operator, into Trino's credential file."""
        if self.credentials is None:
            raise ExternalCatalogError("no credential file target for live Snowflake catalogs", 503)
        self.credentials.write(r["name"], {"connection-user": r["client_id"], "connection-password": self._credential(r)})

    def catalog_sql(self, r: dict) -> str:
        """The CREATE CATALOG statement: no credential in it, only Trino's own key or a credential file by reference."""
        if self._connector(r) == "snowflake":
            body = ", ".join(f'"{k}" = {_lit(v)}' for k, v in self.snowflake_properties(r).items())
            return f'CREATE CATALOG "{r["name"]}" USING snowflake WITH ({body})'
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
        signed_in = True
        try:
            if self._connector(r) != "iceberg":
                signed_in = False
                self._write_credentials(r)               # as it is now: a revoked reference stops the catalog here
                signed_in = True
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
            # Fail closed: a credential that can no longer be read leaves no sign-in behind. (A Trino failure keeps
            # the file: a mounted Secret reaches Trino a little later, and the next reconcile adds the catalog.)
            if not signed_in and self.credentials is not None:
                try:
                    self.credentials.remove(name)
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
        base = f"{r['uri']}/v1/" + (f"{rest_prefix(prefix)}/" if prefix else "")

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

    def _load_rest(self, r: dict) -> tuple[int, list[tuple[str, str, list, dict, Optional[str]]]]:
        """(namespaces, [(schema, table, fields, properties, description)]) from the Iceberg REST API."""
        from cairn.sdk.source import Field

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
        out = []
        for ns, table, meta in loaded:
            schema_id = meta.get("current-schema-id")
            schema = next((s for s in meta.get("schemas") or [] if s.get("schema-id") == schema_id),
                          (meta.get("schemas") or [{}])[-1] if meta.get("schemas") else {})
            fields = []
            for f in schema.get("fields") or []:
                ftype, native = iceberg_field_type(f.get("type"))
                fields.append(Field(path=str(f.get("name")), type=ftype, native_type=native,
                                    nullable=not f.get("required", False), description=f.get("doc") or None))
            out.append((".".join(ns), table, fields, {"iceberg_location": str(meta.get("location") or "")},
                        (meta.get("properties") or {}).get("comment") or None))
        return len(namespaces), out

    def _load_trino(self, r: dict) -> tuple[int, list[tuple[str, str, list, dict, Optional[str]]]]:
        """The same from Trino's information_schema, as Cairn's crawler (a JDBC catalog has no REST API to read)."""
        from cairn.dataaccess.trino import CRAWLER
        from cairn.dataaccess.trino_catalog import field_type
        from cairn.sdk.source import Field

        if self.data_access is None or not self.trino_connection:
            raise ExternalCatalogError("no Trino connection for external catalogs (CAIRN_EXTERNAL_CATALOGS_TRINO_CONNECTION)", 503)
        registry = self.data_access.registry
        definition = (registry.definitions() if registry is not None else {}).get(self.trino_connection)
        if not isinstance(definition, dict):
            raise ExternalCatalogError(f"no trino:// connection {self.trino_connection!r}", 503)
        name = r["name"]
        try:
            _, rows, truncated = self.data_access.run_on_trino(definition, f"urn:li:corpuser:{CRAWLER}", (
                f'SELECT table_schema, table_name, column_name, data_type, is_nullable, comment FROM "{name}".information_schema.columns '
                "WHERE table_schema <> 'information_schema' ORDER BY table_schema, table_name, ordinal_position"), 200_000)
        except Exception as e:  # noqa: BLE001 - Trino's message names no credential (the catalog holds a file path)
            raise ExternalCatalogError(f"Trino could not read the catalog: {str(e)[:300]}", 502)
        if truncated:
            raise ExternalCatalogError("more than 200,000 columns", 413)
        tables: dict[tuple[str, str], list] = {}
        for schema, table, column, dtype, nullable, comment in rows:
            tables.setdefault((schema, table), []).append(Field(
                path=column, type=field_type(dtype), native_type=dtype, nullable=str(nullable).upper() != "NO",
                description=comment or None))
        return len({s for s, _ in tables}), [(s, t, f, {}, None) for (s, t), f in tables.items()]

    def sync(self, name: str, actor: str) -> dict:
        """Reads the external catalog's namespaces, tables and schemas into Cairn's catalog (Cairn Core), with the
        registration's owner and domain."""
        from types import SimpleNamespace

        from cairn.common.proposals import make_mcp
        from cairn.dataaccess.trino_catalog import TrinoCatalogSync, trino_catalog_tables
        from cairn.sdk.source import Dataset, to_mcps

        r = self._row(name)
        started = time.time()
        try:
            namespaces, loaded = (self._load_rest(r) if self._connector(r) == "iceberg" else self._load_trino(r))
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
        preset = r.get("preset") or "iceberg"
        for schema_name, table, fields, extra, description in loaded:
            columns += len(fields)
            platform, dname, env = self.access.dataset_name(name, schema_name, table)
            props = {"trino_catalog": name, "trino_schema": schema_name, "trino_table": table,
                     "external_catalog": name, "external_catalog_preset": preset, **extra}
            ds = Dataset(name=dname, fields=fields, subtype="Table", properties=props, description=description)
            out = [TrinoCatalogSync._keep_dbt_docs(keeper, m) for m in to_mcps(platform, [ds], env)]
            urn = out[0]["entityUrn"]
            urns.add(urn)
            if r.get("owner"):
                out.append(make_mcp(urn, "ownership", {"owners": [{"owner": r["owner"], "type": "DATAOWNER"}],
                                                       "lastModified": {"time": _now(), "actor": actor}}))
            if r.get("domain"):
                out.append(make_mcp(urn, "domains", {"domains": [r["domain"]]}))
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
        result = {"catalog": name, "namespaces": namespaces, "tables": len(urns), "columns": columns,
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

