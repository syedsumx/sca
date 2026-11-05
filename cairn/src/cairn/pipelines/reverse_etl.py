"""Reverse ETL: governed lakehouse tables synced out to SaaS apps (HubSpot, Salesforce), as their owner (L62).

``POST /v1/reverse-syncs`` with ``{"name", "source": "marketing.segments", "kind": "hubspot" | "salesforce",
"object": "contacts" | "Contact", "key": {"column": "email", "field": "email"}, "mapping": {"column": "field",
...}, "secret_scope": "...", "cron": "..."}``.

* **As the owner.** Each run reads the source on Trino as the sync's owner, so their row filters decide which rows
  leave. Sending data out of the platform needs ``EXPORT_DATASET`` on the source, checked at save and at every
  run.
* **Masked columns are never sent.** Before reading, the owner's masking plan for the source is checked: if any
  mapped (or key) column is masked for them, the run is refused and nothing is sent. Masked values are not
  "safe to send": they would overwrite the app's real values with ``j•••@•••.com``.
* **Only changes.** A hash of each row's key and of its mapped values is kept (never the values), and a run sends
  only new and changed rows: HubSpot ``batch/upsert`` by the key property (100 per call), Salesforce composite
  upsert by the external id field (200 per call). Rows that disappear are not deleted in the app.
* **Per-record failures** are counted and the first few kept on the run; only records the app accepted are
  remembered as sent, so failed ones are tried again next run. A failed run is audited and told to the owner.
* **Keys** come from a secret scope read as the owner (``cairn.pipelines.saas.connector_keys``): the same fail-
  closed rules as the SaaS connectors.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, and_, delete, insert, select, update

from cairn.common.db import JSONText, metadata
from cairn.pipelines.saas import (IDENT, KINDS, NAME, SaasError, _next, connector_keys, hubspot_session,
                                  salesforce_session)

logger = logging.getLogger(__name__)

PRIVILEGE = "EXPORT_DATASET"
COLUMN = re.compile(r"^[a-z_][a-z0-9_]{0,127}$")
SOURCE = re.compile(r"^[a-z_][a-z0-9_]{0,63}\.[a-z_][a-z0-9_]{0,63}$")

reverse_syncs = Table(
    "cairn_reverse_syncs", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("source", String(130), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("object", String(80), nullable=False),
    Column("key_column", String(128), nullable=False),
    Column("key_field", String(80), nullable=False),
    Column("mapping", JSONText, nullable=False),
    Column("secret_scope", String(128), nullable=False),
    Column("cron", String(100), nullable=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("next_run_ms", BigInteger, nullable=True, index=True),
    Column("running", Boolean, nullable=False, default=False),
    Column("last_run_ms", BigInteger, nullable=True),
    Column("last_status", String(10), nullable=True),
    Column("last_sent", Integer, nullable=True),
    Column("last_failed", Integer, nullable=True),
    Column("last_error", Text, nullable=True),
    Column("created_ms", BigInteger, nullable=False),
)
reverse_sync_rows = Table(
    "cairn_reverse_sync_rows", metadata,
    Column("sync_id", String(32), primary_key=True),
    Column("key_hash", String(64), primary_key=True),
    Column("row_hash", String(64), nullable=False),
)


def _now() -> int:
    return int(time.time() * 1000)


def _hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- the two destinations
def hubspot_upsert(http: httpx.Client, keys: dict, obj: str, key_field: str, rows: list[dict]) -> list[Optional[str]]:
    """Upserts ``rows`` ({key, properties}); returns per row None (accepted) or the error."""
    base, token = hubspot_session(keys)
    out: list[Optional[str]] = []
    for i in range(0, len(rows), 100):
        part = rows[i:i + 100]
        r = http.post(f"{base}/crm/v3/objects/{obj}/batch/upsert", headers={"Authorization": f"Bearer {token}"},
                      json={"inputs": [{"idProperty": key_field, "id": str(x["key"]), "properties": x["properties"]}
                                       for x in part]})
        if r.status_code in (200, 201):
            out += [None] * len(part)
        elif r.status_code == 207:                       # some failed: HubSpot lists the errors with the ids
            bad = {}
            for err in r.json().get("errors") or []:
                for ident in ((err.get("context") or {}).get("ids") or []):
                    bad[str(ident)] = str(err.get("message") or err.get("category") or "rejected")[:300]
            out += [bad.get(str(x["key"])) for x in part]
        else:
            raise SaasError(f"hubspot: upsert refused ({r.status_code}): {r.text[:200]}", 502)
    return out


def salesforce_upsert(http: httpx.Client, keys: dict, obj: str, key_field: str, rows: list[dict]) -> list[Optional[str]]:
    base, token = salesforce_session(http, keys)
    version = os.environ.get("CAIRN_SAAS_SALESFORCE_API", "v61.0")
    out: list[Optional[str]] = []
    for i in range(0, len(rows), 200):
        part = rows[i:i + 200]
        body = {"allOrNone": False, "records": [{"attributes": {"type": obj}, key_field: x["key"], **x["properties"]}
                                                for x in part]}
        r = http.patch(f"{base}/services/data/{version}/composite/sobjects/{obj}/{key_field}",
                       headers={"Authorization": f"Bearer {token}"}, json=body)
        if r.status_code != 200:
            raise SaasError(f"salesforce: upsert refused ({r.status_code}): {r.text[:200]}", 502)
        for res in r.json():
            out.append(None if res.get("success") else
                       "; ".join(str(e.get("message")) for e in res.get("errors") or [])[:300] or "rejected")
    return out


# --------------------------------------------------------------------------- the service
class ReverseSyncs:
    def __init__(self, engine, run_sql: Callable[[str, str, int], tuple[list[str], list[list[Any]], bool]],
                 plan_for: Callable[[str, str], Any], may_export: Callable[[str, str], bool],
                 read_secret: Callable[[str, str, str], str], urn: Callable[[str, str], str], catalog: str = "lakehouse",
                 audit=None, notify=None):
        """``run_sql(owner, sql, limit)``: a query on Trino as the owner. ``plan_for(owner, urn)``: their masking
        plan. ``may_export(owner, urn)``: EXPORT_DATASET on the source."""
        self.engine, self.run_sql, self.plan_for, self.may_export = engine, run_sql, plan_for, may_export
        self.read_secret, self.urn, self.catalog, self.audit, self.notify = read_secret, urn, catalog, audit, notify
        self.max_rows = int(os.environ.get("CAIRN_REVERSE_ETL_MAX_ROWS", "100000"))
        self.timeout = float(os.environ.get("CAIRN_SAAS_HTTP_TIMEOUT", "60"))
        self.pool = ThreadPoolExecutor(max_workers=int(os.environ.get("CAIRN_REVERSE_ETL_CONCURRENCY", "2")),
                                       thread_name_prefix="cairn-reverse-etl")

    def _source_urn(self, source: str) -> str:
        return self.urn(*source.split("."))

    def _check(self, body: dict) -> dict:
        kind = body.get("kind")
        if kind not in KINDS:
            raise SaasError("kind: salesforce or hubspot")
        source = str(body.get("source") or "")
        if not SOURCE.match(source):
            raise SaasError("source: schema.table (lowercase)")
        obj = str(body.get("object") or "")
        if not IDENT.match(obj):
            raise SaasError("object: an object's API name")
        key = body.get("key") or {}
        if not (isinstance(key, dict) and COLUMN.match(str(key.get("column") or ""))
                and IDENT.match(str(key.get("field") or ""))):
            raise SaasError("key: {column: a source column, field: the app's unique property or external id field}")
        mapping = body.get("mapping")
        if not isinstance(mapping, dict) or not mapping or not all(
                COLUMN.match(str(c)) and IDENT.match(str(f)) for c, f in mapping.items()):
            raise SaasError("mapping: {source column: app field}")
        if not body.get("secret_scope"):
            raise SaasError("secret_scope: the scope that holds the app's keys")
        cron = body.get("cron") or None
        if cron is not None:
            from croniter import croniter

            if not isinstance(cron, str) or not croniter.is_valid(cron):
                raise SaasError("cron: a cron expression (UTC)")
        return {"kind": kind, "source": source, "object": obj, "key_column": key["column"], "key_field": key["field"],
                "mapping": dict(sorted(mapping.items())), "secret_scope": str(body["secret_scope"]), "cron": cron,
                "enabled": bool(body.get("enabled", True))}

    def _need(self, owner: str, v: dict) -> None:
        urn = self._source_urn(v["source"])
        if not self.may_export(owner, urn):
            raise SaasError(f"{owner} may not send {v['source']} out of the platform ({PRIVILEGE})", 403)
        plan = self.plan_for(owner, urn)
        cols = [v["key_column"], *v["mapping"]]
        masked = sorted(c for c in cols if plan.all_fields or plan.masks_field(c))
        if masked:            # (the plan's mask_queries is about query text shown in the catalog, not values)
            raise SaasError(f"{', '.join(masked)} is masked for {owner}: masked values are never sent", 403)

    def save(self, actor: str, body: dict, name: Optional[str] = None) -> dict:
        v = self._check(body)
        self._need(actor, v)
        now = _now()
        with self.engine.begin() as conn:
            if name is None:
                name = str(body.get("name") or "")
                if not NAME.match(name):
                    raise SaasError("name: lowercase letters, digits and -")
                if conn.execute(select(reverse_syncs.c.id).where(reverse_syncs.c.name == name)).first():
                    raise SaasError(f"a sync {name} exists", 409)
                conn.execute(insert(reverse_syncs).values(id=uuid.uuid4().hex, name=name, owner=actor, created_ms=now,
                                                          running=False, next_run_ms=_next(v["cron"], now), **v))
            else:
                cur = self.get(actor, name)
                conn.execute(update(reverse_syncs).where(reverse_syncs.c.id == cur["id"])
                             .values(next_run_ms=_next(v["cron"], now), **v))
                if (cur["kind"], cur["object"], cur["key_field"]) != (v["kind"], v["object"], v["key_field"]):
                    conn.execute(delete(reverse_sync_rows).where(reverse_sync_rows.c.sync_id == cur["id"]))
        self._audit(actor, "reverse.sync_saved", name, {"source": v["source"], "kind": v["kind"], "object": v["object"],
                                                        "fields": sorted(v["mapping"].values())})
        return self.get(actor, name)

    def get(self, actor: str, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(reverse_syncs).where(reverse_syncs.c.name == name)).first()
        if r is None or r.owner != actor:
            raise SaasError(f"no sync {name}", 404)
        return dict(r._mapping)

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(reverse_syncs).where(reverse_syncs.c.owner == actor)
                                                           .order_by(reverse_syncs.c.name))]

    def delete(self, actor: str, name: str) -> None:
        s = self.get(actor, name)
        with self.engine.begin() as conn:
            conn.execute(delete(reverse_sync_rows).where(reverse_sync_rows.c.sync_id == s["id"]))
            conn.execute(delete(reverse_syncs).where(reverse_syncs.c.id == s["id"]))
        self._audit(actor, "reverse.sync_deleted", name, {})

    def _claim(self, s: dict, next_run_ms: Optional[int] = None) -> bool:
        cond = [reverse_syncs.c.id == s["id"], reverse_syncs.c.running.is_(False)]
        values: dict[str, Any] = {"running": True}
        if next_run_ms is not None:
            cond.append(reverse_syncs.c.next_run_ms == s["next_run_ms"])
            values["next_run_ms"] = next_run_ms
        with self.engine.begin() as conn:
            return bool(conn.execute(update(reverse_syncs).where(and_(*cond)).values(**values)).rowcount)

    def run_now(self, actor: str, name: str, wait: bool = False) -> dict:
        s = self.get(actor, name)
        if not self._claim(s):
            raise SaasError(f"{name} is running", 409)
        fut = self.pool.submit(self._run, s)
        return fut.result() if wait else {"sync": name, "status": "running"}

    def run_due(self, now: Optional[int] = None, wait: bool = False) -> int:
        now = now or _now()
        with self.engine.connect() as conn:
            due = [dict(r._mapping) for r in conn.execute(select(reverse_syncs).where(and_(
                reverse_syncs.c.enabled, reverse_syncs.c.next_run_ms.is_not(None), reverse_syncs.c.next_run_ms <= now)))]
        futures = [self.pool.submit(self._run, s) for s in due if self._claim(s, _next(s["cron"], now))]
        if wait:
            for f in futures:
                f.result()
        return len(futures)

    def _run(self, s: dict) -> dict:
        sent, failed, errors, error = 0, 0, [], None
        try:
            self._need(s["owner"], s)                          # rights and masks again, at every run
            keys = connector_keys(self.read_secret, s["owner"], s["secret_scope"], s["kind"])
            cols = [s["key_column"], *[c for c in s["mapping"] if c != s["key_column"]]]
            ns, table = s["source"].split(".")
            sql = (f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} '
                   f'FROM "{self.catalog}"."{ns}"."{table}"')
            names, data, truncated = self.run_sql(s["owner"], sql, self.max_rows)
            if truncated:
                raise SaasError(f"{s['source']} has over {self.max_rows} rows for one sync (CAIRN_REVERSE_ETL_MAX_ROWS)")
            idx = {n.lower(): i for i, n in enumerate(names)}
            with self.engine.connect() as conn:
                known = {r.key_hash: r.row_hash for r in conn.execute(
                    select(reverse_sync_rows).where(reverse_sync_rows.c.sync_id == s["id"]))}
            changed: list[dict] = []
            for row in data:
                key = row[idx[s["key_column"]]]
                if key is None or key == "":
                    continue                                  # no key: nothing to match it to in the app
                props = {f: (None if row[idx[c]] is None else str(row[idx[c]])) for c, f in s["mapping"].items()}
                kh, rh = _hash(s["key_field"], key), _hash(props)
                if known.get(kh) != rh:
                    changed.append({"key": str(key), "properties": props, "kh": kh, "rh": rh})
            push = hubspot_upsert if s["kind"] == "hubspot" else salesforce_upsert
            if changed:
                with httpx.Client(timeout=self.timeout) as http:
                    results = push(http, keys, s["object"], s["key_field"], changed)
                ok = [c for c, r in zip(changed, results) if r is None]
                bad = [(c, r) for c, r in zip(changed, results) if r is not None]
                with self.engine.begin() as conn:             # remember only what the app accepted
                    for c in ok:
                        conn.execute(delete(reverse_sync_rows).where(and_(
                            reverse_sync_rows.c.sync_id == s["id"], reverse_sync_rows.c.key_hash == c["kh"])))
                        conn.execute(insert(reverse_sync_rows).values(sync_id=s["id"], key_hash=c["kh"], row_hash=c["rh"]))
                sent, failed = len(ok), len(bad)
                errors = [r for _, r in bad[:5]]
        except Exception as e:  # noqa: BLE001 - recorded on the sync and told
            error = str(e)[:1000]
            logger.warning("reverse sync %s failed: %s", s["name"], error)
        status = "failed" if error else ("partial" if failed else "ok")
        message = error or ("; ".join(errors) if errors else None)
        with self.engine.begin() as conn:
            conn.execute(update(reverse_syncs).where(reverse_syncs.c.id == s["id"]).values(
                running=False, last_run_ms=_now(), last_status=status, last_sent=sent, last_failed=failed,
                last_error=message))
        self._audit(s["owner"], "reverse.sync_failed" if error else "reverse.sync_run", s["name"],
                    {"sent": sent, "failed": failed, "object": s["object"], **({"error": error} if error else {})})
        if (error or failed) and self.notify is not None:
            try:
                self.notify("saas.sync_failed", f"Reverse sync {s['name']} " + ("failed" if error else f"rejected {failed}"),
                            message or "", [s["owner"]], {"sync": s["name"]})
            except Exception:  # noqa: BLE001 - recorded either way
                logger.exception("could not tell about reverse sync %s", s["name"])
        return {"sync": s["name"], "status": status, "sent": sent, "failed": failed, "error": message}

    def _audit(self, actor: str, action: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("pipelines", action, actor, f"reverse-sync:{name}", details)


def build_reverse_router(rs: ReverseSyncs, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/reverse-syncs", tags=["reverse-etl"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except SaasError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        return await call(lambda: rs.list(p.urn))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: rs.save(p.urn, body))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: rs.get(p.urn, name))

    @router.put("/{name}")
    async def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: rs.save(p.urn, body, name))

    @router.delete("/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        await call(lambda: rs.delete(p.urn, name))

    @router.post("/{name}/run", status_code=202)
    async def run(name: str, wait: bool = False, p=Depends(current_principal)):
        return await call(lambda: rs.run_now(p.urn, name, wait))

    return router
