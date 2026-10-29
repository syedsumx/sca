"""Column-level lineage from SQL: which source columns each output column is computed from.

The context miner already has the SQL that writes data: the query log (``INSERT … SELECT``,
``CREATE TABLE … AS SELECT``, ``MERGE``-free statements) and view definitions (``viewProperties``).
For each such statement, the target's columns are traced through CTEs, subqueries, joins and
expressions back to physical columns with ``sqlglot.lineage``, using the catalogued schemas so that
``SELECT *`` and unqualified columns resolve. Tables are mapped to dataset urns as elsewhere in the
miner (``map_table``).

The result is merged into the target's ``upstreamLineage``: missing upstream datasets and
``fineGrainedLineages`` are added, nothing a crawler or a person wrote is removed, and the statement
that produced an edge is kept on it (``query``) so the lineage view can show the SQL. A column copied
as is is a ``COPY``; anything computed is ``SQL``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

from cairn.common.proposals import make_mcp
from cairn.context.miner import CONTEXT_ACTOR, Snapshot, _dataset_parts, map_table
from cairn.context.sql import PLATFORM_DIALECTS

logger = logging.getLogger(__name__)
MAX_COLUMNS = 500


@dataclass
class ColumnEdge:
    target: str                      # dataset urn
    column: str
    sources: list[tuple[str, str]]   # (dataset urn, column)
    operation: str                   # COPY | SQL
    query: Optional[str] = None      # query urn


@dataclass
class Statement:
    target: Optional[str]            # the table written, as named in the SQL (None for a view: known by urn)
    columns: list[str]
    select: object = None
    tables: list[str] = field(default_factory=list)


def _table(t) -> str:
    return ".".join(p for p in (t.catalog, t.db, t.name) if p).lower()


def _name(s: str) -> str:
    return s.split(".")[-1].strip('"`[]').lower()


def parse(sql: str, dialect: Optional[str] = None, target_columns: Optional[list[str]] = None) -> list[Statement]:
    """The writing statements in ``sql``: their target, output column names and SELECT."""
    import sqlglot
    from sqlglot import exp

    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect, error_level=sqlglot.ErrorLevel.IGNORE) if t]
    except Exception:  # noqa: BLE001 - sqlglot raises many error types on exotic SQL
        return []
    out = []
    for tree in trees:
        target, columns, select = None, [], None
        if isinstance(tree, exp.Insert) and isinstance(tree.expression, exp.Query):
            this = tree.this
            if isinstance(this, exp.Schema):
                columns = [_name(c.name) for c in this.expressions]
                this = this.this
            target, select = _table(this), tree.expression
        elif isinstance(tree, exp.Create) and isinstance(tree.expression, exp.Query):
            this = tree.this
            if isinstance(this, exp.Schema):
                columns = [_name(c.name) for c in this.expressions if isinstance(c, (exp.ColumnDef, exp.Identifier))]
                this = this.this
            target, select = _table(this), tree.expression
        elif isinstance(tree, exp.Query) and target_columns is not None:     # a view's definition
            select = tree
        if select is None:
            continue
        tables = sorted({_table(t) for t in select.find_all(exp.Table) if t.name})
        out.append(Statement(target=target, columns=columns, select=select, tables=tables))
    return out


def _schema(tables: list[str], resolved: dict[str, str], snap: Snapshot) -> Optional[dict]:
    """A sqlglot schema for the statement's tables, as they are named in it (all at one depth)."""
    depth = max((len(t.split(".")) for t in tables), default=0)
    out: dict = {}
    for t in tables:
        urn = resolved.get(t)
        fields = ((snap.datasets.get(urn) or {}).get("schemaMetadata") or {}).get("fields") or [] if urn else []
        if not fields or len(t.split(".")) != depth:
            continue
        node = out
        for part in t.split(".")[:-1]:
            node = node.setdefault(part, {})
        node[t.split(".")[-1]] = {_name(f["fieldPath"]): str(f.get("nativeDataType") or "text") for f in fields
                                  if f.get("fieldPath")}
    return out or None


def trace(stmt: Statement, target_urn: str, snap: Snapshot, dialect: Optional[str] = None,
          target_columns: Optional[list[str]] = None, query: Optional[str] = None) -> list[ColumnEdge]:
    """Trace each output column of ``stmt`` to the physical columns it reads."""
    from sqlglot import exp
    from sqlglot.lineage import lineage
    from sqlglot.optimizer.qualify import qualify

    candidates = [u for u in snap.datasets if u != target_urn]
    resolved = {t: u for t in stmt.tables if (u := map_table(t, candidates))}
    schema = _schema(stmt.tables, resolved, snap)
    select = stmt.select.copy()
    names = stmt.columns or []
    if isinstance(select, exp.Select) and not any(isinstance(e, exp.Star) or (isinstance(e, exp.Column) and e.is_star)
                                                  for e in select.expressions):
        exprs = select.expressions
        names = names or [_name(e.alias_or_name) if e.alias_or_name else "" for e in exprs]
        fallback = target_columns or []
        named = []
        for i, e in enumerate(exprs):
            n = names[i] if i < len(names) and names[i] else (fallback[i] if i < len(fallback) else f"_col_{i}")
            named.append(exp.alias_(e.unalias() if isinstance(e, exp.Alias) else e, n, quoted=False))
        select.set("expressions", named)
        names = [_name(e.alias) for e in named]
        direct = {n: isinstance(e.unalias(), exp.Column) for n, e in zip(names, named)}
    else:                                                       # SELECT * or a UNION: let qualify expand it
        try:
            select = qualify(select, schema=schema, dialect=dialect, validate_qualify_columns=False)
        except Exception:  # noqa: BLE001
            return []
        names = [_name(n) for n in select.named_selects]
        direct = {}
    out_names = stmt.columns if stmt.columns and len(stmt.columns) == len(names) else names
    edges = []
    for n, out_name in list(zip(names, out_names))[:MAX_COLUMNS]:
        if not n or n.startswith("_col_"):
            continue
        try:
            node = lineage(n, select, schema=schema, dialect=dialect)
        except Exception:  # noqa: BLE001 - a column sqlglot can't trace is skipped, not fatal
            continue
        sources = set()
        for leaf in node.walk():
            if leaf.downstream or not isinstance(leaf.source, exp.Table):
                continue
            urn = resolved.get(_table(leaf.source))
            if urn:
                sources.add((urn, _name(leaf.name)))
        if sources:
            op = "COPY" if direct.get(n) and len(sources) == 1 else "SQL"
            edges.append(ColumnEdge(target_urn, out_name, sorted(sources), op, query))
    return edges


def _columns_of(urn: str, snap: Snapshot) -> list[str]:
    fields = ((snap.datasets.get(urn) or {}).get("schemaMetadata") or {}).get("fields") or []
    return [_name(f["fieldPath"]) for f in fields if f.get("fieldPath")]


def extract(snap: Snapshot) -> list[ColumnEdge]:
    """Column lineage from every writing query and every view definition in the snapshot."""
    edges: list[ColumnEdge] = []
    for q in snap.queries:
        dialect = PLATFORM_DIALECTS.get((q.get("platform") or "").lower())
        for stmt in parse(q["sql"], dialect):
            pool = q.get("subjects") or list(snap.datasets)
            target = map_table(stmt.target, pool) or map_table(stmt.target, snap.datasets)
            if target:
                edges += trace(stmt, target, snap, dialect, _columns_of(target, snap), q["urn"])
    for urn, row in snap.datasets.items():
        logic = ((row.get("viewProperties") or {}).get("viewLogic") or "").strip()
        if not logic:
            continue
        dialect = PLATFORM_DIALECTS.get((_dataset_parts(urn)[0] or "").lower())
        cols = _columns_of(urn, snap)
        for stmt in parse(logic, dialect, target_columns=cols):
            if stmt.target is None:
                edges += trace(stmt, urn, snap, dialect, cols)
    return edges


def _field(dataset: str, column: str) -> str:
    return f"urn:li:schemaField:({dataset},{column})"


def to_mcps(edges: Iterable[ColumnEdge], graph, actor: str = CONTEXT_ACTOR) -> list[dict]:
    """Merge the edges into each target's ``upstreamLineage``; only what is missing is added."""
    by_target: dict[str, list[ColumnEdge]] = {}
    for e in edges:
        by_target.setdefault(e.target, []).append(e)
    now = int(time.time() * 1000)
    mcps = []
    for target, items in sorted(by_target.items()):
        before = graph.get_aspect(target, "upstreamLineage") or {}
        upstreams = list(before.get("upstreams") or [])
        known_up = {u.get("dataset") for u in upstreams}
        fine = list(before.get("fineGrainedLineages") or [])
        known_fine = {(tuple(sorted(f.get("upstreams") or [])), tuple(f.get("downstreams") or [])) for f in fine}
        changed = False
        for e in items:
            for ds, _ in e.sources:
                if ds not in known_up:
                    upstreams.append({"dataset": ds, "type": "TRANSFORMED", "auditStamp": {"time": now, "actor": actor}})
                    known_up.add(ds)
                    changed = True
            ups = tuple(sorted({_field(ds, c) for ds, c in e.sources}))
            key = (ups, (_field(target, e.column),))
            if key in known_fine:
                continue
            known_fine.add(key)
            entry = {"upstreamType": "FIELD_SET", "upstreams": list(ups), "downstreamType": "FIELD",
                     "downstreams": [_field(target, e.column)], "transformOperation": e.operation, "confidenceScore": 0.9}
            if e.query:
                entry["query"] = e.query
            fine.append(entry)
            changed = True
        if changed:
            mcps.append(make_mcp(target, "upstreamLineage", {**before, "upstreams": upstreams,
                                                              "fineGrainedLineages": fine}))
    return mcps


def publish(snap: Snapshot, graph) -> dict:
    edges = extract(snap)
    mcps = to_mcps(edges, graph)
    if mcps:
        graph.emit(mcps)
    return {"column_edges": len(edges), "datasets_updated": len(mcps)}
