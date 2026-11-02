"""Derived tables: a SELECT kept as a lakehouse table, refreshed incrementally on Trino.

A derived table names a ``sql`` (one SELECT), the ``target`` (``schema.table`` in the lakehouse catalog)
and how it refreshes:

* ``full``: ``CREATE OR REPLACE TABLE … AS <sql>``, atomically;
* ``append``: only rows whose ``watermark`` column is above the table's current maximum are added;
* ``merge``: rows (those at or above the table's maximum ``watermark``, or all without one) replace the
  rows with the same ``key`` columns, others are inserted (``MERGE``).

The first run, and any run asked to be full, creates the table from the whole query. It refreshes every
``every_minutes``, and/or ``on_change``: when a table the query reads is recorded as changed (pipes,
streams, dbt, Spark, other derived tables), so chains refresh in order. A run is the owner's: it runs on
Trino as that Cairn user, so their access, masks and row filters apply, and Trino's lineage records it.
One run at a time per table (a lease in the database), each recorded with its outcome.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, or_, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

derived_tables = Table(
    "cairn_derived_tables", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(256), nullable=False, unique=True),
    Column("sql", Text, nullable=False),
    Column("target", String(512), nullable=False, unique=True),     # schema.table
    Column("mode", String(16), nullable=False),                      # full | append | merge
    Column("watermark", String(128), nullable=True),
    Column("key", String(1024), nullable=False, default=""),
    Column("every_minutes", Integer, nullable=True),
    Column("on_change", Boolean, nullable=False, default=False),
    Column("upstreams", Text, nullable=False, default="[]"),         # dataset URNs the query reads
    Column("owner", String(256), nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("seen_change", BigInteger, nullable=False, default=0),    # the change log position last refreshed from
    Column("last_run_ms", BigInteger, nullable=True),
    Column("lease_until", BigInteger, nullable=False, default=0),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)

derived_runs = Table(
    "cairn_derived_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("derived_id", String(32), nullable=False, index=True),
    Column("kind", String(16), nullable=False),                      # create | full | append | merge
    Column("status", String(16), nullable=False),                    # ok | error
    Column("trigger", String(16), nullable=False),                   # manual | schedule | change
    Column("started_ms", BigInteger, nullable=False, index=True),
    Column("finished_ms", BigInteger, nullable=False),
    Column("error", String(2000), nullable=True),
)

MODES = ("full", "append", "merge")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
LEASE_MS = 3_600_000


class DerivedError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _row(r) -> dict:
    return {"id": r.id, "name": r.name, "sql": r.sql, "target": r.target, "mode": r.mode, "watermark": r.watermark,
            "key": [k for k in r.key.split(",") if k], "every_minutes": r.every_minutes, "on_change": bool(r.on_change),
            "upstreams": json.loads(r.upstreams or "[]"), "owner": r.owner, "enabled": bool(r.enabled),
            "last_run_ms": r.last_run_ms, "created_by": r.created_by, "created_ms": r.created_ms}


def _q(name: str) -> str:
    return '"' + name + '"'


def tables_read(sql: str, catalog: str) -> list[tuple[str, str]]:
    """The query's one SELECT, checked, and the lakehouse tables it reads (``(schema, table)``)."""
    import sqlglot
    from sqlglot import exp

    try:
        statements = [s for s in sqlglot.parse(sql, read="trino") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise DerivedError(f"the query does not parse: {str(e).splitlines()[0]}")
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise DerivedError("the query is one SELECT")
    tree = statements[0]
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    out = []
    for t in tree.find_all(exp.Table):
        if not t.name or (not t.db and t.name.lower() in ctes):
            continue
        if (t.catalog or catalog).lower() == catalog.lower() and t.db:
            out.append((t.db.lower(), t.name.lower()))
    return sorted(set(out))


class DerivedTables:
    def __init__(self, engine, run: Callable[[str, str], list], catalog: str = "lakehouse", audit=None,
                 changes=None, urn: Optional[Callable[[str, str], str]] = None):
        """``run(owner, sql)``: runs one statement on Trino as that Cairn user and returns its rows."""
        self.engine, self.run, self.catalog, self.audit, self.changes = engine, run, catalog, audit, changes
        self.urn = urn or (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")

    # ---- definitions --------------------------------------------------------------------
    def create(self, body: dict, owner: str, actor: str) -> dict:
        name, sql, target = str(body.get("name") or "").strip(), str(body.get("sql") or "").strip().rstrip(";"), \
            str(body.get("target") or "")
        mode, wm = str(body.get("mode") or "full"), body.get("watermark") or None
        key = body.get("key") or []
        key = [k.strip() for k in key.split(",")] if isinstance(key, str) else [str(k) for k in key]
        parts = target.split(".")
        if not name or len(parts) != 2 or not all(NAME.match(p) for p in parts):
            raise DerivedError("a derived table needs a name and a target schema.table")
        if mode not in MODES:
            raise DerivedError(f"mode is one of {', '.join(MODES)}")
        if mode == "append" and not wm:
            raise DerivedError("append needs a watermark column (rows above the table's maximum are added)")
        if mode == "merge" and not key:
            raise DerivedError("merge needs the key columns")
        if (wm and not NAME.match(wm)) or not all(NAME.match(k) for k in key):
            raise DerivedError("watermark and key are column names")
        every = body.get("every_minutes")
        if every is not None and (not isinstance(every, int) or not 1 <= every <= 7 * 24 * 60):
            raise DerivedError("every_minutes is between 1 and 10080")
        on_change = bool(body.get("on_change"))
        if every is None and not on_change:
            raise DerivedError("refresh every_minutes, on_change, or both")
        read = tables_read(sql, self.catalog)
        if tuple(p.lower() for p in parts) in read:
            raise DerivedError("the query reads its own target")
        row = {"id": uuid.uuid4().hex, "name": name[:256], "sql": sql, "target": target, "mode": mode, "watermark": wm,
               "key": ",".join(key), "every_minutes": every, "on_change": on_change,
               "upstreams": json.dumps([self.urn(s, t) for s, t in read]), "owner": owner,
               "enabled": bool(body.get("enabled", True)), "seen_change": 0, "last_run_ms": None, "lease_until": 0,
               "created_by": actor, "created_ms": _now()}
        if self.changes is not None:
            row["seen_change"] = self.changes.head()
        with self.engine.begin() as conn:
            if conn.execute(select(derived_tables.c.id).where(or_(derived_tables.c.name == row["name"],
                                                                  derived_tables.c.target == target))).first():
                raise DerivedError("a derived table with that name or target exists", 409)
            conn.execute(derived_tables.insert().values(**row))
            if self.audit is not None:
                self.audit.record("lakehouse", "derived.created", actor, self.urn(*parts),
                                  {k: v for k, v in row.items() if k != "lease_until"}, conn=conn)
        return self.get(row["id"])

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [_row(r) for r in conn.execute(select(derived_tables).order_by(derived_tables.c.name))]

    def get(self, did: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(derived_tables).where(derived_tables.c.id == did)).first()
        if r is None:
            raise DerivedError("no such derived table", 404)
        return _row(r)

    def delete(self, did: str, actor: str) -> None:
        """Forgets the definition; the table itself stays (drop it in SQL if it should go)."""
        with self.engine.begin() as conn:
            if not conn.execute(delete(derived_tables).where(derived_tables.c.id == did)).rowcount:
                raise DerivedError("no such derived table", 404)
            conn.execute(delete(derived_runs).where(derived_runs.c.derived_id == did))
            if self.audit is not None:
                self.audit.record("lakehouse", "derived.deleted", actor, None, {"derived": did}, conn=conn)

    def runs(self, did: str, limit: int = 100) -> list[dict]:
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(derived_runs).where(derived_runs.c.derived_id == did)
                .order_by(derived_runs.c.started_ms.desc()).limit(limit))]

    # ---- refreshing ---------------------------------------------------------------------
    def _target(self, d: dict) -> str:
        schema, table = d["target"].split(".")
        return f"{_q(self.catalog)}.{_q(schema)}.{_q(table)}"

    def _exists(self, d: dict) -> bool:
        schema, table = d["target"].split(".")
        rows = self.run(d["owner"], f"SELECT count(*) FROM {_q(self.catalog)}.information_schema.tables "
                                    f"WHERE table_schema = '{schema.lower()}' AND table_name = '{table.lower()}'")
        return bool(rows and rows[0][0])

    def statements(self, d: dict, full: bool = False) -> tuple[str, list[str]]:
        """What a run executes: ``(kind, statements)``."""
        t, sql, wm = self._target(d), d["sql"], d["watermark"]
        if full or d["mode"] == "full" or not self._exists(d):
            schema = d["target"].split(".")[0]
            kind = "full" if (full or d["mode"] == "full") and self._exists(d) else "create"
            return kind, [f"CREATE SCHEMA IF NOT EXISTS {_q(self.catalog)}.{_q(schema)}",
                          f"CREATE OR REPLACE TABLE {t} AS {sql}"]
        if d["mode"] == "append":
            return "append", [f"INSERT INTO {t} SELECT * FROM ({sql}) AS src "
                              f"WHERE (SELECT max({_q(wm)}) FROM {t}) IS NULL OR src.{_q(wm)} > (SELECT max({_q(wm)}) FROM {t})"]
        schema, table = d["target"].split(".")
        cols = [r[0] for r in self.run(d["owner"], f"SELECT column_name FROM {_q(self.catalog)}.information_schema.columns "
                                                   f"WHERE table_schema = '{schema.lower()}' AND table_name = '{table.lower()}' "
                                                   "ORDER BY ordinal_position")]
        where = (f" WHERE (SELECT max({_q(wm)}) FROM {t}) IS NULL OR src.{_q(wm)} >= (SELECT max({_q(wm)}) FROM {t})"
                 if wm else "")
        on = " AND ".join(f"t.{_q(k)} = s.{_q(k)}" for k in d["key"])
        sets = ", ".join(f"{_q(c)} = s.{_q(c)}" for c in cols if c not in d["key"])
        return "merge", [f"MERGE INTO {t} AS t USING (SELECT * FROM ({sql}) AS src{where}) AS s ON {on} "
                         + (f"WHEN MATCHED THEN UPDATE SET {sets} " if sets else "")
                         + f"WHEN NOT MATCHED THEN INSERT ({', '.join(_q(c) for c in cols)}) "
                         f"VALUES ({', '.join('s.' + _q(c) for c in cols)})"]

    def refresh(self, did: str, full: bool = False, trigger: str = "manual") -> dict:
        """Runs one refresh now, unless one is already running (409)."""
        now = _now()
        head = self.changes.head() if self.changes is not None else 0
        with self.engine.begin() as conn:
            got = conn.execute(update(derived_tables).where(derived_tables.c.id == did, derived_tables.c.lease_until < now)
                               .values(lease_until=now + LEASE_MS)).rowcount
        if not got:
            self.get(did)                                  # 404 when it does not exist
            raise DerivedError("a refresh of this table is running", 409)
        d = self.get(did)
        kind, error = d["mode"], None
        try:
            kind, stmts = self.statements(d, full)
            for s in stmts:
                self.run(d["owner"], s)
        except Exception as e:  # noqa: BLE001 - recorded on the run
            error = (str(e).splitlines() or [type(e).__name__])[0][:2000]
            logger.warning("derived table %s: %s failed: %s", d["name"], kind, error)
        run = {"id": uuid.uuid4().hex, "derived_id": did, "kind": kind, "status": "error" if error else "ok",
               "trigger": trigger, "started_ms": now, "finished_ms": _now(), "error": error}
        with self.engine.begin() as conn:
            conn.execute(derived_runs.insert().values(**run))
            values: dict[str, Any] = {"lease_until": 0, "last_run_ms": now}
            if not error:
                values["seen_change"] = head
            conn.execute(update(derived_tables).where(derived_tables.c.id == did).values(**values))
        if not error and self.changes is not None:
            self.changes.record([self.urn(*d["target"].split("."))], "derived", f"derived:{d['name']}")
        return run

    def _seen(self, did: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(derived_tables.c.seen_change).where(derived_tables.c.id == did)).scalar() or 0)

    def due(self, d: dict, now: int) -> Optional[str]:
        if not d["enabled"]:
            return None
        if d["on_change"] and self.changes is not None and d["upstreams"] \
                and self.changes.since(d["upstreams"], self._seen(d["id"]), 1):
            return "change"
        if d["every_minutes"] and now - (d["last_run_ms"] or 0) >= d["every_minutes"] * 60_000:
            return "schedule"
        return None

    def ordered(self) -> list[dict]:
        """Definitions with each one after the derived tables it reads (cycles keep their name order)."""
        defs = self.list()
        by_target = {self.urn(*d["target"].split(".")): d["id"] for d in defs}
        deps = {d["id"]: {by_target[u] for u in d["upstreams"] if u in by_target} for d in defs}
        out, placed = [], set()
        while len(out) < len(defs):
            ready = [d for d in defs if d["id"] not in placed and deps[d["id"]] <= placed] or \
                [next(d for d in defs if d["id"] not in placed)]
            for d in ready:
                out.append(d)
                placed.add(d["id"])
        return out

    def run_due(self, now: Optional[int] = None) -> int:
        """Refreshes every derived table that is due (the scheduler's leader calls this), upstream ones
        first; a few passes, so a table whose upstream was just refreshed follows in the same tick."""
        ran = 0
        for _ in range(5):
            n = 0
            for d in self.ordered():
                why = self.due(d, now or _now())
                if why is None:
                    continue
                try:
                    self.refresh(d["id"], trigger=why)
                except DerivedError as e:
                    if e.status != 409:
                        raise
                    continue                       # another process is refreshing it
                n += 1
            ran += n
            if not n:
                break
            now = None
        return ran


def build_derived_router(dt: DerivedTables, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/derived", tags=["lakehouse"])

    def operator(p) -> bool:
        try:
            authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
            return True
        except HTTPException:
            return False

    def mine(p, did: str) -> dict:
        d = wrap(lambda: dt.get(did))
        if d["owner"] != p.urn and not operator(p):
            raise HTTPException(404, "no such derived table")
        return d

    def wrap(fn):
        try:
            return fn()
        except DerivedError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_derived(p=Depends(current_principal)):
        everything = operator(p)
        return [d for d in dt.list() if everything or d["owner"] == p.urn]

    @router.post("", status_code=201)
    def create(body: dict = Body(...), p=Depends(current_principal)):
        owner = str(body.get("owner") or p.urn)
        if owner != p.urn and not operator(p):
            raise HTTPException(403, "only operators create derived tables that run as someone else")
        return wrap(lambda: dt.create(body, owner, p.urn))

    @router.get("/{did}")
    def get(did: str, p=Depends(current_principal)):
        return mine(p, did)

    @router.delete("/{did}", status_code=204)
    def remove(did: str, p=Depends(current_principal)):
        mine(p, did)
        wrap(lambda: dt.delete(did, p.urn))

    @router.post("/{did}/refresh")
    async def refresh(did: str, body: Optional[dict] = Body(None), p=Depends(current_principal)):
        mine(p, did)
        full = bool((body or {}).get("full"))
        return await run_in_threadpool(lambda: wrap(lambda: dt.refresh(did, full)))

    @router.get("/{did}/runs")
    def runs(did: str, p=Depends(current_principal)):
        mine(p, did)
        return dt.runs(did)

    return router
