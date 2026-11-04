"""Workbooks: spreadsheet-style exploration of a table, compiled to SQL and run as whoever looks at it.

A workbook element starts from a table the person may query and adds, without writing SQL:

* **calculated columns**: ``{"name": "Net", "formula": "[Amount] * (1 - [Discount])"}`` (spreadsheet formulas,
  :mod:`cairn.sqlworkspace.formulas`); they may use one another;
* **filters**: ``{"column": "Region", "op": "in", "values": ["EU", "US"]}`` (``= != > >= < <= in not_in
  contains starts_with between is_null not_null``), on rows, or with ``"on": "groups"`` on the grouped values;
* **grouping**: ``group_by`` columns and ``aggregates`` (``{"name": "Revenue", "formula": "Sum([Net])"}``, optionally
  with ``"where": [filters]`` for an aggregate over only those rows, as metrics use);
* **a pivot**: ``{"column": "Quarter", "values": [...], "aggregate": "Sum([Net])"}`` (the values found in the
  data, up to 24, when none are given): one column per value;
* **sort** and **limit** (at most 10,000 rows).

The element is compiled to one SQL query on Trino (``POST /sql/workbooks/compile`` shows it) and run through
the SQL workspace as the person looking (``POST /sql/workbooks/run``): their policies, masks and row filters
apply as to any query they write, and it is in their history. Workbooks are saved with a name and shared or
not (``/sql/workbooks``), like saved queries; a shared workbook still shows each viewer only what they may see.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Optional

from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, delete, insert, or_, select, update

from cairn.common.db import metadata
from cairn.sqlworkspace.formulas import Compiler, FormulaError, ident, literal

MAX_ROWS = 10_000
MAX_PIVOT = 24
TABLE = re.compile(r'^(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)(?:\.(?:"[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)){0,2}$')
FILTER_OPS = {"=", "!=", ">", ">=", "<", "<=", "in", "not_in", "contains", "starts_with", "between", "is_null", "not_null"}

workbooks = Table(
    "cairn_workbooks", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("name", String(200), nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("connection", String(128), nullable=False),
    Column("spec", Text, nullable=False),
    Column("shared", Boolean, nullable=False, default=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)


class WorkbookError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _table(name: str) -> str:
    if not isinstance(name, str) or not TABLE.match(name.strip()):
        raise WorkbookError("source.table: catalog.schema.table")
    return ".".join(p if p.startswith('"') else ident(p) for p in name.strip().split("."))


def _value(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return literal(v)
    raise WorkbookError("filter values are text, numbers, true/false or null")


def _name(n: Any, what: str) -> str:
    if not isinstance(n, str) or not n.strip() or len(n) > 128 or '"' in n:
        raise WorkbookError(f"{what}: a name (up to 128 characters, no double quotes)")
    return n.strip()


def filter_sql(f: dict, resolve) -> str:
    """One filter as a SQL condition; ``resolve(column)`` gives the column's SQL (and refuses unknown ones)."""
    if not isinstance(f, dict):
        raise WorkbookError("a filter is an object")
    op = f.get("op")
    if op not in FILTER_OPS:
        raise WorkbookError(f"filter op: one of {', '.join(sorted(FILTER_OPS))}")
    x = resolve(f.get("column"))
    if op == "is_null":
        return f"{x} IS NULL"
    if op == "not_null":
        return f"{x} IS NOT NULL"
    if op in ("in", "not_in"):
        vals = f.get("values")
        if not isinstance(vals, list) or not vals or len(vals) > 1000:
            raise WorkbookError("filter values: a list (1 to 1,000)")
        return f"{x} {'NOT IN' if op == 'not_in' else 'IN'} ({', '.join(_value(v) for v in vals)})"
    if op == "between":
        vals = f.get("values")
        if not isinstance(vals, list) or len(vals) != 2:
            raise WorkbookError("between: values [low, high]")
        return f"{x} BETWEEN {_value(vals[0])} AND {_value(vals[1])}"
    if op == "contains":
        return f"strpos(lower(CAST({x} AS varchar)), lower({_value(str(f.get('value', '')))})) > 0"
    if op == "starts_with":
        return f"starts_with(CAST({x} AS varchar), {_value(str(f.get('value', '')))})"
    return f"{x} {'<>' if op == '!=' else op} {_value(f.get('value'))}"


def compile_element(spec: dict, source_columns: list[str], pivot_values: Optional[list] = None) -> str:
    """The SQL for a workbook element over a table with ``source_columns``."""
    if not isinstance(spec, dict):
        raise WorkbookError("an element is an object")
    table = _table((spec.get("source") or {}).get("table", ""))
    cols: dict[str, str] = {c: ident(c) for c in source_columns}
    # Calculated columns, in order: each may use the ones before it.
    calc: list[tuple[str, str]] = []
    for c in spec.get("columns") or []:
        name = _name((c or {}).get("name"), "columns.name")
        if name.lower() in {k.lower() for k in cols}:
            raise WorkbookError(f"a column named {name} exists")
        try:
            out = Compiler(cols).compile(str(c.get("formula") or ""))
        except FormulaError as e:
            raise WorkbookError(f"{name}: {e}")
        if out.aggregate:
            raise WorkbookError(f"{name}: a calculated column works row by row; put Sum() and the like in aggregates")
        cols[name] = f"({out.sql})"
        calc.append((name, out.sql))

    def expr(column: str) -> str:
        hit = next((v for k, v in cols.items() if k.lower() == str(column).lower()), None)
        if hit is None:
            raise WorkbookError(f"no column {column}")
        return hit

    def condition(f: dict, of: Optional[Any] = None) -> str:
        return filter_sql(f, of or expr)

    filters = spec.get("filters") or []
    if not isinstance(filters, list):
        raise WorkbookError("filters: a list")
    where = [condition(f) for f in filters if (f or {}).get("on", "rows") == "rows"]
    group_by = [str(g) for g in spec.get("group_by") or []]
    aggregates = spec.get("aggregates") or []
    pivot = spec.get("pivot")
    limit = spec.get("limit", 1000)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_ROWS:
        raise WorkbookError(f"limit: 1 to {MAX_ROWS}")
    select_rows = ", ".join([f"{ident(c)}" for c in source_columns] + [f"({s}) AS {ident(n)}" for n, s in calc])
    base = f"SELECT {select_rows} FROM {table}" + (f" WHERE {' AND '.join(where)}" if where else "")

    sort = spec.get("sort") or []
    if not group_by and not aggregates and not pivot:
        if any((f or {}).get("on") == "groups" for f in filters):
            raise WorkbookError("a filter on groups needs grouping")
        order = _order(sort, lambda c: expr(c))
        return f"{base}{order} LIMIT {limit}"

    # Grouped: the grouping columns, the aggregates (and the pivot's), over the filtered rows.
    inner = Compiler({k: ident(k) for k in cols})            # in the outer query, columns are the base's output
    keys = [ident(next((k for k in cols if k.lower() == g.lower()), None) or _missing(g)) for g in group_by]
    out_cols, agg_exprs = [], {}
    for a in aggregates:
        name = _name((a or {}).get("name"), "aggregates.name")
        where = a.get("where") or []
        if not isinstance(where, list) or len(where) > 20:
            raise WorkbookError(f"{name}: where is a list of filters")
        cond = " AND ".join(filter_sql(f, lambda col: ident(next((k for k in cols if k.lower() == str(col).lower()), None)
                                                            or _missing(col))) for f in where)
        try:
            if cond:
                # A filtered aggregate: every column it reads is null outside the filter (and Count() counts the filter).
                only = Compiler({k: f"CASE WHEN {cond} THEN {ident(k)} END" for k in cols})
                c = only.compile(str(a.get("formula") or ""))
                c = type(c)(c.sql.replace("count(*)", f"count_if({cond})"), c.columns, c.aggregate)
            else:
                c = inner.compile(str(a.get("formula") or ""))
        except FormulaError as e:
            raise WorkbookError(f"{name}: {e}")
        if not c.aggregate:
            raise WorkbookError(f"{name}: an aggregate uses Sum(), Count(), Avg() or another aggregate")
        stray = {u for u in _outside_aggregates(str(a.get("formula"))) if u.lower() not in {g.lower() for g in group_by}}
        if stray:
            raise WorkbookError(f"{name}: [{sorted(stray)[0]}] is neither grouped nor inside an aggregate")
        out_cols.append(f"{c.sql} AS {ident(name)}")
        agg_exprs[name.lower()] = c.sql
    if pivot:
        pcol = next((k for k in cols if k.lower() == str(pivot.get("column", "")).lower()), None) or _missing(pivot.get("column"))
        try:
            pc = inner.compile(str(pivot.get("aggregate") or ""))
        except FormulaError as e:
            raise WorkbookError(f"pivot: {e}")
        if not pc.aggregate:
            raise WorkbookError("pivot.aggregate: an aggregate, such as Sum([Amount])")
        if _outside_aggregates(str(pivot.get("aggregate"))):
            raise WorkbookError("pivot.aggregate: every column inside an aggregate")
        values = pivot_values if pivot_values is not None else pivot.get("values")
        if not isinstance(values, list) or not values or len(values) > MAX_PIVOT:
            raise WorkbookError(f"pivot.values: 1 to {MAX_PIVOT} values")
        for v in values:
            label = _name(str(v) if v is not None else "(empty)", "pivot value")
            cond = f"{ident(pcol)} IS NULL" if v is None else f"{ident(pcol)} = {_value(v)}"
            # The aggregate over only this value's rows: every column it reads is null elsewhere.
            only = Compiler({k: f"CASE WHEN {cond} THEN {ident(k)} END" for k in cols})
            sql_v = only.compile(str(pivot.get("aggregate"))).sql.replace("count(*)", f"count_if({cond})")
            out_cols.append(f"{sql_v} AS {ident(label)}")
    having = []
    for f in filters:
        if (f or {}).get("on") == "groups":
            having.append(condition(f, of=lambda c: agg_exprs.get(str(c).lower()) or _keyed(c, group_by)))
    if not out_cols and not keys:
        raise WorkbookError("grouping needs group_by or aggregates")
    sel = ", ".join(keys + out_cols)
    sql = f"SELECT {sel} FROM ({base}) AS rows"
    if keys:
        sql += f" GROUP BY {', '.join(keys)}"
    if having:
        sql += f" HAVING {' AND '.join(having)}"
    names = {g.lower(): ident(next(k for k in cols if k.lower() == g.lower())) for g in group_by}
    names.update({k: ident(next(a['name'] for a in aggregates if a['name'].lower() == k)) for k in agg_exprs})
    order = _order(sort, lambda c: names.get(str(c).lower()) or _missing(c))
    return f"{sql}{order} LIMIT {limit}"


def _missing(c: Any) -> str:
    raise WorkbookError(f"no column {c}")


def _keyed(c: Any, group_by: list[str]) -> str:
    g = next((x for x in group_by if x.lower() == str(c).lower()), None)
    if g is None:
        raise WorkbookError(f"a filter on groups is on a grouped column or an aggregate, not {c}")
    return ident(g)


def _outside_aggregates(formula: str) -> set[str]:
    """The columns a formula uses outside any aggregate (those must be grouped)."""
    from cairn.sqlworkspace.formulas import FUNCTIONS, Node, Parser

    out: set[str] = set()

    def walk(n: Node, inside: bool) -> None:
        if n.kind == "col" and not inside:
            out.add(str(n.value))
        agg = n.kind == "call" and FUNCTIONS.get(str(n.value)) is not None and FUNCTIONS[str(n.value)].aggregate
        for a in n.args:
            walk(a, inside or agg)
    walk(Parser(formula).parse(), False)
    return out


def _order(sort: list, resolve) -> str:
    if not isinstance(sort, list):
        raise WorkbookError("sort: a list of {column, desc}")
    parts = [f"{resolve(s.get('column'))} {'DESC' if s.get('desc') else 'ASC'}" for s in sort[:10]]
    return f" ORDER BY {', '.join(parts)}" if parts else ""


class Workbooks:
    def __init__(self, engine, workspace, columns_of):
        """``workspace``: the SqlWorkspace (runs SQL as a person); ``columns_of(actor, connection, table)``:
        the table's columns as the person may see them."""
        self.engine, self.workspace, self.columns_of = engine, workspace, columns_of

    def compile(self, actor: str, connection: str, spec: dict) -> dict:
        table = (spec.get("source") or {}).get("table", "")
        _table(table)
        cols = self.columns_of(actor, connection, table)
        pivot_values = None
        if spec.get("pivot") and not spec["pivot"].get("values"):
            pivot_values = self._pivot_values(actor, connection, spec, cols)
        return {"sql": compile_element(spec, cols, pivot_values), "columns": cols}

    def _pivot_values(self, actor: str, connection: str, spec: dict, cols: list[str]) -> list:
        """The pivot column's values in the data (as the person sees it), up to MAX_PIVOT."""
        probe = {k: v for k, v in spec.items() if k not in ("pivot", "aggregates", "sort", "limit")}
        probe["group_by"] = [spec["pivot"]["column"]]
        probe["aggregates"] = [{"name": "__n", "formula": "Count()"}]
        probe["sort"] = [{"column": spec["pivot"]["column"]}]
        probe["limit"] = MAX_PIVOT + 1
        sql = compile_element(probe, cols)
        out = self.workspace.run(actor, sql, connection, MAX_PIVOT + 1)
        values = [r[0] for r in out.get("rows") or []]
        if len(values) > MAX_PIVOT:
            raise WorkbookError(f"the pivot column has more than {MAX_PIVOT} values: filter it, or list the values")
        return values or [None]

    def run(self, actor: str, connection: str, spec: dict, cache: bool = True) -> dict:
        compiled = self.compile(actor, connection, spec)
        limit = min(int(spec.get("limit", 1000)), MAX_ROWS)
        out = self.workspace.run(actor, compiled["sql"], connection, limit, cache=cache)
        return {**out, "sql": compiled["sql"]}

    # ---- saved workbooks -----------------------------------------------------------------------
    def _row(self, r) -> dict:
        d = dict(r._mapping)
        return {**d, "spec": json.loads(d["spec"])}

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(workbooks).where(or_(workbooks.c.owner == actor, workbooks.c.shared.is_(True)))
                                .order_by(workbooks.c.updated_ms.desc())).all()
        return [self._row(r) for r in rows]

    def get(self, actor: str, wid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(workbooks).where(workbooks.c.id == wid)).first()
        if r is None or (r.owner != actor and not r.shared):
            raise WorkbookError("no such workbook", 404)
        return self._row(r)

    def save(self, actor: str, body: dict, wid: Optional[str] = None) -> dict:
        name = str(body.get("name") or "").strip()
        if not 1 <= len(name) <= 200:
            raise WorkbookError("name: up to 200 characters")
        spec = body.get("spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("elements"), list) or not spec["elements"]:
            raise WorkbookError("spec: {elements: [...]}")
        for el in spec["elements"]:
            _table((el.get("source") or {}).get("table", ""))
        values = {"name": name, "description": str(body.get("description") or ""), "spec": json.dumps(spec),
                  "connection": str(body.get("connection") or ""), "shared": bool(body.get("shared")), "updated_ms": _now()}
        if not values["connection"]:
            raise WorkbookError("connection: where it runs")
        with self.engine.begin() as conn:
            if wid:
                r = conn.execute(select(workbooks.c.owner).where(workbooks.c.id == wid)).first()
                if r is None or r.owner != actor:
                    raise WorkbookError("only its owner changes a workbook", 404 if r is None else 403)
                conn.execute(update(workbooks).where(workbooks.c.id == wid).values(**values))
            else:
                wid = uuid.uuid4().hex
                conn.execute(insert(workbooks).values(id=wid, owner=actor, created_ms=_now(), **values))
        return self.get(actor, wid)

    def delete(self, actor: str, wid: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(workbooks).where(workbooks.c.id == wid, workbooks.c.owner == actor)).rowcount:
                raise WorkbookError("no workbook of yours with that id", 404)


def build_workbooks_router(wb: Workbooks, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    from cairn.sqlworkspace.workspace import WorkspaceError

    router = APIRouter(prefix="/sql/workbooks", tags=["sql"])

    def person(p) -> str:
        if p.is_service:
            raise HTTPException(403, "workbooks run as a person")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except (WorkbookError, WorkspaceError) as e:
            raise HTTPException(e.status, str(e))

    @router.post("/compile")
    async def compile_(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: wb.compile(actor, str(body.get("connection") or ""), body.get("element") or {}))

    @router.post("/run")
    async def run(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: wb.run(actor, str(body.get("connection") or ""), body.get("element") or {}))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: wb.list(actor))

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: wb.save(actor, body))

    @router.get("/{wid}")
    async def get(wid: str, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: wb.get(actor, wid))

    @router.put("/{wid}")
    async def change(wid: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: wb.save(actor, body, wid))

    @router.delete("/{wid}", status_code=204)
    async def remove(wid: str, p=Depends(current_principal)):
        actor = person(p)
        await call(lambda: wb.delete(actor, wid))

    return router
