"""The metrics layer: a business measure defined once, and the same number wherever it is used.

A metric names a measure over one table: ``{"name": "revenue", "label": "Revenue", "connection": "lakehouse",
"table": "sales.orders", "measure": "Sum([amount])", "filters": [{"column": "status", "op": "=", "value":
"paid"}], "time_column": "ordered_at", "dimensions": ["region", "country"]}``. The measure is a workbook
aggregate formula; its own filters apply to that measure only (two metrics over the same table, with different
filters, are computed side by side in one query).

``POST /sql/metrics/query`` with ``{"metrics": [...], "dimensions": [...], "grain": "month", "filters": [...]}``
compiles to one workbook element (:mod:`cairn.sqlworkspace.workbooks`) and runs it as the person asking: their
policies, masks and row filters apply, so a metric means the same everywhere and shows each person only what they
may see. Dashboards show metric tiles (``{"metric": {...the query...}}``), and changing a definition changes every
tile, workbook and answer that uses it.

* Only the dimensions a metric lists (when it lists any) may break it down, and every metric in one query reads the
  same table and connection.
* Defined by anyone; changed or deleted by the owner (or an operator). **Certified** metrics (by an operator) are
  marked as the trusted definition. Each change bumps ``version`` and is audited (``metric.saved``,
  ``metric.certified``, ``metric.deleted``).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, insert, select, update

from cairn.common.db import metadata
from cairn.sqlworkspace.workbooks import MAX_ROWS, WorkbookError, _table

NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
GRAINS = ("day", "week", "month", "quarter", "year")
PERIOD = "period"

metrics = Table(
    "cairn_metrics", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(64), nullable=False, unique=True),
    Column("label", String(200), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("connection", String(128), nullable=False),
    Column("table_name", String(512), nullable=False),
    Column("measure", Text, nullable=False),
    Column("filters", Text, nullable=False, default="[]"),
    Column("time_column", String(128), nullable=True),
    Column("dimensions", Text, nullable=False, default="[]"),
    Column("owner", String(256), nullable=False, index=True),
    Column("certified", Boolean, nullable=False, default=False),
    Column("version", Integer, nullable=False, default=1),
    Column("updated_ms", BigInteger, nullable=False),
)


class MetricError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _row(r) -> dict:
    d = dict(r._mapping)
    return {**d, "table": d.pop("table_name"), "filters": json.loads(d["filters"]), "dimensions": json.loads(d["dimensions"]),
            "certified": bool(d["certified"])}


def element_for(defs: list[dict], query: dict) -> dict:
    """The workbook element a metric query compiles to (metrics already looked up, in order)."""
    if not defs:
        raise MetricError("metrics: at least one")
    tables = {(d["connection"], d["table"]) for d in defs}
    if len(tables) > 1:
        raise MetricError("metrics in one query read the same table and connection")
    dims = query.get("dimensions") or []
    if not isinstance(dims, list) or len(dims) > 6 or not all(isinstance(x, str) and x for x in dims):
        raise MetricError("dimensions: up to 6 column names")
    for d in defs:
        allowed = {x.lower() for x in d["dimensions"]}
        if allowed:
            off = [x for x in dims if x.lower() not in allowed]
            if off:
                raise MetricError(f"{d['name']} is not broken down by {off[0]} (it lists {', '.join(d['dimensions'])})")
    grain = query.get("grain")
    columns, group_by = [], list(dims)
    if grain is not None:
        if grain not in GRAINS:
            raise MetricError(f"grain: one of {', '.join(GRAINS)}")
        times = {d["time_column"] for d in defs}
        if None in times or len(times) != 1:
            raise MetricError("a grain needs metrics with the same time column")
        columns.append({"name": PERIOD, "formula": f'DateTrunc("{grain}", [{times.pop()}])'})
        group_by.append(PERIOD)
    filters = query.get("filters") or []
    if not isinstance(filters, list):
        raise MetricError("filters: a list")
    limit = query.get("limit", 1000)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
        raise MetricError(f"limit: 1 to {MAX_ROWS}")
    sort = query.get("sort") or ([{"column": PERIOD}] if grain else [{"column": c} for c in dims])
    return {"source": {"table": defs[0]["table"]}, "columns": columns,
            "filters": [{**f, "on": "rows"} if isinstance(f, dict) else f for f in filters],
            "group_by": group_by, "sort": sort, "limit": limit,
            "aggregates": [{"name": d["name"], "formula": d["measure"], "where": d["filters"]} for d in defs]}


class Metrics:
    def __init__(self, engine, workbooks, audit=None, is_operator: Optional[Callable[[str], bool]] = None):
        """``workbooks``: compiles and runs elements as a person; ``is_operator(actor)``: may certify and change
        anyone's metric."""
        self.engine, self.wb, self.audit = engine, workbooks, audit
        self.is_operator = is_operator or (lambda actor: False)

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [_row(r) for r in conn.execute(select(metrics).order_by(metrics.c.name))]

    def get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(metrics).where(metrics.c.name == name)).first()
        if r is None:
            raise MetricError(f"no metric {name}", 404)
        return _row(r)

    def save(self, actor: str, body: dict, name: Optional[str] = None) -> dict:
        if not isinstance(body, dict):
            raise MetricError("a metric is an object")
        new = name is None
        name = str(body.get("name") or "") if new else name
        if not NAME.match(name or ""):
            raise MetricError("name: lowercase letters, digits and _ (it is how the metric is referred to)")
        before = None if new else self.get(name)
        if before is not None and before["owner"] != actor and not self.is_operator(actor):
            raise MetricError("only its owner or an operator changes a metric", 403)
        d = {"name": name, "label": str(body.get("label") or name)[:200], "description": str(body.get("description") or ""),
             "connection": str(body.get("connection") or ""), "table": str(body.get("table") or ""),
             "measure": str(body.get("measure") or ""), "filters": body.get("filters") or [],
             "time_column": body.get("time_column") or None, "dimensions": body.get("dimensions") or []}
        _table(d["table"])
        if not isinstance(d["filters"], list) or not isinstance(d["dimensions"], list) or \
                not all(isinstance(x, str) and x for x in d["dimensions"]):
            raise MetricError("filters and dimensions are lists")
        # Checked by compiling it, as its owner, over the table as they see it: the measure is an aggregate, the
        # filters, the time column and the dimensions name real columns.
        probe = element_for([d], {"dimensions": d["dimensions"][:6], "grain": "day" if d["time_column"] else None,
                                  "limit": 1})
        try:
            self.wb.compile(actor, d["connection"], probe)
        except WorkbookError as e:
            raise MetricError(f"{name}: {e}", e.status)
        values = {"label": d["label"], "description": d["description"], "connection": d["connection"],
                  "table_name": d["table"], "measure": d["measure"], "filters": json.dumps(d["filters"]),
                  "time_column": d["time_column"], "dimensions": json.dumps(d["dimensions"]),
                  "updated_ms": int(time.time() * 1000)}
        with self.engine.begin() as conn:
            if new:
                if conn.execute(select(metrics.c.id).where(metrics.c.name == name)).first():
                    raise MetricError(f"a metric {name} exists", 409)
                conn.execute(insert(metrics).values(id=uuid.uuid4().hex, name=name, owner=actor, certified=False,
                                                    version=1, **values))
            else:
                # A changed definition is no longer the certified one until an operator certifies it again.
                conn.execute(update(metrics).where(metrics.c.name == name).values(
                    version=metrics.c.version + 1, certified=False, **values))
        self._audit(actor, "metric.saved", name, {"measure": d["measure"], "table": d["table"]})
        return self.get(name)

    def certify(self, actor: str, name: str, certified: bool) -> dict:
        if not self.is_operator(actor):
            raise MetricError("certifying a metric needs MANAGE_SYSTEM_OPERATIONS", 403)
        self.get(name)
        with self.engine.begin() as conn:
            conn.execute(update(metrics).where(metrics.c.name == name).values(certified=bool(certified)))
        self._audit(actor, "metric.certified", name, {"certified": bool(certified)})
        return self.get(name)

    def delete(self, actor: str, name: str) -> None:
        m = self.get(name)
        if m["owner"] != actor and not self.is_operator(actor):
            raise MetricError("only its owner or an operator deletes a metric", 403)
        with self.engine.begin() as conn:
            conn.execute(delete(metrics).where(metrics.c.name == name))
        self._audit(actor, "metric.deleted", name, {})

    def compile(self, actor: str, query: dict) -> tuple[dict, list[dict]]:
        if not isinstance(query, dict):
            raise MetricError("a metric query is an object")
        names = query.get("metrics")
        if not isinstance(names, list) or not 1 <= len(names) <= 12 or not all(isinstance(n, str) for n in names):
            raise MetricError("metrics: 1 to 12 metric names")
        defs = [self.get(n) for n in names]
        return element_for(defs, query), defs

    def query(self, actor: str, query: dict) -> dict:
        """The metrics broken down by the dimensions (and the grain), run as ``actor``."""
        element, defs = self.compile(actor, query)
        try:
            out = self.wb.run(actor, defs[0]["connection"], element)
        except WorkbookError as e:
            raise MetricError(str(e), e.status)
        return {**out, "metrics": [{k: d[k] for k in ("name", "label", "version", "certified")} for d in defs]}

    def _audit(self, actor: str, action: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("sql", action, actor, f"metric:{name}", details)


def build_metrics_router(m: Metrics, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    from cairn.sqlworkspace.workspace import WorkspaceError

    router = APIRouter(prefix="/sql/metrics", tags=["sql"])

    def person(p) -> str:
        if p.is_service:
            raise HTTPException(403, "metrics are defined and queried by people")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except (MetricError, WorkbookError, WorkspaceError) as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        person(p)
        return await call(m.list)

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: m.save(actor, body))

    @router.post("/query")
    async def query(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: m.query(actor, body))

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        person(p)
        return await call(lambda: m.get(name))

    @router.put("/{name}")
    async def change(name: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: m.save(actor, body, name))

    @router.post("/{name}/certify")
    async def certify(name: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: m.certify(actor, name, bool(body.get("certified", True))))

    @router.delete("/{name}", status_code=204)
    async def delete_(name: str, p=Depends(current_principal)):
        actor = person(p)
        await call(lambda: m.delete(actor, name))

    return router
