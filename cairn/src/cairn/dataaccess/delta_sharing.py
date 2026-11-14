"""Delta Sharing for Cairn's shares: the open REST protocol, so recipients read shared tables with any Delta Sharing
client (pandas and Spark through ``delta-sharing``, Power BI, Tableau, Databricks, Snowflake's connector) as well as
through Iceberg REST.

A share (``cairn.dataaccess.shares``) is one Delta Sharing share of the same name; a table's namespace is its schema.
The recipient gets a profile file when the share is made (``delta_sharing`` in the answer: the endpoint, a bearer
token and its expiry, the share's). Every request is checked against the share as it is now, as Iceberg REST
requests are:

* the bearer token is ``<share id>.<secret>``, the share's own credential: only a hash is kept. A revoked or expired
  share is refused at once (403), an unknown token too (401);
* only the shared tables are listed or read, and only while they may still be shared: never one a column mask or row
  filter applies to (its files hold what they hide), never one pinned to a region the recipient's is not allowed
  (data residency);
* a query loads the table from Cairn Catalog as the share itself (the catalog asks Cairn, as for any recipient) and
  answers with the current snapshot's Parquet files as pre-signed Cairn Store URLs. They are signed with the
  catalog's read-only keys for that one table and last ``CAIRN_SHARES_DELTA_URL_SECONDS`` (default 900), never past
  the share's end or the keys' own expiry: a revoked share's last URLs stop with them;
* Iceberg tables are served as Delta Sharing's Parquet response format: ``protocol`` (reader version 1), ``metaData``
  (the schema as Delta's ``schemaString``) and one ``file`` per data file, with its row count in ``stats``. A table
  with row-level deletes (delete files) is refused: its data files still hold the deleted rows. So is a table whose
  columns were renamed after files were written (the files carry the old names), and a column type Delta has no
  equivalent for (``time``, ``uuid``, ``fixed``). The table version is the snapshot's sequence number. Time travel
  (``version``, ``timestamp``) and change data feed are not offered (400).

Settings: ``CAIRN_SHARES_DELTA_SHARING_URL`` (the endpoint recipients reach, ``…/v1/delta-sharing``; without it,
the request's own base URL), ``CAIRN_SHARES_CATALOG_INTERNAL_URL`` (where Cairn reaches Cairn Catalog; default
``CAIRN_SHARES_CATALOG_URL``) and ``CAIRN_SHARES_STORE_URL`` (the store endpoint recipients fetch files from, signed
into the URLs; default the endpoint the catalog vends). Every query is audited (``share.delta_query``) and counted
in the share's usage.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any, Optional

from starlette.requests import Request  # module level: FastAPI resolves the endpoints' annotations here

from cairn.dataaccess.shares import AUDIENCE, PREFIX, ShareError, Shares, _hash, _split

logger = logging.getLogger(__name__)

PROTOCOL = {"protocol": {"minReaderVersion": 1}}
CAPABILITIES = "delta-sharing-capabilities"


class SharingError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def delta_type(t: Any) -> Any:
    """An Iceberg type as Delta's schema JSON."""
    from pyiceberg import types as T

    simple = {T.BooleanType: "boolean", T.IntegerType: "integer", T.LongType: "long", T.FloatType: "float",
              T.DoubleType: "double", T.StringType: "string", T.DateType: "date", T.BinaryType: "binary",
              T.TimestamptzType: "timestamp", T.TimestampType: "timestamp"}
    for cls, name in simple.items():
        if isinstance(t, cls):
            return name
    if isinstance(t, T.DecimalType):
        return f"decimal({t.precision},{t.scale})"
    if isinstance(t, T.StructType):
        return {"type": "struct", "fields": [delta_field(f) for f in t.fields]}
    if isinstance(t, T.ListType):
        return {"type": "array", "elementType": delta_type(t.element_type), "containsNull": not t.element_required}
    if isinstance(t, T.MapType):
        return {"type": "map", "keyType": delta_type(t.key_type), "valueType": delta_type(t.value_type),
                "valueContainsNull": not t.value_required}
    raise SharingError(400, "INVALID_PARAMETER_VALUE", f"columns of type {t} cannot be shared through Delta Sharing")


def delta_field(f: Any) -> dict:
    return {"name": f.name, "type": delta_type(f.field_type), "nullable": not f.required, "metadata": {}}


class DeltaSharing:
    def __init__(self, shares: Shares, catalog_url: Optional[str] = None, store_url: Optional[str] = None,
                 url_seconds: Optional[int] = None, endpoint: Optional[str] = None):
        self.shares = shares
        self.catalog_url = (catalog_url or os.environ.get("CAIRN_SHARES_CATALOG_INTERNAL_URL")
                            or shares.catalog_url or "").rstrip("/")
        self.store_url = (store_url or os.environ.get("CAIRN_SHARES_STORE_URL") or "").rstrip("/")
        self.url_seconds = int(url_seconds or os.environ.get("CAIRN_SHARES_DELTA_URL_SECONDS", "900"))
        if not 60 <= self.url_seconds <= 3600:
            raise ValueError("CAIRN_SHARES_DELTA_URL_SECONDS must be between 60 and 3600 seconds")
        self.endpoint = (endpoint or os.environ.get("CAIRN_SHARES_DELTA_SHARING_URL") or "").rstrip("/")

    # ---- the recipient -------------------------------------------------------------------------------
    def profile(self, share: dict, base_url: str) -> dict:
        """The profile file the recipient's client reads, for a share just made (its credential is in it)."""
        sid, secret = share["credential"].split(":", 1)
        expires = datetime.datetime.fromtimestamp(share["expires_at"], tz=datetime.timezone.utc)
        return {"shareCredentialsVersion": 1, "endpoint": self.endpoint or f"{base_url.rstrip('/')}/v1/delta-sharing",
                "bearerToken": f"{sid}.{secret}", "expirationTime": expires.strftime("%Y-%m-%dT%H:%M:%S.000Z")}

    def recipient(self, authorization: str) -> dict:
        """The share a bearer token stands for, if it is active now."""
        token = authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""
        sid, _, secret = token.partition(".")
        with self.shares.engine.connect() as conn:
            row = self.shares._row(conn, sid) if sid and secret else None
        if row is None or not hmac.compare_digest(row["secret_hash"], _hash(secret)):
            if row is not None:
                self.shares._count(row["id"], "", failures=1)
            raise SharingError(401, "UNAUTHENTICATED", "unknown bearer token")
        if row["revoked_s"] is not None or int(time.time()) >= row["expires_s"]:
            self.shares._count(row["id"], "", refusals=1)
            raise SharingError(403, "PERMISSION_DENIED", "the share was revoked" if row["revoked_s"] else "the share has expired")
        return row

    def _readable(self, row: dict) -> list[str]:
        """The shared tables the recipient may read now: the same decision Cairn Catalog gets."""
        tables = sorted(json.loads(row["tables"]))
        asks = [{"namespace": _split(t)[0], "name": _split(t)[1]} for t in tables]
        decided = self.shares.decide(f"{PREFIX}{row['id']}", asks)["tables"]
        return [t for t, d in zip(tables, decided) if d.get("read")]

    def _share(self, row: dict, share: str) -> None:
        if share != row["name"]:
            raise SharingError(404, "RESOURCE_DOES_NOT_EXIST", f"share {share} does not exist")

    def list_shares(self, row: dict) -> dict:
        return {"items": [{"name": row["name"], "id": row["id"]}]}

    def get_share(self, row: dict, share: str) -> dict:
        self._share(row, share)
        return {"share": {"name": row["name"], "id": row["id"]}}

    def list_schemas(self, row: dict, share: str) -> dict:
        self._share(row, share)
        names = sorted({".".join(_split(t)[0]) for t in self._readable(row)})
        return {"items": [{"name": n, "share": share} for n in names]}

    def list_tables(self, row: dict, share: str, schema: Optional[str] = None) -> dict:
        self._share(row, share)
        items = []
        for t in self._readable(row):
            ns, name = _split(t)
            if schema is None or ".".join(ns) == schema:
                items.append({"name": name, "schema": ".".join(ns), "share": share, "shareId": row["id"],
                              "id": hashlib.sha256(f"{row['id']}/{t}".encode()).hexdigest()[:32]})
        if schema is not None and not items and schema not in {".".join(_split(t)[0]) for t in json.loads(row["tables"])}:
            raise SharingError(404, "RESOURCE_DOES_NOT_EXIST", f"schema {schema} does not exist")
        return {"items": items}

    # ---- a table ---------------------------------------------------------------------------------------
    def _load(self, row: dict, share: str, schema: str, table: str):
        self._share(row, share)
        full = f"{schema}.{table}"
        if full not in self._readable(row):
            self.shares._count(row["id"], full[:512], refusals=1)
            raise SharingError(404, "RESOURCE_DOES_NOT_EXIST", f"table {schema}.{table} does not exist in share {share}")
        if not self.catalog_url:
            raise SharingError(503, "INTERNAL_ERROR", "Delta Sharing needs Cairn Catalog's address "
                                                      "(CAIRN_SHARES_CATALOG_INTERNAL_URL or CAIRN_SHARES_CATALOG_URL)")
        from pyiceberg.catalog.rest import RestCatalog

        # As the share itself: the catalog asks Cairn about this very table and vends read-only keys for it alone.
        ttl = max(60, min(self.url_seconds, row["expires_s"] - int(time.time())))
        token = self.shares.tokens.mint(f"urn:li:corpuser:{PREFIX}{row['id']}", ttl, audience=AUDIENCE,
                                        extra={"share": row["id"]})["token"]
        try:
            cat = RestCatalog(f"delta-sharing-{row['id']}", uri=self.catalog_url, token=token,
                              **{"header.X-Iceberg-Access-Delegation": "vended-credentials"})
            return cat.load_table((*schema.split("."), table)), ttl
        except Exception as e:  # noqa: BLE001 - refused or unreachable: nothing is served
            logger.warning("delta sharing: loading %s for share %s failed: %s", full, row["id"], e)
            raise SharingError(403, "PERMISSION_DENIED", f"table {full} cannot be read now")

    @staticmethod
    def _version(t) -> int:
        snap = t.current_snapshot()
        return int(snap.sequence_number or 0) if snap is not None else 0

    def version(self, row: dict, share: str, schema: str, table: str) -> int:
        t, _ = self._load(row, share, schema, table)
        return self._version(t)

    def _metadata(self, t, full: str) -> dict:
        meta = t.metadata
        names: dict[int, set[str]] = {}
        for s in meta.schemas:
            for f in s.fields:
                names.setdefault(f.field_id, set()).add(f.name)
        if any(len(n) > 1 for n in names.values()):
            raise SharingError(400, "INVALID_PARAMETER_VALUE",
                               f"{full} has renamed columns: its files carry the old names, so it is not served as Delta")
        schema = {"type": "struct", "fields": [delta_field(f) for f in t.schema().fields]}
        return {"metaData": {"id": str(meta.table_uuid), "name": full, "format": {"provider": "parquet", "options": {}},
                             "schemaString": json.dumps(schema), "partitionColumns": [], "configuration": {},
                             "version": self._version(t), "size": None, "numFiles": None}}

    def metadata(self, row: dict, share: str, schema: str, table: str) -> tuple[int, list[dict]]:
        t, _ = self._load(row, share, schema, table)
        return self._version(t), [PROTOCOL, self._metadata(t, f"{schema}.{table}")]

    def query(self, row: dict, share: str, schema: str, table: str, body: dict) -> tuple[int, list[dict]]:
        if body.get("version") is not None or body.get("timestamp") is not None or body.get("startingVersion") is not None:
            raise SharingError(400, "INVALID_PARAMETER_VALUE", "time travel is not offered: the current version only")
        full = f"{schema}.{table}"
        t, ttl = self._load(row, share, schema, table)
        meta = self._metadata(t, full)
        version = self._version(t)
        lines: list[dict] = [PROTOCOL, meta]
        if t.current_snapshot() is not None:
            tasks = list(t.scan().plan_files())
            if any(task.delete_files for task in tasks):
                raise SharingError(400, "INVALID_PARAMETER_VALUE",
                                   f"{full} has row-level deletes: its data files still hold the deleted rows")
            presign, expires_ms = self._signer(t, ttl)
            for task in tasks:
                f = task.file
                if str(f.file_format).upper().find("PARQUET") < 0:
                    raise SharingError(400, "INVALID_PARAMETER_VALUE", f"{full} has data files that are not Parquet")
                lines.append({"file": {"url": presign(f.file_path), "id": hashlib.sha256(f.file_path.encode()).hexdigest(),
                                       "partitionValues": {}, "size": int(f.file_size_in_bytes),
                                       "stats": json.dumps({"numRecords": int(f.record_count)}),
                                       "version": version, "expirationTimestamp": expires_ms}})
        self.shares._count(row["id"], full, loads=1)
        if self.shares.audit is not None:
            self.shares.audit.record("shares", "share.delta_query", f"urn:li:corpuser:{PREFIX}{row['id']}", None,
                                     {"share": row["id"], "name": row["name"], "table": full, "version": version,
                                      "files": len(lines) - 2})
        return version, lines

    def _signer(self, t, ttl: int):
        """Pre-signs GETs of the table's files with the read-only keys the catalog vended for it."""
        import boto3
        from botocore.config import Config

        p = t.io.properties
        if not (p.get("s3.access-key-id") and p.get("s3.session-token")):
            raise SharingError(403, "PERMISSION_DENIED", "the catalog vended no scoped keys for this table")
        endpoint = self.store_url or p.get("s3.endpoint")
        s3 = boto3.client("s3", endpoint_url=endpoint, region_name=p.get("s3.region") or "us-east-1",
                          aws_access_key_id=p["s3.access-key-id"], aws_secret_access_key=p["s3.secret-access-key"],
                          aws_session_token=p["s3.session-token"],
                          config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
        seconds = ttl
        expiry = p.get("s3.session-token-expires-at-ms")
        if expiry:                                   # never past the keys' own end
            seconds = max(1, min(seconds, int((int(expiry) - time.time() * 1000) / 1000)))
        expires_ms = int(time.time() * 1000) + seconds * 1000

        def presign(path: str) -> str:
            if not path.startswith("s3://"):
                raise SharingError(400, "INVALID_PARAMETER_VALUE", "a data file is not in Cairn Store")
            bucket, _, key = path[5:].partition("/")
            return s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=seconds)
        return presign, expires_ms


def build_delta_sharing_router(ds: DeltaSharing):
    from fastapi import APIRouter, Body
    from fastapi.responses import JSONResponse, Response

    router = APIRouter(prefix="/v1/delta-sharing", tags=["shares"])

    def fail(e: SharingError) -> JSONResponse:
        return JSONResponse({"errorCode": e.code, "message": str(e)}, status_code=e.status,
                            headers={"WWW-Authenticate": 'Bearer realm="delta-sharing"'} if e.status == 401 else None)

    def answer(request: Request, fn, *a):
        try:
            row = ds.recipient(request.headers.get("authorization", ""))
            return JSONResponse(fn(row, *a))
        except SharingError as e:
            return fail(e)
        except ShareError as e:
            return fail(SharingError(e.status, "INVALID_PARAMETER_VALUE", str(e)))

    def ndjson(version: int, lines: list[dict]) -> Response:
        return Response("".join(json.dumps(x) + "\n" for x in lines), media_type="application/x-ndjson",
                        headers={"delta-table-version": str(version), CAPABILITIES: "responseformat=parquet"})

    @router.get("/shares")
    def shares(request: Request):
        """The share the bearer token stands for."""
        return answer(request, ds.list_shares)

    @router.get("/shares/{share}")
    def share(share: str, request: Request):
        return answer(request, ds.get_share, share)

    @router.get("/shares/{share}/schemas")
    def schemas(share: str, request: Request):
        return answer(request, ds.list_schemas, share)

    @router.get("/shares/{share}/schemas/{schema}/tables")
    def tables(share: str, schema: str, request: Request):
        return answer(request, ds.list_tables, share, schema)

    @router.get("/shares/{share}/all-tables")
    def all_tables(share: str, request: Request):
        return answer(request, ds.list_tables, share)

    @router.get("/shares/{share}/schemas/{schema}/tables/{table}/version")
    def version(share: str, schema: str, table: str, request: Request):
        try:
            row = ds.recipient(request.headers.get("authorization", ""))
            if request.query_params.get("startingTimestamp"):
                raise SharingError(400, "INVALID_PARAMETER_VALUE", "time travel is not offered")
            return Response(status_code=200, headers={"delta-table-version": str(ds.version(row, share, schema, table))})
        except SharingError as e:
            return fail(e)

    @router.get("/shares/{share}/schemas/{schema}/tables/{table}/metadata")
    def metadata(share: str, schema: str, table: str, request: Request):
        try:
            row = ds.recipient(request.headers.get("authorization", ""))
            return ndjson(*ds.metadata(row, share, schema, table))
        except SharingError as e:
            return fail(e)

    @router.post("/shares/{share}/schemas/{schema}/tables/{table}/query")
    def query(share: str, schema: str, table: str, request: Request, body: Optional[dict] = Body(None)):
        """The table's current files as pre-signed URLs (Delta Sharing's Parquet response format)."""
        try:
            row = ds.recipient(request.headers.get("authorization", ""))
            return ndjson(*ds.query(row, share, schema, table, body or {}))
        except SharingError as e:
            return fail(e)

    @router.get("/shares/{share}/schemas/{schema}/tables/{table}/changes")
    def changes(share: str, schema: str, table: str, request: Request):
        try:
            ds.recipient(request.headers.get("authorization", ""))
            raise SharingError(400, "INVALID_PARAMETER_VALUE", "change data feed is not offered")
        except SharingError as e:
            return fail(e)

    return router
