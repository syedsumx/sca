"""BI assets in the catalog: saved queries, workbooks and dashboards with lineage from the tables and columns they read.

Each save publishes the asset to Cairn's catalog: a saved query or a workbook as a **chart**, a dashboard as a
**dashboard** whose charts are the saved queries its tiles use. The chart's inputs are the datasets its SQL
reads (``chartInfo.inputs``), and its ``inputFields`` the columns, so the catalog's lineage reaches from a table
to every dashboard over it. Deleting an asset marks its entity removed.

The columns each asset reads are also kept in Cairn (``cairn_bi_lineage``) for **impact**:
``GET /sql/impact?dataset=<urn>&column=<name>`` lists the queries, workbooks and dashboards that would break if
the column (or, without ``column``, the table) went away. ``SELECT *`` reads every column. People see the names of
the assets they may open; the rest are counted, with their owners, so whoever plans the change knows whom to
ask. ``POST /sql/catalog/sync`` (an operator) publishes everything again.

Columns are found by parsing the SQL (a workbook element or a metric tile through the SQL it compiles to, as
its owner). A column that cannot be tied to one table of a query reading several is counted against each of them:
impact over-reports rather than misses.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Optional

from sqlalchemy import Column, Index, PrimaryKeyConstraint, String, Table, and_, delete, insert, select

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

PLATFORM = "cairn"
ALL = "*"

bi_lineage = Table(
    "cairn_bi_lineage", metadata,
    Column("kind", String(16), nullable=False),
    Column("asset_id", String(64), nullable=False),
    Column("dataset", String(700), nullable=False),
    Column("column", String(256), nullable=False),
    PrimaryKeyConstraint("kind", "asset_id", "dataset", "column"),
    Index("ix_cairn_bi_lineage_dataset", "dataset", "column"),
)


def chart_urn(kind: str, asset_id: str) -> str:
    return f"urn:li:chart:({PLATFORM},{kind}.{asset_id})"


def dashboard_urn(asset_id: str) -> str:
    return f"urn:li:dashboard:({PLATFORM},{asset_id})"


def columns_read(sql: str, dialect: str, urn_of: Callable[[Any], str]) -> dict[str, set[str]]:
    """``{dataset urn: columns}`` that ``sql`` reads (``*`` for all of them)."""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read=dialect)
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = [t for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes]
    by_alias: dict[str, str] = {}
    out: dict[str, set[str]] = {}
    for t in tables:
        u = urn_of(t)
        out.setdefault(u, set())
        by_alias[(t.alias_or_name or t.name).lower()] = u
        by_alias.setdefault(t.name.lower(), u)
    if not out:
        return out
    every = list(out)
    for star in tree.find_all(exp.Star):
        parent = star.parent
        if isinstance(parent, exp.Column) and parent.table and parent.table.lower() in by_alias:
            out[by_alias[parent.table.lower()]].add(ALL)
        elif isinstance(star.parent, (exp.Select, exp.Column)) or star.parent is None:
            for u in every:
                out[u].add(ALL)
    for col in tree.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        name = col.name
        if not name:
            continue
        q = (col.table or "").lower()
        if q and q in by_alias:
            out[by_alias[q]].add(name)
        elif q in ctes:
            continue                                       # resolved through the CTE's own columns
        else:
            for u in (every if len(every) > 1 else every[:1]):
                out[u].add(name)
    return out


class BiCatalog:
    def __init__(self, engine, graph, workspace, workbooks, boards, metrics=None, can_open=None, is_operator=None):
        """``can_open(actor, kind, id)``: may the actor open the asset (sharing); ``is_operator(actor)``."""
        self.engine, self.graph, self.ws, self.wb, self.boards, self.metrics = engine, graph, workspace, workbooks, boards, metrics
        self.can_open = can_open or (lambda actor, kind, aid: False)
        self.is_operator = is_operator or (lambda actor: False)

    # ---- what an asset reads ---------------------------------------------------------------------
    def _refs_sql(self, sql: str, connection: str) -> dict[str, set[str]]:
        source = self.ws.data._source(connection, None)
        return columns_read(sql, source.dialect, lambda t: source.urn(t.db or "", t.name, t.catalog or None))

    def _refs_element(self, owner: str, connection: str, element: dict) -> dict[str, set[str]]:
        """A workbook element is compiled again against the table's columns each time it runs, so a column
        breaks it only if the element names it: the columns without which it no longer compiles."""
        from cairn.sqlworkspace.workbooks import WorkbookError, compile_element

        table = (element.get("source") or {}).get("table", "")
        cols = self.wb.columns_of(owner, connection, table)
        pivot = element.get("pivot") or {}
        values = pivot.get("values") or (["_"] if pivot else None)
        compile_element(element, cols, values)                    # it compiles as it is
        needed = set()
        for c in cols:
            try:
                compile_element(element, [x for x in cols if x != c], values)
            except WorkbookError:
                needed.add(c)
        source = self.ws.data._source(connection, None)
        schema, name = table.split(".", 1) if "." in table else ("", table)
        return {source.urn(schema, name, None): needed}

    def _refs(self, kind: str, row: dict) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}

        def add(refs):
            for u, cols in refs.items():
                out.setdefault(u, set()).update(cols)
        if kind == "query":
            add(self._refs_sql(row["sql"], row["connection"]))
        elif kind == "workbook":
            for el in (row.get("spec") or {}).get("elements") or []:
                add(self._refs_element(row["owner"], row["connection"], el))
        elif kind == "dashboard":
            for t in row.get("tiles") or []:
                try:
                    if t.get("query"):
                        q = self.ws.get(row["owner"], t["query"], through_dashboard=True)
                        add(self._refs_sql(q["sql"], q["connection"]))
                    elif t.get("element"):
                        add(self._refs_element(row["owner"], t.get("connection") or "", t["element"]))
                    elif t.get("metric") and self.metrics is not None:
                        element, defs = self.metrics.compile(row["owner"], t["metric"])
                        add(self._refs_element(row["owner"], defs[0]["connection"], element))
                except Exception as e:  # noqa: BLE001 - one tile that no longer compiles does not hide the others
                    logger.warning("lineage of a tile of dashboard %s: %s", row["id"], e)
        return out

    # ---- publishing -----------------------------------------------------------------------------
    def _row(self, kind: str, aid: str) -> Optional[dict]:
        from cairn.sqlworkspace.boards import _board, dashboards
        from cairn.sqlworkspace.workbooks import workbooks
        from cairn.sqlworkspace.workspace import _saved, saved_queries

        t = {"query": saved_queries, "workbook": workbooks, "dashboard": dashboards}[kind]
        with self.engine.connect() as conn:
            r = conn.execute(select(t).where(t.c.id == aid)).first()
        if r is None:
            return None
        if kind == "query":
            return _saved(r)
        if kind == "dashboard":
            return _board(r)
        d = dict(r._mapping)
        return {**d, "spec": json.loads(d["spec"])}

    def changed(self, kind: str, aid: str) -> None:
        """After a save or a delete: the asset's lineage and its catalog entity, as they are now."""
        try:
            row = self._row(kind, aid)
            if row is None:
                self._forget(kind, aid)
            else:
                self._publish(kind, row)
        except Exception:  # noqa: BLE001 - the catalog being away never fails the save; sync() catches up
            logger.exception("publishing %s %s to the catalog failed", kind, aid)

    def _publish(self, kind: str, row: dict) -> None:
        from cairn.common.proposals import audit_stamp, make_mcp, schema_field_urn

        refs = self._refs(kind, row)
        with self.engine.begin() as conn:
            conn.execute(delete(bi_lineage).where(and_(bi_lineage.c.kind == kind, bi_lineage.c.asset_id == row["id"])))
            values = [{"kind": kind, "asset_id": row["id"], "dataset": u, "column": c[:256]}
                      for u, cols in refs.items() for c in (cols or {ALL})]
            if values:
                conn.execute(insert(bi_lineage), values)
        if self.graph is None:
            return
        stamp = audit_stamp(row["owner"], int(row.get("updated_ms") or time.time() * 1000))
        changed = {"created": stamp, "lastModified": stamp}
        datasets = sorted(refs)
        props = {"cairn_kind": kind, "cairn_id": row["id"], "shared": str(bool(row.get("shared"))).lower()}
        if kind == "dashboard":
            urn = dashboard_urn(row["id"])
            charts = sorted({chart_urn("query", t["query"]) for t in row.get("tiles") or [] if t.get("query")})
            info = {"title": row["name"], "description": row.get("description") or "", "lastModified": changed,
                    "charts": charts, "datasets": datasets, "customProperties": props,
                    "dashboardUrl": f"/sql/dashboards/{row['id']}"}
            mcps = [make_mcp(urn, "dashboardInfo", info)]
        else:
            urn = chart_urn(kind, row["id"])
            info = {"title": row["name"], "description": row.get("description") or "", "lastModified": changed,
                    "inputs": [{"string": u} for u in datasets], "customProperties": props,
                    "chartUrl": f"/sql/{'saved' if kind == 'query' else 'workbooks'}/{row['id']}",
                    "type": "TABLE"}
            fields = [{"schemaFieldUrn": schema_field_urn(u, c)} for u in datasets for c in sorted(refs[u]) if c != ALL]
            mcps = [make_mcp(urn, "chartInfo", info), make_mcp(urn, "inputFields", {"fields": fields})]
        mcps += [make_mcp(urn, "ownership", {"owners": [{"owner": row["owner"], "type": "DATAOWNER"}],
                                              "lastModified": stamp}),
                 make_mcp(urn, "status", {"removed": False})]
        self.graph.emit(mcps)

    def _forget(self, kind: str, aid: str) -> None:
        from cairn.common.proposals import make_mcp

        with self.engine.begin() as conn:
            conn.execute(delete(bi_lineage).where(and_(bi_lineage.c.kind == kind, bi_lineage.c.asset_id == aid)))
        if self.graph is not None:
            urn = dashboard_urn(aid) if kind == "dashboard" else chart_urn(kind, aid)
            self.graph.emit([make_mcp(urn, "status", {"removed": True})])

    def sync(self, actor: str) -> dict:
        if not self.is_operator(actor):
            from cairn.sqlworkspace.workspace import WorkspaceError

            raise WorkspaceError("publishing every BI asset needs MANAGE_SYSTEM_OPERATIONS", 403)
        from cairn.sqlworkspace.boards import dashboards
        from cairn.sqlworkspace.workbooks import workbooks
        from cairn.sqlworkspace.workspace import saved_queries

        n = 0
        for kind, t in (("query", saved_queries), ("workbook", workbooks), ("dashboard", dashboards)):
            with self.engine.connect() as conn:
                ids = [r[0] for r in conn.execute(select(t.c.id))]
            for aid in ids:
                self.changed(kind, aid)
                n += 1
        return {"published": n}

    # ---- impact -------------------------------------------------------------------------------------
    def impact(self, actor: str, dataset: str, column: Optional[str] = None) -> dict:
        q = select(bi_lineage).where(bi_lineage.c.dataset == dataset)
        if column:
            q = q.where(bi_lineage.c.column.in_([column, ALL]))
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        direct: dict[tuple[str, str], set[str]] = {}
        for r in rows:
            direct.setdefault((r.kind, r.asset_id), set()).add(r.column)
        # Dashboards whose tiles use an affected saved query break too.
        queries = {aid for k, aid in direct if k == "query"}
        via: dict[str, list[str]] = {}
        if queries:
            from cairn.sqlworkspace.boards import _board, dashboards

            with self.engine.connect() as conn:
                for r in conn.execute(select(dashboards)):
                    b = _board(r)
                    hit = [t["query"] for t in b["tiles"] if t.get("query") in queries]
                    if hit:
                        via[b["id"]] = hit
                        direct.setdefault(("dashboard", b["id"]), set())
        assets, hidden = [], {}
        for (kind, aid), cols in sorted(direct.items()):
            row = self._row(kind, aid)
            if row is None:
                continue
            if row["owner"] == actor or self.can_open(actor, kind, aid) or self.is_operator(actor):
                assets.append({"kind": kind, "id": aid, "name": row["name"], "owner": row["owner"],
                               "columns": sorted(cols), "through_queries": via.get(aid, [])})
            else:
                hidden[row["owner"]] = hidden.get(row["owner"], 0) + 1
        return {"dataset": dataset, "column": column, "assets": assets,
                "others": [{"owner": o, "assets": n} for o, n in sorted(hidden.items())]}


def build_bi_catalog_router(cat: BiCatalog, current_principal) -> Any:
    from fastapi import APIRouter, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    from cairn.sqlworkspace.workspace import WorkspaceError

    router = APIRouter(prefix="/sql", tags=["sql"])

    @router.get("/impact")
    async def impact(dataset: str, column: Optional[str] = None, p=Depends(current_principal)):
        return await run_in_threadpool(lambda: cat.impact(p.urn, dataset, column))

    @router.post("/catalog/sync")
    async def sync(p=Depends(current_principal)):
        try:
            return await run_in_threadpool(lambda: cat.sync(p.urn))
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    return router
