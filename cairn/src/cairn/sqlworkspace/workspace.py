"""The SQL workspace: run read-only SQL on any connection as the signed-in person, browse what they may
query, keep a history of what they ran, and save queries (to keep, or to share with everyone) with a
chart. Queries run through the data access service, so every table is authorized and masked for the
caller, exactly as on a dataset's page; this module adds no way around it.

Tables: ``cairn_sql_history`` (each run: who, where, what, how it went) and ``cairn_saved_queries``.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, and_, delete, or_, select, update

from cairn.common.db import metadata

sql_history = Table(
    "cairn_sql_history", metadata,
    Column("id", String(32), primary_key=True),
    Column("actor", String(256), nullable=False, index=True),
    Column("connection", String(256), nullable=False),
    Column("sql", Text, nullable=False),
    Column("status", String(16), nullable=False),      # ok | error | running | cancelled (statements, L163)
    Column("rows", Integer, nullable=True),
    Column("ms", Integer, nullable=True),
    Column("error", String(1000), nullable=True),
    Column("at_ms", BigInteger, nullable=False, index=True),
)

saved_queries = Table(
    "cairn_saved_queries", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("name", String(256), nullable=False),
    Column("description", String(2000), nullable=False, default=""),
    Column("connection", String(256), nullable=False),
    Column("sql", Text, nullable=False),
    Column("shared", Boolean, nullable=False, default=False),
    Column("chart", Text, nullable=False, default="{}"),       # {"type": one of CHART_TYPES, "x": col, "y": [cols]}
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

HISTORY_KEEP = 500          # per person
CHART_TYPES = ("table", "bar", "stacked_bar", "line", "area", "pie", "scatter", "number", "heatmap", "map")


class WorkspaceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _chart(chart: Optional[dict]) -> str:
    chart = chart or {}
    t = chart.get("type", "table")
    if t not in CHART_TYPES:
        raise WorkspaceError(f"chart type is one of {', '.join(CHART_TYPES)}")
    y = chart.get("y") or []
    if not isinstance(y, list) or not all(isinstance(c, str) for c in y):
        raise WorkspaceError("chart y is a list of column names")
    out = {"type": t, "x": str(chart.get("x") or ""), "y": y[:8]}
    # A heatmap colours one value (y) by two columns, x across and ``series`` down; a map places rows at ``lat`` and
    # ``lon`` (degrees), sized by y and named by x.
    for k in ("series", "lat", "lon"):
        v = chart.get(k)
        if v is not None and not isinstance(v, str):
            raise WorkspaceError(f"chart {k} is a column name")
        if v:
            out[k] = v
    if t == "heatmap" and not (out["x"] and out.get("series") and y):
        raise WorkspaceError("a heatmap needs x, series and a y column")
    if t == "map" and not (out.get("lat") and out.get("lon")):
        raise WorkspaceError("a map needs lat and lon columns")
    return json.dumps(out)


def _saved(r, level: int = 0) -> dict:
    from cairn.sqlworkspace.sharing import role_name

    return {"id": r.id, "owner": r.owner, "role": role_name(level), "name": r.name, "description": r.description,
            "connection": r.connection,
            "sql": r.sql, "shared": bool(r.shared), "chart": json.loads(r.chart or "{}"),
            "created_ms": r.created_ms, "updated_ms": r.updated_ms}


class SqlWorkspace:
    def __init__(self, engine, data, graph=None):
        """``data``: the DataAccessService queries run through. ``graph``: the metadata store, for the
        schema browser."""
        self.engine, self.data, self.graph = engine, data, graph
        self.cache = None                                  # cairn.sqlworkspace.cache.ResultCache (dashboards, workbooks)
        self.matviews = None                               # cairn.pipelines.matviews, set when derived tables are
        self.sharing = None                                # cairn.sqlworkspace.sharing.Sharing (people and groups)
        self.on_change: Optional[Callable[[str, str, str], None]] = None   # (kind, id, actor) after a save or delete
        self.on_view: Optional[Callable[[str, str, str], None]] = None     # (kind, id, actor) when a saved query is run
        self._schema_cache: dict[str, tuple[float, list[dict]]] = {}

    # ---- running ------------------------------------------------------------------------
    def run(self, actor: str, sql: str, connection: str, limit: int = 200, cache: bool = False) -> dict:
        """Runs ``sql`` as ``actor`` (authorized and masked per table) and records it in their history. With
        ``cache`` (dashboards, workbooks, metrics), a result computed for someone whose policies on every table read
        are the same is reused (:mod:`cairn.sqlworkspace.cache`)."""
        from cairn.dataaccess.service import DataAccessError

        started = time.monotonic()
        if self.matviews is not None:                      # CREATE / REFRESH / DROP MATERIALIZED VIEW are Cairn's
            from cairn.pipelines.derived import DerivedError

            try:
                done = self.matviews.handle(actor, sql)
            except DerivedError as e:
                self._record(actor, connection, sql, "error", None, int((time.monotonic() - started) * 1000), str(e))
                raise WorkspaceError(str(e), e.status)
            if done is not None:
                out = self.matviews.result(done, started)
                self._record(actor, connection, sql, "ok", 1, out["elapsed_ms"], None)
                return out
        ticket = None
        if cache and self.cache is not None:
            hit, ticket = self.cache.get(actor, sql, connection, limit)
            if hit is not None:
                self._record(actor, connection, sql, "ok", len(hit.get("rows") or []), 0, None)
                return hit
        try:
            out = self.data.query(actor, sql, connection, None, limit)
        except DataAccessError as e:
            self._record(actor, connection, sql, "error", None, int((time.monotonic() - started) * 1000), str(e))
            raise WorkspaceError(str(e), e.status)
        self._record(actor, connection, sql, "ok", len(out.get("rows") or []), out.get("elapsed_ms"), None)
        if ticket is not None:
            self.cache.put(ticket, out)
        return out

    def explain(self, actor: str, sql: str, connection: str) -> dict:
        """The engine's plan for ``sql`` as ``actor`` would run it (L163; :meth:`DataAccessService.explain`). Plans
        are not recorded in the history: nothing ran."""
        from cairn.dataaccess.service import DataAccessError

        try:
            return self.data.explain(actor, sql, connection)
        except DataAccessError as e:
            raise WorkspaceError(str(e), e.status)

    # ---- statements read page by page (L163): one history entry, written when it starts and when it ends --------
    def started(self, actor: str, connection: str, sql: str) -> str:
        return self._record(actor, connection, sql, "running", None, None, None)

    def finished(self, actor: str, hid: str, status: str, rows: Optional[int], ms: Optional[int],
                 error: Optional[str] = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(sql_history).where((sql_history.c.id == hid) & (sql_history.c.actor == actor)
                                                   & (sql_history.c.status == "running"))
                         .values(status=status, rows=rows, ms=ms, error=(error or None) and error[:1000]))

    def _record(self, actor, connection, sql, status, rows, ms, error) -> str:
        hid = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(sql_history.insert().values(id=hid, actor=actor, connection=connection, sql=sql,
                                                     status=status, rows=rows, ms=ms, error=(error or None) and error[:1000],
                                                     at_ms=_now()))
            # Keep the newest HISTORY_KEEP per person.
            old = conn.execute(select(sql_history.c.id).where(sql_history.c.actor == actor)
                               .order_by(sql_history.c.at_ms.desc()).offset(HISTORY_KEEP)).scalars().all()
            if old:
                conn.execute(delete(sql_history).where(sql_history.c.id.in_(old)))
        return hid

    def history(self, actor: str, limit: int = 100) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(sql_history).where(sql_history.c.actor == actor)
                                .order_by(sql_history.c.at_ms.desc()).limit(limit)).all()
        return [{"id": r.id, "connection": r.connection, "sql": r.sql, "status": r.status, "rows": r.rows,
                 "ms": r.ms, "error": r.error, "at_ms": r.at_ms} for r in rows]

    # ---- saved queries ------------------------------------------------------------------
    def saved(self, actor: str) -> list[dict]:
        """The caller's queries, those shared with them (or a group of theirs) and those shared with everyone."""
        from cairn.sqlworkspace.sharing import access

        visible = [saved_queries.c.owner == actor, saved_queries.c.shared]
        if self.sharing is not None:
            visible.append(saved_queries.c.id.in_(self.sharing.ids(actor, "query")))
        with self.engine.connect() as conn:
            rows = conn.execute(select(saved_queries).where(or_(*visible))
                                .order_by(saved_queries.c.updated_ms.desc())).all()
        return [_saved(r, access(self.sharing, actor, "query", r)) for r in rows]

    def get(self, actor: str, qid: str, through_dashboard: bool = False) -> dict:
        """A saved query the actor may open. ``through_dashboard``: a tile of a dashboard they opened (its
        definition is theirs to render; its rows are still read as them)."""
        from cairn.sqlworkspace.sharing import access

        with self.engine.connect() as conn:
            r = conn.execute(select(saved_queries).where(saved_queries.c.id == qid)).first()
        level = access(self.sharing, actor, "query", r)
        if r is None or (not level and not through_dashboard):
            raise WorkspaceError("no such saved query", 404)
        return _saved(r, level)

    def save(self, actor: str, body: dict, qid: Optional[str] = None) -> dict:
        name = str(body.get("name") or "").strip()
        sql, connection = str(body.get("sql") or ""), str(body.get("connection") or "")
        if not name or not sql.strip() or not connection:
            raise WorkspaceError("a saved query needs a name, its SQL and its connection")
        from cairn.quality.models import validate_readonly_sql

        try:
            validate_readonly_sql(sql)
        except ValueError as e:
            raise WorkspaceError(str(e))
        values = {"name": name[:256], "description": str(body.get("description") or "")[:2000], "sql": sql,
                  "connection": connection, "shared": bool(body.get("shared")), "chart": _chart(body.get("chart")),
                  "updated_ms": _now()}
        from cairn.sqlworkspace.sharing import OWNER, ROLES, access

        with self.engine.begin() as conn:
            if qid is None:
                qid = uuid.uuid4().hex
                conn.execute(saved_queries.insert().values(id=qid, owner=actor, created_ms=values["updated_ms"], **values))
            else:
                r = conn.execute(select(saved_queries).where(saved_queries.c.id == qid)).first()
                level = access(self.sharing, actor, "query", r)
                if level < ROLES["EDITOR"]:
                    raise WorkspaceError("no such saved query", 404) if not level else \
                        WorkspaceError("only its owner and editors change a saved query", 403)
                if level < OWNER:
                    values["shared"] = bool(r.shared)       # who sees it is the owner's to decide
                conn.execute(update(saved_queries).where(saved_queries.c.id == qid).values(**values))
        if self.on_change is not None:
            if body.get("autosave"):                    # the editor's autosave (L163): versions coalesce
                self.on_change("query", qid, actor, autosave=True)
            else:
                self.on_change("query", qid, actor)
        return self.get(actor, qid)

    def delete(self, actor: str, qid: str) -> None:
        with self.engine.begin() as conn:
            n = conn.execute(delete(saved_queries).where(and_(saved_queries.c.id == qid, saved_queries.c.owner == actor))).rowcount
            if n and self.sharing is not None:
                self.sharing.drop("query", qid, conn)
        if not n:
            raise WorkspaceError("no such saved query of yours", 404)
        if self.on_change is not None:
            self.on_change("query", qid, actor)

    def run_saved(self, actor: str, qid: str, limit: int = 1000) -> dict:
        """Runs a saved query as the caller (never as its owner): shared queries show each person only
        what they may see."""
        q = self.get(actor, qid)
        out = {**self.run(actor, q["sql"], q["connection"], limit), "query": q}
        if self.on_view is not None:
            self.on_view("query", qid, actor)
        return out

    # ---- schema browser -----------------------------------------------------------------
    def schema(self, connection: str, can_view: Callable[[str], bool]) -> list[dict]:
        """The datasets of ``connection``'s platform the caller may see, with their columns."""
        src = next((s for s in self.data.sources() if s.connection == connection), None)
        if src is None:
            raise WorkspaceError(f"unknown connection {connection!r}", 404)
        if self.graph is None:
            return []
        now = time.monotonic()
        hit = self._schema_cache.get(src.platform)
        if hit is None or hit[0] < now:
            rows = self.graph.scroll_entities("dataset", ["schemaMetadata", "datasetProperties"])
            items = []
            for r in rows:
                urn = r["urn"]
                if f"urn:li:dataPlatform:{src.platform}," not in urn:
                    continue
                name = urn.split(",")[1]
                cols = [{"name": f.get("fieldPath", ""), "type": (f.get("nativeDataType") or "")}
                        for f in ((r.get("schemaMetadata") or {}).get("fields") or [])]
                items.append({"urn": urn, "name": name, "columns": cols})
            hit = (now + 60, sorted(items, key=lambda i: i["name"]))
            self._schema_cache[src.platform] = hit
        return [i for i in hit[1] if can_view(i["urn"])]


class RunIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    connection: str = Field(min_length=1)
    limit: int = Field(default=200, ge=1, le=1000)


class ExplainIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    connection: str = Field(min_length=1)


def build_router(ws: SqlWorkspace, current_principal, can_view) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/sql", tags=["sql"])

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "the SQL workspace is for people")
        return p

    def wrap(fn):
        try:
            return fn()
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/run")
    async def run(body: RunIn, p=Depends(current_principal)):
        person(p)
        return await run_in_threadpool(lambda: wrap(lambda: ws.run(p.urn, body.sql, body.connection, body.limit)))

    @router.post("/explain")
    async def explain(body: ExplainIn, p=Depends(current_principal)):
        person(p)
        return await run_in_threadpool(lambda: wrap(lambda: ws.explain(p.urn, body.sql, body.connection)))

    @router.get("/history")
    def history(limit: int = Query(100, ge=1, le=HISTORY_KEEP), p=Depends(current_principal)):
        return ws.history(person(p).urn, limit)

    @router.get("/schema")
    def schema(connection: str, p=Depends(current_principal)):
        person(p)
        return wrap(lambda: ws.schema(connection, can_view(p)))

    @router.get("/saved")
    def saved(p=Depends(current_principal)):
        return ws.saved(person(p).urn)

    @router.post("/saved", status_code=201)
    def create(body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: ws.save(person(p).urn, body))

    @router.get("/saved/{qid}")
    def one(qid: str, p=Depends(current_principal)):
        return wrap(lambda: ws.get(person(p).urn, qid))

    @router.put("/saved/{qid}")
    def change(qid: str, body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: ws.save(person(p).urn, body, qid))

    @router.delete("/saved/{qid}", status_code=204)
    def remove(qid: str, p=Depends(current_principal)):
        wrap(lambda: ws.delete(person(p).urn, qid))

    @router.post("/saved/{qid}/run")
    async def run_saved(qid: str, limit: int = Query(1000, ge=1, le=1000), p=Depends(current_principal)):
        person(p)
        return await run_in_threadpool(lambda: wrap(lambda: ws.run_saved(p.urn, qid, limit)))

    return router
