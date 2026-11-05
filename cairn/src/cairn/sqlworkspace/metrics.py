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
* **Over SQL, for outside BI tools**: ``POST /sql/metrics/<name>/publish`` makes the metric a Trino view
  (``metrics.<name>``, ``SECURITY INVOKER``) of its measure per dimension and day, so Tableau or Power BI, signed in
  to Trino as the person, get that person's number: their row filters and masks apply when they query it, and
  summing it up gives the same number as a dashboard. Only measures that can be summed up again (``Sum``, ``Count``,
  ``SumIf``, ``CountIf``) are published; the view follows each change of the definition and goes with the metric.
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
from cairn.sqlworkspace.formulas import Compiler, FormulaError, ident
from cairn.sqlworkspace.workbooks import MAX_ROWS, WorkbookError, _table, filter_sql

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
    # Published as a SQL view for outside BI tools (Tableau, Power BI): <CAIRN_METRIC_VIEW_SCHEMA>.<name>.
    Column("published", Boolean, nullable=False, default=False, server_default="0"),
)


class MetricError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _row(r) -> dict:
    d = dict(r._mapping)
    return {**d, "table": d.pop("table_name"), "filters": json.loads(d["filters"]), "dimensions": json.loads(d["dimensions"]),
            "certified": bool(d["certified"]), "published": bool(d.get("published"))}


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


def _columns_in(formula: str) -> set[str]:
    from cairn.sqlworkspace.formulas import Node, Parser

    out: set[str] = set()

    def walk(n: Node) -> None:
        if n.kind == "col":
            out.add(str(n.value))
        for a in n.args:
            walk(a)
    walk(Parser(formula).parse())
    return out


def sql_expression(m: dict) -> str:
    """The metric as one SQL aggregate over its table's columns (its filters inside it), for text-to-SQL. The
    definition was checked against the table when it was saved."""
    try:
        cond = " AND ".join(filter_sql(f, lambda c: ident(str(c))) for f in m["filters"])
        cols = _columns_in(m["measure"])
        out = Compiler({c: (f"CASE WHEN {cond} THEN {ident(c)} END" if cond else ident(c)) for c in cols}).compile(m["measure"])
    except (FormulaError, WorkbookError) as e:
        raise MetricError(f"{m['name']}: {e}")
    return out.sql.replace("count(*)", f"count_if({cond})") if cond else out.sql


# Measures that stay right when a BI tool sums the per-dimension-and-day rows up again.
ADDITIVE = re.compile(r"^\s*(Sum|Count|SumIf|CountIf)\s*\((?:[^()]|\([^()]*\))*\)\s*$", re.I)


class Metrics:
    def __init__(self, engine, workbooks, audit=None, is_operator: Optional[Callable[[str], bool]] = None,
                 ddl: Optional[Callable[[str, str, str], Any]] = None):
        """``workbooks``: compiles and runs elements as a person; ``is_operator(actor)``: may certify and change
        anyone's metric; ``ddl(actor, connection, sql)``: runs a statement on the connection's Trino as ``actor``
        (publishing metrics as views)."""
        self.engine, self.wb, self.audit = engine, workbooks, audit
        self.is_operator = is_operator or (lambda actor: False)
        self.ddl = ddl
        import os

        self.view_schema = os.environ.get("CAIRN_METRIC_VIEW_SCHEMA", "metrics")
        if not NAME.match(self.view_schema):
            raise ValueError("CAIRN_METRIC_VIEW_SCHEMA: lowercase letters, digits and _")

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [_row(r) for r in conn.execute(select(metrics).order_by(metrics.c.name))]

    def get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(metrics).where(metrics.c.name == name)).first()
        if r is None:
            raise MetricError(f"no metric {name}", 404)
        return _row(r)

    def for_tables(self, refs: set[str]) -> list[dict]:
        """Ask-your-data definitions for the metrics over these tables (``schema.table``, any case)."""
        want = {r.lower() for r in refs}
        out = []
        for m in self.list():
            if m["table"].lower() in want or m["table"].lower().split(".", 1)[-1] in want:
                try:
                    out.append({"name": m["name"], "kind": "metric", "version": m["version"],
                                "doc": {"expression": sql_expression(m), "table": m["table"],
                                        "description": (m["description"] or m["label"]) +
                                        (" (certified)" if m["certified"] else "")}})
                except MetricError:
                    continue
        return out

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
        if before is not None and before.get("published"):
            # The view follows the definition; one that can no longer be a view is taken away, not left stale.
            try:
                self.publish(actor, name)
            except MetricError:
                self.unpublish(actor, name)
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
        if m.get("published"):
            self.unpublish(actor, name, m)                     # the view goes with the metric
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

    # ---- over SQL ---------------------------------------------------------------------------------
    def view_sql(self, actor: str, m: dict) -> str:
        """The SELECT a metric's view runs: its measure per dimension (and per day, with a time column)."""
        if not ADDITIVE.match(m["measure"] or ""):
            raise MetricError(f"{m['name']}: only measures that sum up again (Sum, Count, SumIf, CountIf) are published "
                              "as views; an average or a distinct count per day cannot be summed by a BI tool")
        element = element_for([m], {"dimensions": m["dimensions"][:6], "grain": "day" if m["time_column"] else None,
                                    "limit": 1})
        element["sort"] = []
        try:
            sql = self.wb.compile(actor, m["connection"], element)["sql"]
        except WorkbookError as e:
            raise MetricError(f"{m['name']}: {e}", e.status)
        if not sql.endswith(" LIMIT 1"):
            raise MetricError("the metric's query did not compile to a plain grouping")
        return sql[: -len(" LIMIT 1")]

    def publish(self, actor: str, name: str) -> dict:
        m = self.get(name)
        if m["owner"] != actor and not self.is_operator(actor):
            raise MetricError("only its owner or an operator publishes a metric", 403)
        if self.ddl is None:
            raise MetricError("publishing metrics as views is not configured (needs Trino access)", 503)
        view = f"{self.view_schema}.{name}"
        # Run as the person publishing: Cairn's policies decide whether they may create views in that schema.
        self.ddl(actor, m["connection"], f"CREATE OR REPLACE VIEW {view} SECURITY INVOKER AS {self.view_sql(actor, m)}")
        with self.engine.begin() as conn:
            conn.execute(update(metrics).where(metrics.c.name == name).values(published=True))
        self._audit(actor, "metric.published", name, {"view": view})
        return {**self.get(name), "view": view}

    def unpublish(self, actor: str, name: str, m: Optional[dict] = None) -> None:
        m = m or self.get(name)
        if m["owner"] != actor and not self.is_operator(actor):
            raise MetricError("only its owner or an operator unpublishes a metric", 403)
        if self.ddl is not None:
            self.ddl(actor, m["connection"], f"DROP VIEW IF EXISTS {self.view_schema}.{name}")
        with self.engine.begin() as conn:
            conn.execute(update(metrics).where(metrics.c.name == name).values(published=False))
        self._audit(actor, "metric.unpublished", name, {})

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

    @router.post("/{name}/publish")
    async def publish(name: str, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: m.publish(actor, name))

    @router.delete("/{name}/publish", status_code=204)
    async def unpublish(name: str, p=Depends(current_principal)):
        actor = person(p)
        await call(lambda: m.unpublish(actor, name))

    @router.delete("/{name}", status_code=204)
    async def delete_(name: str, p=Depends(current_principal)):
        actor = person(p)
        await call(lambda: m.delete(actor, name))

    return router
