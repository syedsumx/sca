"""SaaS data connectors: Salesforce and HubSpot objects loaded into governed Iceberg tables, incrementally (L61).

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
KINDS = ("salesforce", "hubspot")

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
def salesforce_records(http: httpx.Client, secrets: dict[str, str], obj: str, fields: list[str],
                       cursor: Optional[str]) -> Iterator[dict]:
    """Records of ``obj`` modified at or after ``cursor`` (ISO time), oldest first: ``{id, modified, values}``."""
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
    base = (secrets.get("base_url") or "https://api.hubapi.com").rstrip("/")
    if not base.startswith("https://") and not os.environ.get("CAIRN_SAAS_ALLOW_HTTP"):
        raise SaasError("hubspot: base_url must be https://")
    token = secrets.get("access_token")
    if not token:
        raise SaasError("hubspot: the secret scope has no access_token", 400)
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


# --------------------------------------------------------------------------- the service
class SaasSyncs:
    def __init__(self, engine, catalog: dict, read_secret: Callable[[str, str, str], str],
                 may_write: Callable[[str, str], bool], urn: Optional[Callable[[str, str], str]] = None,
                 audit=None, changes=None, notify=None):
        """``read_secret(owner, scope, key)``: a secret, read as the owner. ``may_write(owner, urn)``: WRITE_DATASET.
        ``catalog``: pyiceberg REST catalog properties (``CAIRN_SAAS_CATALOG`` or ``CAIRN_PIPES_CATALOG``)."""
        self.engine, self.catalog_props, self.read_secret, self.may_write = engine, catalog, read_secret, may_write
        self.urn = urn or (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")
        self.audit, self.changes, self.notify = audit, changes, notify
        self.batch = int(os.environ.get("CAIRN_SAAS_BATCH_ROWS", "5000"))
        self.pool = ThreadPoolExecutor(max_workers=int(os.environ.get("CAIRN_SAAS_CONCURRENCY", "2")),
                                       thread_name_prefix="cairn-saas")
        self.timeout = float(os.environ.get("CAIRN_SAAS_HTTP_TIMEOUT", "60"))

    def _check(self, body: dict) -> dict:
        kind = body.get("kind")
        if kind not in KINDS:
            raise SaasError("kind: salesforce or hubspot")
        obj = str(body.get("object") or "")
        if not IDENT.match(obj):
            raise SaasError("object: an object's API name (letters, digits and _)")
        fields = body.get("fields")
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) and IDENT.match(f) for f in fields):
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
                "cron": cron, "enabled": bool(body.get("enabled", True))}

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
                if (cur["kind"], cur["object"], cur["target"]) != (v["kind"], v["object"], v["target"]):
                    changed["cursor"] = None             # another object or table: start from the beginning
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

        return RestCatalog("cairn-saas", **self.catalog_props)

    REQUIRED = {"salesforce": ("instance_url",), "hubspot": ("access_token",)}
    OPTIONAL = {"salesforce": ("client_id", "client_secret", "access_token"), "hubspot": ("base_url",)}

    def _secrets(self, s: dict) -> dict[str, str]:
        """The connector's keys, read as the owner. A required key (or the scope) the owner cannot read stops the
        run before any request is made."""
        out = {}
        for k in self.REQUIRED[s["kind"]]:
            try:
                out[k] = self.read_secret(s["owner"], s["secret_scope"], k)
            except Exception as e:  # noqa: BLE001 - no scope, no key, or no right: fail closed
                raise SaasError(f"cannot read {k} from secret scope {s['secret_scope']} as {s['owner']}: {e}", 403)
        for k in self.OPTIONAL[s["kind"]]:
            try:
                out[k] = self.read_secret(s["owner"], s["secret_scope"], k)
            except Exception as e:  # noqa: BLE001 - an absent optional key is fine; anything else is not
                if str(e) != "no such secret":
                    raise SaasError(f"cannot read {k} from secret scope {s['secret_scope']}: {e}", 403)
        return out

    def _run(self, s: dict) -> dict:
        rows, error, cursor = 0, None, s["cursor"]
        try:
            self._need_write(s["owner"], s["target"])        # the owner may since have lost the right
            secrets = self._secrets(s)
            fetch = salesforce_records if s["kind"] == "salesforce" else hubspot_records
            with httpx.Client(timeout=self.timeout) as http:
                batch: list[dict] = []
                for rec in fetch(http, secrets, s["object"], s["fields"], cursor):
                    batch.append(rec)
                    if len(batch) >= self.batch:
                        cursor = self._commit(s, batch)
                        rows += len(batch)
                        batch = []
                if batch:
                    cursor = self._commit(s, batch)
                    rows += len(batch)
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

    def _commit(self, s: dict, batch: list[dict]) -> str:
        """Upserts one batch by id, then advances the cursor (only once the table has committed)."""
        import pyarrow as pa
        from pyiceberg.exceptions import NoSuchTableError

        latest: dict[str, dict] = {}
        for rec in batch:                                  # a record twice in a batch: its latest version
            latest[rec["id"]] = rec
        recs = list(latest.values())
        now = _now()
        cols = {"id": pa.array([r["id"] for r in recs], pa.string())}
        for f in s["fields"]:
            cols[f.lower()] = pa.array([None if r["values"].get(f) is None else str(r["values"][f]) for r in recs],
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
        with table.transaction() as tx:
            tx.delete(In("id", [r["id"] for r in recs]))
            tx.append(rows)
        cursor = max(r["modified"] for r in recs)
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
