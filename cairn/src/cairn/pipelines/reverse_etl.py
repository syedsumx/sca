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
* **Data residency.** A source pinned to a region (``cairn.governance.residency``) leaves only for a ``region``
  its pin allows: give the sync the region the app keeps its data in. Checked at save and at every run.
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
from cairn.pipelines.saas import IDENT, NAME, SaasError, _next, connector_keys, hubspot_session, salesforce_session

logger = logging.getLogger(__name__)

PRIVILEGE = "EXPORT_DATASET"
KINDS = ("hubspot", "salesforce")      # the apps a sync writes to (the SaaS connectors read from more)
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
    Column("region", String(32), nullable=True),            # where the app keeps the data (residency)
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
def _wait(seconds: float) -> None:
    time.sleep(seconds)


def _call(http: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    """One request, waiting out the app's rate limits: a 429 or 503 is tried again after its ``Retry-After`` (or
    1, 2, 4... seconds), each wait at most ``CAIRN_SAAS_MAX_WAIT`` (60) seconds, at most ``CAIRN_SAAS_RETRIES`` (5)
    times; then the last answer is returned and the caller refuses it."""
    tries = max(0, int(os.environ.get("CAIRN_SAAS_RETRIES", "5")))
    longest = float(os.environ.get("CAIRN_SAAS_MAX_WAIT", "60"))
    for n in range(tries + 1):
        r = http.request(method, url, **kw)
        if r.status_code not in (429, 503) or n == tries:
            return r
        try:
            after = float(r.headers.get("Retry-After") or "")
        except ValueError:
            after = float(2 ** n)
        _wait(min(max(after, 0.0), longest))
    return r


def _refused(app: str, r: httpx.Response) -> SaasError:
    """A refused call, by its status and the app's error code only: the app's message often quotes the values it
    refused, and this error goes to the audit log and the server's log."""
    try:
        body = r.json()
        first = body[0] if isinstance(body, list) and body else body
        code = str(first.get("category") or first.get("errorCode") or "") if isinstance(first, dict) else ""
    except ValueError:
        code = ""
    code = code if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else ""
    return SaasError(f"{app}: upsert refused ({r.status_code}{', ' + code if code else ''})", 502)


Answered = Optional[Callable[[list[dict], list[Optional[str]]], None]]


def hubspot_upsert(http: httpx.Client, keys: dict, obj: str, key_field: str, rows: list[dict],
                   answered: Answered = None) -> list[Optional[str]]:
    """Upserts ``rows`` ({key, properties}); returns per row None (accepted) or the error. ``answered(part,
    results)`` is called after each call the app answered, so what it accepted is kept if a later call fails."""
    base, token = hubspot_session(keys)
    out: list[Optional[str]] = []
    for i in range(0, len(rows), 100):
        part = rows[i:i + 100]
        r = _call(http, "POST", f"{base}/crm/v3/objects/{obj}/batch/upsert",
                  headers={"Authorization": f"Bearer {token}"},
                  json={"inputs": [{"idProperty": key_field, "id": str(x["key"]), "properties": x["properties"]}
                                   for x in part]})
        if r.status_code in (200, 201):
            out += [None] * len(part)
        elif r.status_code == 207:                       # some failed: HubSpot lists the errors with the ids
            bad, untied = {}, None
            mine = {str(x["key"]).casefold() for x in part}
            for err in r.json().get("errors") or []:
                message = str(err.get("message") or err.get("category") or "rejected")[:300]
                ids = [str(i).casefold() for i in ((err.get("context") or {}).get("ids") or [])]
                if not ids or any(i not in mine for i in ids):
                    untied = untied or message           # not tied to this batch's records: none counts as accepted
                for ident in ids:
                    bad[ident] = message
            out += [untied or bad.get(str(x["key"]).casefold()) for x in part]
        else:
            raise _refused("hubspot", r)
        if answered is not None:
            answered(part, out[i:])
    return out


def salesforce_upsert(http: httpx.Client, keys: dict, obj: str, key_field: str, rows: list[dict],
                      answered: Answered = None) -> list[Optional[str]]:
    base, token = salesforce_session(http, keys)
    version = os.environ.get("CAIRN_SAAS_SALESFORCE_API", "v61.0")
    out: list[Optional[str]] = []
    for i in range(0, len(rows), 200):
        part = rows[i:i + 200]
        body = {"allOrNone": False, "records": [{"attributes": {"type": obj}, key_field: x["key"], **x["properties"]}
                                                for x in part]}
        r = _call(http, "PATCH", f"{base}/services/data/{version}/composite/sobjects/{obj}/{key_field}",
                  headers={"Authorization": f"Bearer {token}"}, json=body)
        if r.status_code != 200:
            raise _refused("salesforce", r)
        results = r.json()
        if not isinstance(results, list) or len(results) != len(part):   # one per record, in order
            raise SaasError(f"salesforce: {len(part)} records sent, but the answer has "
                            f"{len(results) if isinstance(results, list) else 'no'} results", 502)
        for res in results:
            out.append(None if res.get("success") else
                       "; ".join(str(e.get("message")) for e in res.get("errors") or [])[:300] or "rejected")
        if answered is not None:
            answered(part, out[i:])
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
        # may_leave(schema, region): may the source schema's data go to that region (data residency)? None: no rules.
        self.may_leave: Optional[Callable[[str, Optional[str]], bool]] = None

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
        from cairn.governance.residency import ResidencyError, valid_region

        try:
            region = valid_region(body.get("region"))
        except ResidencyError as e:
            raise SaasError(str(e))
        return {"kind": kind, "source": source, "object": obj, "key_column": key["column"], "key_field": key["field"],
                "mapping": dict(sorted(mapping.items())), "secret_scope": str(body["secret_scope"]), "cron": cron,
                "enabled": bool(body.get("enabled", True)), "region": region}

    def _need(self, owner: str, v: dict) -> None:
        urn = self._source_urn(v["source"])
        if not self.may_export(owner, urn):
            raise SaasError(f"{owner} may not send {v['source']} out of the platform ({PRIVILEGE})", 403)
        if self.may_leave is not None:
            region = v.get("region")
            try:
                ok = self.may_leave(v["source"].split(".")[0], region)
            except Exception as e:  # noqa: BLE001 - fail closed
                logger.warning("residency check failed for %s: %s", v["source"], e)
                ok = False
            if not ok:
                where = f"region {region}" if region else "an app with no region"
                raise SaasError(f"{v['source']} may not leave for {where}: data residency", 409)
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
                # Another app, object, key or account (the scope's keys name the account): every row is sent again.
                if (cur["kind"], cur["object"], cur["key_field"], cur["secret_scope"]) != (
                        v["kind"], v["object"], v["key_field"], v["secret_scope"]):
                    conn.execute(delete(reverse_sync_rows).where(reverse_sync_rows.c.sync_id == cur["id"]))
        self._audit(actor, "reverse.sync_saved", name, {"source": v["source"], "kind": v["kind"], "object": v["object"],
                                                        "fields": sorted(v["mapping"].values())})
        return self.get(actor, name)

    def get(self, actor: str, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(reverse_syncs).where(reverse_syncs.c.name == name)).first()
        if r is None or r.owner != actor:
            raise SaasError(f"no sync {name}", 404)
        return self._public(r)

    @staticmethod
    def _public(r) -> dict:
        d = dict(r._mapping)
        return {**d, "key": {"column": d["key_column"], "field": d["key_field"]}}   # as it is given

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [self._public(r) for r in conn.execute(select(reverse_syncs).where(reverse_syncs.c.owner == actor)
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
            seen: dict[str, int] = {}
            for row in data:
                key = row[idx[s["key_column"]]]
                if key is not None and key != "":
                    seen[str(key)] = seen.get(str(key), 0) + 1
            twice = {k: n for k, n in seen.items() if n > 1}
            for row in data:
                key = row[idx[s["key_column"]]]
                if key is None or key == "" or str(key) in twice:
                    continue                                  # no key, or one on several rows: which would win?
                props = {f: (None if row[idx[c]] is None else str(row[idx[c]])) for c, f in s["mapping"].items()}
                kh, rh = _hash(s["key_field"], key), _hash(props)
                if known.get(kh) != rh:
                    changed.append({"key": str(key), "properties": props, "kh": kh, "rh": rh})
            if twice:                                         # reported as failed, never their values
                failed = len(twice)
                errors.append(f"{len(twice)} key value(s) on several rows were not sent (one on {max(twice.values())} "
                              f"rows); the key column {s['key_column']} must be unique")
            push = {"hubspot": hubspot_upsert, "salesforce": salesforce_upsert}[s["kind"]]
            def answered(part: list[dict], results: list[Optional[str]]) -> None:
                nonlocal sent, failed
                ok = [c for c, r in zip(part, results) if r is None]
                bad = [r for r in results if r is not None]
                with self.engine.begin() as conn:             # remember only what the app accepted, call by call
                    for c in ok:
                        conn.execute(delete(reverse_sync_rows).where(and_(
                            reverse_sync_rows.c.sync_id == s["id"], reverse_sync_rows.c.key_hash == c["kh"])))
                        conn.execute(insert(reverse_sync_rows).values(sync_id=s["id"], key_hash=c["kh"], row_hash=c["rh"]))
                sent, failed = sent + len(ok), failed + len(bad)
                errors.extend(bad[:5 - len(errors)])
            if changed:
                with httpx.Client(timeout=self.timeout) as http:
                    push(http, keys, s["object"], s["key_field"], changed, answered)
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
