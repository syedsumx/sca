"""``/data/sheets``: Cairn in Excel and Google Sheets.

The spreadsheet add-ins (``cairn/addins/excel`` and ``cairn/addins/sheets``) call these as the person signed in
to them, with an OAuth access token of the add-in's client (:mod:`cairn.identity.addins`), scoped to ``read``,
``query_data`` and ``model``. Every answer is a grid ready to spill into cells: ``values`` is a 2-D array whose
first row is the headers (unless ``headers`` is false), cells are strings, numbers or booleans, and an empty value
is ``""``. Each query runs through the same data access as the SQL editor: the person's table permissions, row
filters and masks apply, and an agent's declared intent is checked.

* ``POST /data/sheets/query`` ``{"sql", "connection"?, "limit"?, "headers"?}``: ``=CAIRN.QUERY(sql)``;
* ``POST /data/sheets/metric`` ``{"name", "dimensions", "filters", "grain"?, "limit"?}``: ``=CAIRN.METRIC(name,
  dims, filters)``. Dimensions are a list or ``"region, country"``; filters a list of objects or of strings such as
  ``"region = EU"``, ``"amount >= 10"``, ``"region in EU, US"``. The metrics layer's metric of that name is used,
  else the semantic layer's approved metric;
* ``POST /data/sheets/ask`` ``{"question", "limit"?}``: ``=CAIRN.ASK(question)``, the answer's rows;
* ``GET /data/sheets/metrics?q=``: the governed metrics to browse in the task pane or sidebar.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from cairn.common.security import Principal

MAX_CELLS = 200_000
_FILTER = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(>=|<=|!=|<>|=|>|<|\bnot\s+in\b|\bin\b)\s*(.*?)\s*$", re.I | re.S)


class SheetsError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class SheetQueryIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    connection: Optional[str] = Field(default=None, max_length=200)
    limit: int = Field(default=1000, ge=1, le=10_000)
    headers: bool = True


class SheetMetricIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    dimensions: Any = None              # ["region"] or "region, country"
    filters: Any = None                 # [{"column", "op", "value"}] or ["region = EU"] or "region = EU; amount > 3"
    grain: Optional[str] = Field(default=None, max_length=10)
    limit: int = Field(default=1000, ge=1, le=10_000)
    headers: bool = True


class SheetAskIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    limit: int = Field(default=1000, ge=1, le=10_000)
    headers: bool = True


def _scalar(text: str) -> Any:
    t = text.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        return t[1:-1]
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d*\.\d+(e-?\d+)?|-?\d+e-?\d+", t, re.I):
        return float(t)
    if t.lower() in ("true", "false"):
        return t.lower() == "true"
    return t


def _cells(v: Any) -> list[str]:
    """A spreadsheet argument (a string, or a range: a list of rows) as its non-empty cells' text."""
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        out: list[str] = []
        for x in v:
            out.extend(_cells(x))
        return out
    if isinstance(v, dict):
        raise SheetsError("expected text or a range of cells")
    s = str(v).strip()
    return [s] if s else []


def parse_dimensions(v: Any) -> list[str]:
    dims = [d.strip() for cell in _cells(v) for d in cell.split(",") if d.strip()]
    for d in dims:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", d):
            raise SheetsError(f"dimension {d!r}: a column name")
    if len(dims) > 6:
        raise SheetsError("dimensions: up to 6")
    return dims


def parse_filter(text: str) -> dict:
    """``"region = EU"`` → ``{"column": "region", "op": "=", "value": "EU"}``; ``in``/``not in`` take a list."""
    m = _FILTER.match(text)
    if not m or not m.group(3):
        raise SheetsError(f"filter {text!r}: write column, operator and value, e.g. region = EU or amount >= 10")
    column, op, value = m.group(1), re.sub(r"\s+", "_", m.group(2).lower()), m.group(3)
    if op in ("in", "not_in"):
        values = [_scalar(x) for x in re.split(r"\s*,\s*", value.strip("() ")) if x.strip()]
        if not values:
            raise SheetsError(f"filter {text!r}: in needs values")
        return {"column": column, "op": op, "values": values}
    return {"column": column, "op": "!=" if op == "<>" else op, "value": _scalar(value)}


def parse_filters(v: Any) -> list[dict]:
    if isinstance(v, list) and all(isinstance(x, dict) for x in v):
        out = v
    else:
        out = [parse_filter(part) for cell in _cells(v) for part in cell.split(";") if part.strip()]
    if len(out) > 20:
        raise SheetsError("filters: up to 20")
    for f in out:
        if not isinstance(f, dict) or not isinstance(f.get("column"), str):
            raise SheetsError("a filter names a column")
    return out


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


def grid(columns: list[str], rows: list[list[Any]], headers: bool = True, **extra) -> dict:
    """The answer as a 2-D array of cells (headers first)."""
    if not columns:
        raise SheetsError("the query returned no columns", 422)
    body = [[_cell(v) for v in r] for r in rows]
    if (len(body) + 1) * len(columns) > MAX_CELLS:
        raise SheetsError(f"the answer is larger than {MAX_CELLS:,} cells; add filters or a limit", 413)
    values = ([list(columns)] if headers else []) + body
    if not values:                       # no rows and no headers: one empty cell, never an empty array
        values = [[""]]
    return {"values": values, "columns": list(columns), "rows": len(body), **extra}


class SheetsService:
    def __init__(self, data, metrics: Callable[[], Any], semantic: Callable[[], Any], ask: Callable[[], Any],
                 default_connection: Optional[str] = None):
        """``metrics()``, ``semantic()``, ``ask()``: the metrics layer, the semantic layer and ask-your-data, or None
        (looked up per call: they are wired after this)."""
        self.data, self._metrics, self._semantic, self._ask = data, metrics, semantic, ask
        self.default_connection = default_connection if default_connection is not None else \
            (os.environ.get("CAIRN_SHEETS_DEFAULT_CONNECTION") or None)

    def connection(self, given: Optional[str]) -> str:
        if given:
            return given
        if self.default_connection:
            return self.default_connection
        names = sorted({s.connection for s in self.data.sources()})
        if len(names) == 1:
            return names[0]
        raise SheetsError("name the connection (the second argument), or ask an operator to set a default: "
                          f"{', '.join(names) or 'no connection serves catalog datasets'}")

    def query(self, actor: str, body: SheetQueryIn, connection: str) -> dict:
        out = self.data.query(actor, body.sql, connection, None, body.limit)
        return grid(out["columns"], out["rows"], body.headers, masked_columns=out.get("masked_columns") or [],
                    truncated=bool(out.get("truncated")), connection=connection)

    def metrics(self, can_view: Callable[[str], bool], q: str = "") -> list[dict]:
        out, seen = [], set()
        m = self._metrics()
        if m is not None:
            ql = q.lower().strip()
            for d in m.list():
                text = " ".join([d["name"], d.get("label") or "", d.get("description") or "", *(d.get("synonyms") or [])])
                if ql and ql not in text.lower():
                    continue
                seen.add(d["name"])
                out.append({"name": d["name"], "label": d.get("label") or d["name"], "description": d.get("description") or "",
                            "dimensions": d.get("dimensions") or [], "time_column": d.get("time_column"),
                            "certified": bool(d.get("certified")), "version": d.get("version"), "source": "metrics"})
        sem = self._semantic()
        if sem is not None:
            for d in sem.metrics(can_view, q):
                if d["name"] in seen:
                    continue
                out.append({"name": d["name"], "label": d["name"], "description": d.get("description") or "",
                            "dimensions": d.get("dimensions") or [], "time_column": d.get("time_dimension"),
                            "certified": True, "version": d.get("version"), "source": "semantic"})
        return out

    def metric(self, actor: str, body: SheetMetricIn, can_view: Callable[[str], bool]) -> dict:
        dims, filters = parse_dimensions(body.dimensions), parse_filters(body.filters)
        m = self._metrics()
        if m is not None:
            from cairn.sqlworkspace.metrics import MetricError

            try:
                m.get(body.name)
            except MetricError as e:
                if e.status != 404:
                    raise
            else:
                out = m.query(actor, {"metrics": [body.name], "dimensions": dims, "filters": filters,
                                      "grain": body.grain, "limit": body.limit})
                return grid(out["columns"], out["rows"], body.headers, masked_columns=out.get("masked_columns") or [],
                            truncated=bool(out.get("truncated")), metric=body.name, source="metrics",
                            version=(out.get("metrics") or [{}])[0].get("version"))
        sem = self._semantic()
        if sem is None:
            raise SheetsError(f"no metric named {body.name!r}", 404)
        sf = [{"dimension": f["column"], "op": f["op"], "value": f.get("value", f.get("values"))} for f in filters]
        out = sem.query(actor, body.name, can_view, dims, sf, body.grain, body.limit)
        r = out["result"]
        return grid(r["columns"], r["rows"], body.headers, masked_columns=r.get("masked_columns") or [],
                    truncated=bool(r.get("truncated")), metric=body.name, source="semantic", version=out.get("version"))

    def ask(self, actor: str, body: SheetAskIn, can_view: Callable[[str], bool]) -> dict:
        a = self._ask()
        if a is None or not a.configured:
            raise SheetsError("asking questions of data is not configured (no LLM provider)", 503)
        out = a.ask(actor, body.question, can_view, [], True, min(body.limit, self.data.max_rows))
        r = out.get("result")
        if r is None:
            raise SheetsError(out.get("error") or "no answer", 422)
        return grid(r["columns"], r["rows"], body.headers, masked_columns=r.get("masked_columns") or [],
                    truncated=bool(r.get("truncated")), sql=out.get("sql"), explanation=out.get("explanation"))


def build_router(svc: SheetsService, current_principal, can_view, intents=None, ai_actor=None) -> APIRouter:
    from cairn.dataaccess.api import check_agent_sql

    router = APIRouter(prefix="/data/sheets", tags=["data"])

    async def call(fn):
        from cairn.context.semantic import SemanticError
        from cairn.dataaccess.ask import AskError
        from cairn.dataaccess.service import DataAccessError
        from cairn.sqlworkspace.metrics import MetricError
        from cairn.sqlworkspace.workbooks import WorkbookError

        try:
            return await run_in_threadpool(fn)
        except (SheetsError, DataAccessError, MetricError, WorkbookError, SemanticError, AskError) as e:
            raise HTTPException(getattr(e, "status", 400), str(e))

    def person(p: Principal) -> str:
        if p.is_service:
            raise HTTPException(403, "spreadsheet answers are for people (sign in through the add-in)")
        return p.urn

    @router.get("/metrics")
    async def metrics(q: str = "", p: Principal = Depends(current_principal)):
        person(p)
        return await call(lambda: svc.metrics(can_view(p), q[:200]))

    @router.post("/query")
    async def query(body: SheetQueryIn, p: Principal = Depends(current_principal)):
        actor = person(p)

        def run():
            connection = svc.connection(body.connection)
            check_agent_sql(svc.data, intents, p, body.sql, connection, None)
            return svc.query(actor, body, connection)
        return await call(run)

    @router.post("/metric")
    async def metric(body: SheetMetricIn, p: Principal = Depends(current_principal)):
        actor = person(p)
        return await call(lambda: svc.metric(actor, body, can_view(p)))

    @router.post("/ask")
    async def ask(body: SheetAskIn, p: Principal = Depends(current_principal)):
        actor = person(p)
        if ai_actor is not None:
            ai_actor(p)
        return await call(lambda: svc.ask(actor, body, can_view(p)))

    return router
