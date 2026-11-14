"""SaaS data connectors: Salesforce and HubSpot objects loaded into governed Iceberg tables, incrementally (L61);
Google Sheets ranges loaded whole (queue row 27).

**Google Sheets** (``"kind": "google_sheets"``): ``object`` is ``<spreadsheet id>/<sheet name>``. The sheet's first
row is its header, turned into column names ("Order ID" is ``order_id``), and ``fields`` names the columns to
load. A sheet's rows have no ids or modification times, so each run replaces the table with the sheet as it is
(in one commit); ``id`` is the row number. The keys: ``service_account_json`` (a service account's JSON key; share
the spreadsheet with its address, read-only is enough) or ``api_key`` (a sheet shared with anyone with the link),
and optionally ``base_url``.

``POST /v1/saas-syncs`` with ``{"name", "kind": "salesforce" | "hubspot", "object": "Account" | "contacts",
"fields": [...], "target": "crm.accounts", "secret_scope": "crm-keys", "cron": "0 * * * *"}``.

* **Incremental.** Each run asks only for records modified since the last one it loaded (Salesforce
  ``SystemModstamp``, HubSpot ``hs_lastmodifieddate``), in modification order, and upserts them by ``id``
  into the target table (the batch's ids deleted and the batch appended, in one commit), which is created on
  the first run. The cursor advances after each committed batch (``CAIRN_SAAS_BATCH_ROWS``, 5,000), so a
  failed run resumes where it stopped. Records modified at the
  cursor's exact time are asked for again (``>=``) and upserted again, so none is skipped.
* **Governed.** The table is an ordinary lakehouse table: Cairn's policies, masks and row filters apply to
  everyone who reads it. The sync's owner needs ``WRITE_DATASET`` on it, checked when the sync is saved and
  at every run. The connector's keys live in a secret scope the owner may read
  (``cairn.identity.secret_scopes``), read as the owner at each run, never stored on the sync or logged.
  - Salesforce: ``instance_url``, ``client_id``, ``client_secret`` (a connected app's client credentials
    flow), or ``access_token``.
  - HubSpot: ``access_token`` (a private app), and optionally ``base_url``.
* **Columns.** ``id``, the chosen fields (as text, as the APIs return them), ``_modified`` and ``_synced_ms``.
  Adding a field adds a column. Deleted records are not removed (neither API's incremental query returns
  them).
* **Runs** go to a pool beside the scheduler's tick; each sync is claimed, so one scheduler runs it. A failed
  run is recorded, audited and told to the owner (``saas.sync_failed``).
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

import httpx
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, and_, delete, insert, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")           # Salesforce/HubSpot object and field names
TARGET = re.compile(r"^[a-z_][a-z0-9_]{0,63}\.[a-z_][a-z0-9_]{0,63}$")
KINDS = ("salesforce", "hubspot", "google_sheets", "delta_sharing", "rest")
SHEET = re.compile(r"^[A-Za-z0-9_-]{10,100}/[^/\\]{1,60}$")    # <spreadsheet id>/<sheet name>
# A Delta Sharing table: <share>.<schema>.<table> (names as the provider gives them).
SHARED = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,63}\.[A-Za-z0-9_][A-Za-z0-9_-]{0,63}\.[A-Za-z0-9_][A-Za-z0-9_-]{0,63}$")
COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

saas_syncs = Table(
    "cairn_saas_syncs", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("kind", String(16), nullable=False),
    Column("object", String(80), nullable=False),
    Column("fields", JSONText, nullable=False),
    Column("target", String(130), nullable=False),
    Column("secret_scope", String(128), nullable=False),
    Column("spec", JSONText, nullable=True),                  # kind rest: how to read the API (cairn.pipelines.rest_source)
    Column("cron", String(100), nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("cursor", String(64), nullable=True),
    Column("next_run_ms", BigInteger, nullable=True, index=True),
    Column("running", Boolean, nullable=False, default=False),
    Column("last_run_ms", BigInteger, nullable=True),
    Column("last_status", String(10), nullable=True),
    Column("last_rows", Integer, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_ms", BigInteger, nullable=False),
)


class SaasError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _next(cron: Optional[str], after_ms: int) -> Optional[int]:
    if not cron:
        return None
    from croniter import croniter

    return int(croniter(cron, datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)).get_next(float) * 1000)


# --------------------------------------------------------------------------- the two APIs
def salesforce_session(http: httpx.Client, secrets: dict[str, str]) -> tuple[str, str]:
    """(instance URL, access token): the given token, or one from a connected app's client credentials."""
    base = secrets.get("instance_url", "").rstrip("/")
    if not base.startswith("https://") and not os.environ.get("CAIRN_SAAS_ALLOW_HTTP"):
        raise SaasError("salesforce: instance_url must be https://")
    token = secrets.get("access_token")
    if not token:
        r = http.post(f"{base}/services/oauth2/token", data={"grant_type": "client_credentials",
                                                              "client_id": secrets.get("client_id", ""),
                                                              "client_secret": secrets.get("client_secret", "")})
        if r.status_code != 200:
            raise SaasError(f"salesforce: sign-in refused ({r.status_code})", 502)
        token = r.json()["access_token"]
    return base, token


def hubspot_session(secrets: dict[str, str]) -> tuple[str, str]:
    """(API base URL, private app token)."""
    base = (secrets.get("base_url") or "https://api.hubapi.com").rstrip("/")
    if not base.startswith("https://") and not os.environ.get("CAIRN_SAAS_ALLOW_HTTP"):
        raise SaasError("hubspot: base_url must be https://")
    token = secrets.get("access_token")
    if not token:
        raise SaasError("hubspot: the secret scope has no access_token", 400)
    return base, token


def salesforce_records(http: httpx.Client, secrets: dict[str, str], obj: str, fields: list[str],
                       cursor: Optional[str]) -> Iterator[dict]:
    """Records of ``obj`` modified at or after ``cursor`` (ISO time), oldest first: ``{id, modified, values}``."""
    base, token = salesforce_session(http, secrets)
    version = os.environ.get("CAIRN_SAAS_SALESFORCE_API", "v61.0")
    cols = ", ".join(["Id", "SystemModstamp", *[f for f in fields if f not in ("Id", "SystemModstamp")]])
    soql = f"SELECT {cols} FROM {obj}"
    if cursor:
        soql += f" WHERE SystemModstamp >= {_soql_time(cursor)}"   # a SOQL datetime literal: not quoted
    soql += " ORDER BY SystemModstamp ASC"
    headers = {"Authorization": f"Bearer {token}"}
    r = http.get(f"{base}/services/data/{version}/query", params={"q": soql}, headers=headers)
    while True:
        if r.status_code != 200:
            raise SaasError(f"salesforce: query refused ({r.status_code}): {r.text[:200]}", 502)
        body = r.json()
        for rec in body.get("records") or []:
            yield {"id": str(rec["Id"]), "modified": str(rec["SystemModstamp"]),
                   "values": {f: rec.get(f) for f in fields}}
        nxt = body.get("nextRecordsUrl")
        if body.get("done", True) or not nxt:
            return
        r = http.get(f"{base}{nxt}", headers=headers)


def hubspot_records(http: httpx.Client, secrets: dict[str, str], obj: str, fields: list[str],
                    cursor: Optional[str]) -> Iterator[dict]:
    """Records of ``obj`` modified at or after ``cursor`` (epoch ms), oldest first."""
    base, token = hubspot_session(secrets)
    headers = {"Authorization": f"Bearer {token}"}
    since = cursor
    while True:                                  # a search returns at most 10,000 records: then ask again from the last
        after, last, seen = None, None, 0
        while True:
            body: dict[str, Any] = {"sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
                                    "properties": [*fields, "hs_lastmodifieddate"], "limit": 100}
            if since:
                body["filterGroups"] = [{"filters": [{"propertyName": "hs_lastmodifieddate", "operator": "GTE",
                                                      "value": since}]}]
            if after:
                body["after"] = after
            r = http.post(f"{base}/crm/v3/objects/{obj}/search", json=body, headers=headers)
            if r.status_code != 200:
                raise SaasError(f"hubspot: search refused ({r.status_code}): {r.text[:200]}", 502)
            out = r.json()
            for rec in out.get("results") or []:
                props = rec.get("properties") or {}
                modified = props.get("hs_lastmodifieddate") or rec.get("updatedAt") or ""
                seen += 1
                last = modified
                yield {"id": str(rec["id"]), "modified": str(modified), "values": {f: props.get(f) for f in fields}}
            after = ((out.get("paging") or {}).get("next") or {}).get("after")
            if not after:
                return
            if int(after) >= 10_000:
                break
        if not last or last == since:
            raise SaasError("hubspot: over 10,000 records modified at one time; narrow the object", 502)
        since = _hubspot_ms(last)


def _soql_time(v: str) -> str:
    """A SOQL datetime literal (``2026-11-05T10:00:00Z``) from a stored cursor; seconds floored, so records
    modified within the cursor's second are asked for again (and upserted again)."""
    s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v.replace("Z", "+00:00"))
    return datetime.fromisoformat(s).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hubspot_ms(v: str) -> str:
    """HubSpot's filter takes epoch milliseconds; its properties come back as ISO times."""
    if re.fullmatch(r"\d+", v or ""):
        return v
    return str(int(datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp() * 1000))


REQUIRED_KEYS = {"salesforce": ("instance_url",), "hubspot": ("access_token",), "google_sheets": (),
                 "delta_sharing": ("profile",), "rest": ("base_url",)}
OPTIONAL_KEYS = {"salesforce": ("client_id", "client_secret", "access_token"), "hubspot": ("base_url",),
                 "google_sheets": ("service_account_json", "api_key", "base_url"), "delta_sharing": (),
                 "rest": ("token", "username", "password")}


# --------------------------------------------------------------------------- Delta Sharing (recipient)
def delta_sharing_profile(secrets: dict[str, str]) -> tuple[str, str]:
    """(endpoint, bearer token) from a Delta Sharing profile file (``shareCredentialsVersion`` 1: what Databricks
    and other providers hand a recipient). An expired profile is refused before any request."""
    import json as _json

    try:
        prof = _json.loads(secrets["profile"])
        version, endpoint, token = int(prof.get("shareCredentialsVersion") or 0), str(prof["endpoint"]), str(prof["bearerToken"])
    except Exception as e:  # noqa: BLE001 - never echo the profile (it holds the token)
        raise SaasError(f"delta_sharing: profile is not a Delta Sharing profile file ({type(e).__name__})")
    if version != 1 or (prof.get("type") not in (None, "bearer_token")):
        raise SaasError("delta_sharing: only bearer-token profiles (shareCredentialsVersion 1) are supported")
    expires = prof.get("expirationTime")
    if expires:
        try:
            if datetime.fromisoformat(str(expires).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                raise SaasError("delta_sharing: the profile's token has expired; ask the provider for a new one", 403)
        except ValueError:
            raise SaasError("delta_sharing: the profile's expirationTime is not a time")
    return _public(endpoint.rstrip("/"), "delta_sharing: the profile's endpoint"), token


def delta_type(t: Any):
    """A Delta schema type (``schemaString``) -> an Arrow type."""
    import pyarrow as pa

    if isinstance(t, dict):
        kind = t.get("type")
        if kind == "struct":
            return pa.struct([pa.field(f["name"], delta_type(f["type"]), f.get("nullable", True)) for f in t.get("fields") or []])
        if kind == "array":
            return pa.list_(delta_type(t["elementType"]))
        if kind == "map":
            return pa.map_(delta_type(t["keyType"]), delta_type(t["valueType"]))
        raise SaasError(f"delta_sharing: unsupported type {kind}")
    s = str(t)
    simple = {"string": pa.string(), "long": pa.int64(), "integer": pa.int32(), "short": pa.int16(), "byte": pa.int8(),
              "float": pa.float32(), "double": pa.float64(), "boolean": pa.bool_(), "binary": pa.binary(),
              "date": pa.date32(), "timestamp": pa.timestamp("us", tz="UTC"), "timestamp_ntz": pa.timestamp("us")}
    if s in simple:
        return simple[s]
    m = re.fullmatch(r"decimal\((\d+),\s*(\d+)\)", s)
    if m:
        return pa.decimal128(int(m.group(1)), int(m.group(2)))
    raise SaasError(f"delta_sharing: unsupported type {s}")


def _partition_value(v: Optional[str], t):
    import pyarrow as pa

    if v is None:
        return pa.scalar(None, t)
    return pa.array([v], pa.string()).cast(t)[0]


def delta_sharing_read(http: httpx.Client, secrets: dict[str, str], obj: str, fields: list[str],
                       known_version: Optional[str], max_bytes: int) -> tuple[str, Optional[Any], list[dict], dict]:
    """Reads a shared table as a recipient: ``(version, Arrow table or None when the version is unchanged, the Delta
    schema's fields, facts)``. The table's Parquet files are fetched from the provider's pre-signed URLs (the
    bearer token goes to the sharing server only, never to the storage)."""
    import io
    import json as _json

    import pyarrow as pa
    import pyarrow.parquet as pq

    endpoint, token = delta_sharing_profile(secrets)
    share, schema, table = obj.split(".")
    base = f"{endpoint}/shares/{share}/schemas/{schema}/tables/{table}"
    headers = {"Authorization": f"Bearer {token}", "delta-sharing-capabilities": "responseformat=parquet"}
    r = http.get(f"{base}/version", headers=headers)
    if r.status_code != 200:
        raise SaasError(f"delta_sharing: the table's version: HTTP {r.status_code}", 403 if r.status_code in (401, 403) else 502)
    version = r.headers.get("delta-table-version") or ""
    if not version.isdigit():
        raise SaasError("delta_sharing: the server named no table version")
    r = http.post(f"{base}/query", headers={**headers, "Content-Type": "application/json"}, content=b"{}")
    if r.status_code != 200:
        raise SaasError(f"delta_sharing: the table's query: HTTP {r.status_code}", 403 if r.status_code in (401, 403) else 502)
    protocol, meta, files = {}, {}, []
    for line in r.text.splitlines():
        if not line.strip():
            continue
        doc = _json.loads(line)
        protocol = doc.get("protocol", protocol)
        meta = doc.get("metaData", meta)
        if "file" in doc:
            files.append(doc["file"])
    if int(protocol.get("minReaderVersion") or 1) > 1:
        raise SaasError("delta_sharing: the table needs a newer reader (deletion vectors or column mapping); "
                        "ask the provider to share it without them")
    try:
        delta_fields = _json.loads(meta["schemaString"])["fields"]
    except Exception:  # noqa: BLE001
        raise SaasError("delta_sharing: the server sent no schema")
    version = str(r.headers.get("delta-table-version") or version)
    facts = {"share": share, "schema": schema, "table": table, "version": int(version), "files": len(files),
             "endpoint_host": httpx.URL(endpoint).host, "delta_table_id": meta.get("id"),
             "partition_columns": list(meta.get("partitionColumns") or [])}
    if known_version is not None and known_version == version:
        return version, None, delta_fields, facts
    want = [f["name"] for f in delta_fields if not fields or f["name"] in fields]
    missing = [f for f in fields if f not in {d["name"] for d in delta_fields}]
    if missing:
        raise SaasError(f"delta_sharing: the table has no column {', '.join(missing)}")
    target = pa.schema([pa.field(f["name"], delta_type(f["type"]), f.get("nullable", True))
                        for f in delta_fields if f["name"] in want])
    parts, size, fetched = [], 0, 0
    too_big = SaasError(f"delta_sharing: the table is larger than CAIRN_SAAS_DELTA_SHARING_MAX_BYTES ({max_bytes})", 413)
    for f in files:
        size += int(f.get("size") or 0)                  # what the provider says: refused early when over
        if size > max_bytes:
            raise too_big
        url = _public(str(f.get("url") or ""), "delta_sharing: a data file's URL")
        buf = io.BytesIO()
        with http.stream("GET", url) as got:             # pre-signed: no Authorization header
            if got.status_code != 200:
                raise SaasError(f"delta_sharing: a data file: HTTP {got.status_code}", 502)
            for chunk in got.iter_bytes():               # what it sends: counted, never trusted to the size it said
                buf.write(chunk)
                if fetched + buf.tell() > max_bytes:
                    raise too_big
        fetched += buf.tell()
        t = pq.read_table(buf)
        cols = []
        for fld in target:
            if fld.name in t.column_names:
                cols.append(t.column(fld.name).cast(fld.type))
            elif fld.name in (f.get("partitionValues") or {}):
                pv = _partition_value(f["partitionValues"][fld.name], fld.type).as_py()
                cols.append(pa.array([pv] * t.num_rows, fld.type))
            else:
                cols.append(pa.nulls(t.num_rows, fld.type))
        parts.append(pa.Table.from_arrays(cols, schema=target))
    data = pa.concat_tables(parts) if parts else target.empty_table()
    return version, data, delta_fields, facts
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"


def _https(url: str, what: str) -> str:
    if not url.startswith("https://") and not os.environ.get("CAIRN_SAAS_ALLOW_HTTP"):
        raise SaasError(f"{what} must be https://")
    return url


def _public(url: str, what: str) -> str:
    """A URL a third party chose (a Delta Sharing provider's endpoint and its data files' pre-signed URLs), checked
    before Cairn fetches it (:func:`cairn.common.egress.check_public`): https (unless CAIRN_SAAS_ALLOW_HTTP), no
    credentials in it, and a public address. A link-local address (the cloud metadata service) is always refused;
    private, loopback and reserved ones unless CAIRN_SAAS_ALLOW_PRIVATE_NETWORKS=true. Otherwise a provider could
    have Cairn read an internal service and land its answer in a table the sync's owner reads."""
    import socket

    from cairn.common.egress import EgressRefused, check_public

    allow = os.environ.get("CAIRN_SAAS_ALLOW_PRIVATE_NETWORKS", "").lower() in ("1", "true", "yes")
    try:
        return check_public(url, what, allow_http=bool(os.environ.get("CAIRN_SAAS_ALLOW_HTTP")), allow_private=allow,
                            resolve=socket.getaddrinfo)
    except EgressRefused as e:
        msg = str(e)
        if "private address" in msg:
            msg += "; an operator allows SaaS syncs to reach private networks with CAIRN_SAAS_ALLOW_PRIVATE_NETWORKS=true"
        if "must be https" in msg:
            msg = f"{what} must be https://"
        raise SaasError(msg, e.status)


def google_session(http: httpx.Client, secrets: dict[str, str], scope: Optional[str] = None,
                   default_base: str = "https://sheets.googleapis.com",
                   what: str = "google_sheets") -> tuple[str, dict, dict]:
    """(API base URL, headers, query parameters): a service account's token (a JWT it signs, exchanged at its
    token_uri, for ``scope``), or an API key."""
    import base64
    import json as _json

    scope = scope or SHEETS_SCOPE
    base = _https((secrets.get("base_url") or default_base).rstrip("/"), f"{what}: base_url")
    if secrets.get("service_account_json"):
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            sa = _json.loads(secrets["service_account_json"])
            key = serialization.load_pem_private_key(sa["private_key"].encode(), password=None)
            email, token_uri = sa["client_email"], sa.get("token_uri") or "https://oauth2.googleapis.com/token"
        except Exception as e:  # noqa: BLE001 - never echo the key
            raise SaasError(f"{what}: service_account_json is not a service account key ({type(e).__name__})")
        _https(token_uri, f"{what}: the key's token_uri")

        def b64(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
        now = int(time.time())
        head = b64(_json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = b64(_json.dumps({"iss": email, "scope": scope, "aud": token_uri, "iat": now, "exp": now + 3600}).encode())
        sig = key.sign(f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
        r = http.post(token_uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": f"{head}.{claims}.{b64(sig)}"})
        if r.status_code != 200:
            raise SaasError(f"{what}: sign-in refused ({r.status_code})", 502)
        return base, {"Authorization": f"Bearer {r.json()['access_token']}"}, {}
    if secrets.get("api_key"):
        return base, {}, {"key": secrets["api_key"]}
    raise SaasError(f"{what}: the secret scope has neither service_account_json nor api_key", 400)


def google_sheets_records(http: httpx.Client, secrets: dict[str, str], obj: str, fields: list[str],
                          cursor: Optional[str]) -> Iterator[dict]:
    """Every row of the sheet (row 1 is the header), as records keyed by row number."""
    from urllib.parse import quote

    from cairn.pipelines.pipes import column_name

    base, headers, params = google_session(http, secrets)
    sheet_id, sheet = obj.split("/", 1)
    r = http.get(f"{base}/v4/spreadsheets/{quote(sheet_id)}/values/{quote(sheet, safe='')}", headers=headers,
                 params={**params, "valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"})
    if r.status_code != 200:
        raise SaasError(f"google_sheets: reading {sheet} failed ({r.status_code})", 502)
    values = r.json().get("values") or []
    if not values:
        return
    cols = {column_name(h, i): i for i, h in enumerate(values[0])}
    missing = [f for f in fields if f.lower() not in cols]
    if missing:
        raise SaasError(f"google_sheets: no column {', '.join(missing)} in {sheet} (it has {', '.join(cols)})")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    for n, row in enumerate(values[1:], start=2):
        if not any(v not in (None, "") for v in row):
            continue
        yield {"id": str(n), "modified": stamp,
               "values": {f: (row[cols[f.lower()]] if cols[f.lower()] < len(row) and row[cols[f.lower()]] != "" else None)
                          for f in fields}}


def connector_keys(read_secret: Callable[[str, str, str], str], owner: str, scope: str, kind: str) -> dict[str, str]:
    """A connector's keys, read from ``scope`` as ``owner``. A required key (or the scope) the owner cannot read
    stops the run before any request is made; an absent optional key is fine."""
    out = {}
    for k in REQUIRED_KEYS[kind]:
        try:
            out[k] = read_secret(owner, scope, k)
        except Exception as e:  # noqa: BLE001 - no scope, no key, or no right: fail closed
            raise SaasError(f"cannot read {k} from secret scope {scope} as {owner}: {e}", 403)
    for k in OPTIONAL_KEYS[kind]:
        try:
            out[k] = read_secret(owner, scope, k)
        except Exception as e:  # noqa: BLE001 - an absent optional key is fine; anything else is not
            if str(e) != "no such secret":
                raise SaasError(f"cannot read {k} from secret scope {scope}: {e}", 403)
    return out


# --------------------------------------------------------------------------- the service
class SaasSyncs:
    def __init__(self, engine, catalog: dict, read_secret: Callable[[str, str, str], str],
                 may_write: Callable[[str, str], bool], urn: Optional[Callable[[str, str], str]] = None,
                 audit=None, changes=None, notify=None, graph=None):
        """``read_secret(owner, scope, key)``: a secret, read as the owner. ``may_write(owner, urn)``: WRITE_DATASET.
        ``catalog``: pyiceberg REST catalog properties (``CAIRN_SAAS_CATALOG`` or ``CAIRN_PIPES_CATALOG``).
        ``graph``: Cairn Core, where a shared table (Delta Sharing) is catalogued with its lineage to the target."""
        self.engine, self.catalog_props, self.read_secret, self.may_write = engine, catalog, read_secret, may_write
        self.urn = urn or (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")
        self.audit, self.changes, self.notify, self.graph = audit, changes, notify, graph
        self.max_shared_bytes = int(os.environ.get("CAIRN_SAAS_DELTA_SHARING_MAX_BYTES", str(4 << 30)))
        self.batch = int(os.environ.get("CAIRN_SAAS_BATCH_ROWS", "5000"))
        self.max_rest_rows = int(os.environ.get("CAIRN_SAAS_REST_MAX_ROWS", "1000000"))
        self.pool = ThreadPoolExecutor(max_workers=int(os.environ.get("CAIRN_SAAS_CONCURRENCY", "2")),
                                       thread_name_prefix="cairn-saas")
        self.timeout = float(os.environ.get("CAIRN_SAAS_HTTP_TIMEOUT", "60"))

    def _check(self, body: dict) -> dict:
        kind = body.get("kind")
        if kind not in KINDS:
            raise SaasError("kind: salesforce, hubspot, google_sheets, delta_sharing or rest")
        obj = str(body.get("object") or "")
        spec = None
        if kind == "rest":                              # a declarative REST source (cairn.pipelines.rest_source)
            from cairn.pipelines import rest_source

            if not rest_source.PATH.match(obj) or len(obj) > 80 or ".." in obj.split("/"):
                raise SaasError("object: the resource's path under the API's base address (e.g. v1/customers)")
            try:
                spec = rest_source.check_spec(body.get("spec"))
            except rest_source.RestSpecError as e:
                raise SaasError(str(e))
        elif body.get("spec") is not None:
            raise SaasError("spec: only for kind rest")
        elif kind == "google_sheets":
            if not SHEET.match(obj) or len(obj) > 80:
                raise SaasError("object: <spreadsheet id>/<sheet name> (at most 80 characters)")
        elif kind == "delta_sharing":
            if not SHARED.match(obj) or len(obj) > 80:
                raise SaasError("object: <share>.<schema>.<table> (at most 80 characters)")
        elif not IDENT.match(obj):
            raise SaasError("object: an object's API name (letters, digits and _)")
        fields = body.get("fields")
        if kind == "delta_sharing":                     # the columns to read; none: all of them
            fields = [] if fields is None else fields
            if not isinstance(fields, list) or not all(isinstance(f, str) and COLUMN.match(f) for f in fields):
                raise SaasError("fields: the shared table's columns to read (none: all)")
        elif kind == "rest":
            from cairn.pipelines import rest_source

            if not isinstance(fields, list) or not fields or len(fields) > 200 or not all(
                    isinstance(f, str) and rest_source.FIELD.match(f) for f in fields):
                raise SaasError("fields: the record's fields to load (names, or dotted paths such as address.city)")
            cols = [rest_source.column(f) for f in fields]
            taken = {c for c in cols if cols.count(c) > 1} | (set(cols) & {"id", "_modified", "_synced_ms"})
            if taken:
                raise SaasError(f"fields: two fields would be the column {sorted(taken)[0]} (id, _modified and "
                                "_synced_ms are the sync's own)")
        elif not isinstance(fields, list) or not fields or not all(isinstance(f, str) and IDENT.match(f) for f in fields):
            raise SaasError("fields: a list of field API names")
        target = str(body.get("target") or "")
        if not TARGET.match(target):
            raise SaasError("target: namespace.table (lowercase)")
        scope = str(body.get("secret_scope") or "")
        if not scope:
            raise SaasError("secret_scope: the secret scope that holds the connector's keys")
        cron = body.get("cron") or None
        if cron is not None:
            from croniter import croniter

            if not isinstance(cron, str) or not croniter.is_valid(cron):
                raise SaasError("cron: a cron expression (UTC)")
        return {"kind": kind, "object": obj, "fields": sorted(set(fields)), "target": target, "secret_scope": scope,
                "spec": spec, "cron": cron, "enabled": bool(body.get("enabled", True))}

    def _urn(self, target: str) -> str:
        return self.urn(*target.split("."))

    def _need_write(self, owner: str, target: str) -> None:
        if not self.may_write(owner, self._urn(target)):
            raise SaasError(f"{owner} may not write {target} (WRITE_DATASET)", 403)

    def save(self, actor: str, body: dict, name: Optional[str] = None) -> dict:
        v = self._check(body)
        self._need_write(actor, v["target"])
        now = _now()
        with self.engine.begin() as conn:
            if name is None:
                name = str(body.get("name") or "")
                if not NAME.match(name):
                    raise SaasError("name: lowercase letters, digits and -")
                if conn.execute(select(saas_syncs.c.id).where(saas_syncs.c.name == name)).first():
                    raise SaasError(f"a sync {name} exists", 409)
                conn.execute(insert(saas_syncs).values(id=uuid.uuid4().hex, name=name, owner=actor, created_ms=now,
                                                       running=False, next_run_ms=_next(v["cron"], now), **v))
            else:
                cur = self._get(name)
                if cur["owner"] != actor:
                    raise SaasError("only its owner changes a sync", 403)
                changed = {k: v[k] for k in v}
                if (cur["kind"], cur["object"], cur["target"], cur.get("spec")) != (
                        v["kind"], v["object"], v["target"], v["spec"]):
                    changed["cursor"] = None             # another object, table or way to read it: from the beginning
                conn.execute(update(saas_syncs).where(saas_syncs.c.name == name)
                             .values(next_run_ms=_next(v["cron"], now), **changed))
        self._audit(actor, "saas.sync_saved", name, {"kind": v["kind"], "object": v["object"], "target": v["target"]})
        return self.get(actor, name)

    def _get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(saas_syncs).where(saas_syncs.c.name == name)).first()
        if r is None:
            raise SaasError(f"no sync {name}", 404)
        return dict(r._mapping)

    def get(self, actor: str, name: str) -> dict:
        s = self._get(name)
        if s["owner"] != actor:
            raise SaasError(f"no sync {name}", 404)
        return s

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(saas_syncs).where(saas_syncs.c.owner == actor)
                                                           .order_by(saas_syncs.c.name))]

    def delete(self, actor: str, name: str) -> None:
        self.get(actor, name)
        with self.engine.begin() as conn:
            conn.execute(delete(saas_syncs).where(saas_syncs.c.name == name))
        self._audit(actor, "saas.sync_deleted", name, {})

    # ---- running ---------------------------------------------------------------------------------------
    def run_now(self, actor: str, name: str, wait: bool = False) -> dict:
        s = self.get(actor, name)
        if not self._claim(s):
            raise SaasError(f"{name} is running", 409)
        fut = self.pool.submit(self._run, s)
        return fut.result() if wait else {"sync": name, "status": "running"}

    def _claim(self, s: dict, next_run_ms: Optional[int] = None) -> bool:
        values: dict[str, Any] = {"running": True}
        if next_run_ms is not None:
            values["next_run_ms"] = next_run_ms
        cond = [saas_syncs.c.id == s["id"], saas_syncs.c.running.is_(False)]
        if next_run_ms is not None:
            cond.append(saas_syncs.c.next_run_ms == s["next_run_ms"])
        with self.engine.begin() as conn:
            return bool(conn.execute(update(saas_syncs).where(and_(*cond)).values(**values)).rowcount)

    def _catalog(self):
        from pyiceberg.catalog.rest import RestCatalog

        from cairn.pipelines.iceberg_v3 import enable

        enable()                                         # pyiceberg writes v3 tables too
        return RestCatalog("cairn-saas", **self.catalog_props)

    def _secrets(self, s: dict) -> dict[str, str]:
        return connector_keys(self.read_secret, s["owner"], s["secret_scope"], s["kind"])

    def _run(self, s: dict) -> dict:
        rows, error, cursor = 0, None, s["cursor"]
        try:
            self._need_write(s["owner"], s["target"])        # the owner may since have lost the right
            secrets = self._secrets(s)
            if s["kind"] == "delta_sharing":                 # a shared table, whole at each new version
                rows, cursor = self._run_shared(s, secrets)
            elif s["kind"] == "rest":
                rows, cursor = self._run_rest(s, secrets, cursor)
            else:
                rows, cursor = self._run_records(s, secrets, cursor)
        except Exception as e:  # noqa: BLE001 - recorded on the sync and told, never raised into the scheduler
            error = str(e)[:1000]
            logger.warning("saas sync %s failed: %s", s["name"], error)
        status = "failed" if error else "ok"
        with self.engine.begin() as conn:
            conn.execute(update(saas_syncs).where(saas_syncs.c.id == s["id"]).values(
                running=False, last_run_ms=_now(), last_status=status, last_rows=rows, last_error=error))
        self._audit(s["owner"], f"saas.sync_{'failed' if error else 'run'}", s["name"],
                    {"rows": rows, "cursor": cursor, **({"error": error} if error else {})})
        if rows and self.changes is not None:
            self.changes.record([self._urn(s["target"])], "saas", f"saas:{s['name']}")
        if error and self.notify is not None:
            try:
                self.notify("saas.sync_failed", f"SaaS sync {s['name']} failed", error, [s["owner"]], {"sync": s["name"]})
            except Exception:  # noqa: BLE001 - the failure is recorded either way
                logger.exception("could not tell about sync %s", s["name"])
        return {"sync": s["name"], "status": status, "rows": rows, "cursor": cursor, "error": error}

    def _run_rest(self, s: dict, secrets: dict[str, str], cursor: Optional[str]) -> tuple[int, Optional[str]]:
        """A declarative REST source. Incremental: batches are upserted as they arrive, and the cursor moves to the
        newest modification time only when the whole run has loaded (an API need not answer in order, so a cursor
        advanced mid-run could skip what a failed run never reached). Otherwise the table is replaced."""
        from cairn.pipelines.rest_source import rest_records

        spec = s["spec"] or {}
        whole = not spec.get("modified")
        rows, newest, batch = 0, cursor, []
        with httpx.Client(timeout=self.timeout) as http:
            for rec in rest_records(http, secrets, s["object"], s["fields"], spec, cursor, _public, error=SaasError):
                batch.append(rec)
                if rec["modified"] and (newest is None or rec["modified"] > newest):
                    newest = rec["modified"]
                if not whole and len(batch) >= self.batch:
                    self._commit(s, batch, advance=False)
                    rows += len(batch)
                    batch = []
                if whole and len(batch) > self.max_rest_rows:
                    raise SaasError(f"rest: over {self.max_rest_rows} records without a modification time to go by "
                                    "(CAIRN_SAAS_REST_MAX_ROWS); give modified and cursor_param", 502)
        if batch or whole:
            self._commit(s, batch, replace=whole, advance=False)
            rows += len(batch)
        if not whole and newest != cursor:
            with self.engine.begin() as conn:
                conn.execute(update(saas_syncs).where(saas_syncs.c.id == s["id"]).values(cursor=newest))
        return rows, (None if whole else newest)

    def _run_records(self, s: dict, secrets: dict[str, str], cursor: Optional[str]) -> tuple[int, Optional[str]]:
        rows = 0
        fetch = {"salesforce": salesforce_records, "hubspot": hubspot_records,
                 "google_sheets": google_sheets_records}[s["kind"]]
        whole = s["kind"] == "google_sheets"            # no ids or times to go by: the sheet replaces the table
        with httpx.Client(timeout=self.timeout) as http:
            batch: list[dict] = []
            for rec in fetch(http, secrets, s["object"], s["fields"], cursor):
                batch.append(rec)
                if len(batch) >= self.batch and not whole:
                    cursor = self._commit(s, batch)
                    rows += len(batch)
                    batch = []
            if batch or whole:
                cursor = self._commit(s, batch, replace=whole)
                rows += len(batch)
        return rows, cursor

    # ---- Delta Sharing: a shared table read as a recipient -----------------------------------------------
    def _run_shared(self, s: dict, secrets: dict[str, str]) -> tuple[int, Optional[str]]:
        """Reads the shared table when its version moved (the cursor is the version loaded last) and replaces the
        target with it in one commit; catalogues the shared table in Cairn Core with its lineage to the target."""
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as http:
            version, data, delta_fields, facts = delta_sharing_read(http, secrets, s["object"], s["fields"],
                                                                    s["cursor"], self.max_shared_bytes)
        self._catalog_shared(s, delta_fields, facts)
        if data is None:                                  # unchanged since the last run
            return 0, version
        self._replace(s, data, {"cairn.delta-sharing.table": s["object"], "cairn.delta-sharing.version": version})
        with self.engine.begin() as conn:
            conn.execute(update(saas_syncs).where(saas_syncs.c.id == s["id"]).values(cursor=version))
        return data.num_rows, version

    def _replace(self, s: dict, data, props: dict[str, str]) -> None:
        """The target becomes ``data`` (an Arrow table) in one commit: created on the first run, a column the share
        added is added to it, then overwritten."""
        from pyiceberg.exceptions import NoSuchTableError

        cat = self._catalog()
        ns, name = s["target"].split(".")
        try:
            table = cat.load_table((ns, name))
        except NoSuchTableError:
            try:
                cat.create_namespace(ns)
            except Exception:  # noqa: BLE001 - it exists
                pass
            table = cat.create_table((ns, name), schema=data.schema)
        have = set(table.schema().column_names)
        if any(c not in have for c in data.column_names):
            with table.update_schema() as u:
                u.union_by_name(data.schema)
            table = cat.load_table((ns, name))
        from cairn.pipelines.pipes import fit

        table.overwrite(fit(table, data, s["target"]), snapshot_properties=props)

    def shared_urn(self, obj: str) -> str:
        return f"urn:li:dataset:(urn:li:dataPlatform:databricks,{obj.lower()},PROD)"

    def _catalog_shared(self, s: dict, delta_fields: list[dict], facts: dict) -> None:
        """The shared table as a ``databricks`` dataset (schema, the share's facts) and the target's upstream."""
        if self.graph is None:
            return
        import json as _json

        from cairn.common.proposals import make_mcp
        from cairn.sdk.source import Dataset, Field, to_mcps

        def ftype(t: Any) -> str:
            if isinstance(t, dict):
                return {"struct": "record", "array": "array", "map": "map"}.get(str(t.get("type")), "string")
            t = str(t)
            if t in ("long", "integer", "short", "byte", "float", "double") or t.startswith("decimal"):
                return "number"
            return {"boolean": "boolean", "date": "date", "binary": "bytes"}.get(t, "time" if t.startswith("timestamp") else "string")
        try:
            fields = [Field(path=f["name"], type=ftype(f["type"]),
                            native_type=f["type"] if isinstance(f["type"], str) else _json.dumps(f["type"]),
                            nullable=bool(f.get("nullable", True)), description=(f.get("metadata") or {}).get("comment"))
                      for f in delta_fields]
            props = {"delta_sharing_share": facts["share"], "delta_sharing_schema": facts["schema"],
                     "delta_sharing_table": facts["table"], "delta_sharing_version": str(facts["version"]),
                     "delta_sharing_endpoint": str(facts["endpoint_host"]), "cairn_saas_sync": s["name"]}
            ds = Dataset(name=s["object"].lower(), fields=fields, subtype="Table", properties=props,
                         description="A table shared with Cairn through Delta Sharing")
            mcps = list(to_mcps("databricks", [ds], "PROD"))
            src, target = self.shared_urn(s["object"]), self._urn(s["target"])
            cur = self.graph.get_aspect(target, "upstreamLineage") or {}
            ups = [u for u in (cur.get("upstreams") or []) if u.get("dataset") != src]
            ups.append({"dataset": src, "type": "COPY", "properties": {"cairn.delta-sharing.version": str(facts["version"])},
                        "auditStamp": {"time": _now(), "actor": s["owner"]}})
            mcps.append(make_mcp(target, "upstreamLineage", {**cur, "upstreams": ups}))
            self.graph.emit(mcps)
        except Exception as e:  # noqa: BLE001 - the data is loaded; the catalog entry is reported, not fatal
            logger.warning("saas sync %s: the shared table was not catalogued: %s", s["name"], e)

    def _commit(self, s: dict, batch: list[dict], replace: bool = False, advance: bool = True) -> Optional[str]:
        """Upserts one batch by id (with ``replace``, the batch becomes the whole table), then advances the cursor
        (only once the table has committed; with ``advance`` off, the caller does at the end of its run)."""
        import pyarrow as pa
        from pyiceberg.exceptions import NoSuchTableError

        latest: dict[str, dict] = {}
        for rec in batch:                                  # a record twice in a batch: its latest version
            latest[rec["id"]] = rec
        recs = list(latest.values())
        now = _now()
        cols = {"id": pa.array([r["id"] for r in recs], pa.string())}
        for f in s["fields"]:
            cols[f.lower().replace(".", "_")] = pa.array([None if r["values"].get(f) is None else str(r["values"][f]) for r in recs],
                                       pa.string())
        cols["_modified"] = pa.array([r["modified"] for r in recs], pa.string())
        cols["_synced_ms"] = pa.array([now] * len(recs), pa.int64())
        data = pa.table(cols)
        cat = self._catalog()
        ns, name = s["target"].split(".")
        try:
            table = cat.load_table((ns, name))
        except NoSuchTableError:
            try:
                cat.create_namespace(ns)
            except Exception:  # noqa: BLE001 - it exists
                pass
            from pyiceberg.schema import Schema
            from pyiceberg.types import LongType, NestedField, StringType

            fields = [NestedField(1, "id", StringType(), required=True)]
            for i, c in enumerate(data.column_names[1:], start=2):
                fields.append(NestedField(i, c, LongType() if c == "_synced_ms" else StringType(), required=False))
            table = cat.create_table((ns, name), schema=Schema(*fields, identifier_field_ids=[1]))
        have = set(table.schema().column_names)
        new = [c for c in data.column_names if c not in have]
        if new:                                            # a field added to the sync: a column added to the table
            from pyiceberg.types import StringType

            # (Only the new columns: a union with the batch's schema would try to make the required id optional.)
            with table.update_schema() as u:
                for c in new:
                    u.add_column(c, StringType())
            table = cat.load_table((ns, name))
        from pyiceberg.expressions import In

        from cairn.pipelines.pipes import fit

        # Upsert by id in one commit: the batch's ids out, the batch in. (pyiceberg's own upsert compares against
        # the files' older schema once a column was added, and refuses.)
        rows = fit(table, data, s["target"])
        from pyiceberg.expressions import AlwaysTrue

        with table.transaction() as tx:
            tx.delete(AlwaysTrue() if replace else In("id", [r["id"] for r in recs]))
            if recs:
                tx.append(rows)
        if not advance:
            return None
        cursor = max(r["modified"] for r in recs) if recs else datetime.now(timezone.utc).isoformat()
        if s["kind"] == "hubspot":
            cursor = _hubspot_ms(cursor)
        with self.engine.begin() as conn:
            conn.execute(update(saas_syncs).where(saas_syncs.c.id == s["id"]).values(cursor=cursor))
        return cursor

    def run_due(self, now: Optional[int] = None, wait: bool = False) -> int:
        """Starts every enabled sync whose time has come (the scheduler's leader calls this each tick)."""
        now = now or _now()
        with self.engine.connect() as conn:
            due = [dict(r._mapping) for r in conn.execute(select(saas_syncs).where(and_(
                saas_syncs.c.enabled, saas_syncs.c.next_run_ms.is_not(None), saas_syncs.c.next_run_ms <= now)))]
        futures = []
        for s in due:
            if self._claim(s, _next(s["cron"], now)):
                futures.append(self.pool.submit(self._run, s))
        if wait:
            for f in futures:
                f.result()
        return len(futures)

    def _audit(self, actor: str, action: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("pipelines", action, actor, f"saas:{name}", details)


def build_saas_router(ss: SaasSyncs, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/saas-syncs", tags=["saas"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except SaasError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: ss.list(p.urn))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: ss.save(p.urn, body))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: ss.get(p.urn, name))

    @router.put("/{name}")
    async def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: ss.save(p.urn, body, name))

    @router.delete("/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        await call(lambda: ss.delete(p.urn, name))

    @router.post("/{name}/run", status_code=202)
    async def run(name: str, wait: bool = False, p=Depends(current_principal)):
        return await call(lambda: ss.run_now(p.urn, name, wait))

    return router
