"""Streaming transformations: a SQL SELECT applied to each micro-batch of a stream before it lands.

A stream's ``transform`` is one SELECT over the relation ``batch`` (the batch's incoming rows, one column per
field, nested objects as structs): projection, renames, casts, filters and row-level expressions::

    SELECT id, lower(email) AS email, CAST(amount AS DECIMAL(12, 2)) AS amount, ts::TIMESTAMP AS ts
    FROM batch WHERE status <> 'test'

It runs in-process on the batch with DuckDB, so transformed rows reach the table in the same commit as before,
within seconds, instead of waiting for a derived table's schedule. Each output row comes from exactly one input
row, so the statement may not join, aggregate, group, de-duplicate, window, unnest, limit, use subqueries, CTEs or
set operations, or read anything but ``batch`` (no other tables: their access could not be governed here).

The engine is sandboxed: an in-memory database with no file system or network access, no extension loading, one
thread, a memory limit, and its configuration locked before the user's SQL runs.

Rows: a row the ``WHERE`` drops is skipped (filtering is what was asked). A row the statement fails on (a cast that
does not convert, a function error) is set aside in the stream's rejects with the reason, and the rest of the batch
goes on: the batch is split in halves until the failing rows are found.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Optional

BATCH = "batch"
COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_SQL = 20_000
MEMORY = os.environ.get("CAIRN_STREAMS_TRANSFORM_MEMORY", "512MB")


class TransformError(ValueError):
    pass


def _parse(sql: str):
    import sqlglot
    from sqlglot import exp

    if not isinstance(sql, str) or not sql.strip():
        raise TransformError("transform: one SELECT over batch")
    if len(sql) > MAX_SQL:
        raise TransformError(f"transform: at most {MAX_SQL} characters")
    try:
        statements = [s for s in sqlglot.parse(sql.strip().rstrip(";"), read="duckdb") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise TransformError(f"transform does not parse: {str(e).splitlines()[0]}")
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise TransformError("transform is one SELECT over batch (no other statements, no UNION)")
    tree = statements[0]
    forbidden = [(exp.With, "a WITH clause"), (exp.Join, "a join"), (exp.Group, "GROUP BY"), (exp.Having, "HAVING"),
                 (exp.Distinct, "DISTINCT"), (exp.Window, "a window function"), (exp.AggFunc, "an aggregate"),
                 (exp.Limit, "LIMIT"), (exp.Offset, "OFFSET"), (exp.Order, "ORDER BY"), (exp.Unnest, "UNNEST"),
                 (exp.Lateral, "LATERAL"), (exp.Qualify, "QUALIFY"), (exp.Pivot, "PIVOT"), (exp.Into, "INTO")]
    for kind, what in forbidden:
        if tree.find(kind) is not None:
            raise TransformError(f"transform: {what} is not allowed (each output row comes from one input row)")
    if len(list(tree.find_all(exp.Select))) != 1 or tree.find(exp.Subquery) is not None:
        raise TransformError("transform: no subqueries; it reads batch only")
    tables = list(tree.find_all(exp.Table))
    src = tree.args.get("from_") or tree.args.get("from")
    if (len(tables) != 1 or src is None or tables[0].name.lower() != BATCH or tables[0].args.get("db")
            or tables[0].args.get("catalog")):
        raise TransformError("transform reads FROM batch (the incoming rows) and nothing else")
    if tables[0].alias and tables[0].alias.lower() != BATCH:
        raise TransformError("transform: batch takes no alias")
    return tree


def outputs(tree) -> tuple[list[str], bool]:
    """The output column names the statement names, and whether it also passes columns through with ``*``."""
    from sqlglot import exp

    names, star = [], False
    for e in tree.expressions:
        if isinstance(e, exp.Star) or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star)):
            star = True
            continue
        name = e.alias_or_name if isinstance(e, (exp.Alias, exp.Column)) else ""
        if not name or not COLUMN.match(name):
            raise TransformError(f"transform: name every computed column with AS (a column name), not {e.sql('duckdb')!r}")
        names.append(name)
    if len({n.lower() for n in names}) != len(names):
        raise TransformError("transform: two output columns have the same name")
    return names, star


def _excluded(tree) -> set[str]:
    out = set()
    for e in tree.expressions:
        ex = e.args.get("except_") or e.args.get("except") or []
        out |= {c.name.lower() for c in ex}
    return out


def referenced(tree) -> list[str]:
    """The input columns the statement reads (the first part of each column reference)."""
    from sqlglot import exp

    out: dict[str, None] = {}
    for c in tree.find_all(exp.Column):
        parts = [p.name for p in c.parts]
        if not parts or isinstance(c.this, exp.Star):
            continue
        if parts[0].lower() == BATCH and len(parts) > 1:
            parts = parts[1:]
        out[parts[0]] = None
    return list(out)


def check(sql: str, key: Optional[list[str]] = None) -> str:
    """The transform, checked: it parses as one row-for-row SELECT over batch, names its columns, keeps the
    stream's key columns, and binds in the engine (functions exist, casts are valid types)."""
    tree = _parse(sql)
    names, star = outputs(tree)
    lowered = {n.lower() for n in names}
    for k in key or []:
        if k.lower() not in lowered and (not star or k.lower() in _excluded(tree)):
            raise TransformError(f"transform: the key column {k} must be in its output (upserts find rows by it)")
    import pyarrow as pa

    cols = referenced(tree) or ["_"]
    probe = pa.table({c: pa.array([None], pa.null()) for c in cols})
    try:
        _run(sql, probe)
    except Exception as e:  # noqa: BLE001 - the engine's own message, shown to whoever defines it
        raise TransformError(f"transform does not run: {_reason(e)}")
    return sql.strip().rstrip(";")


def _connect():
    import duckdb

    con = duckdb.connect(":memory:", config={
        "enable_external_access": False, "autoinstall_known_extensions": False, "autoload_known_extensions": False,
        "threads": 1, "memory_limit": MEMORY, "preserve_insertion_order": True})
    con.execute("SET lock_configuration = true")       # the user's SQL cannot turn the sandbox off
    return con


def _run(sql: str, table):
    con = _connect()
    try:
        con.register(BATCH, table)
        return con.sql(sql).to_arrow_table()
    finally:
        con.close()


def _reason(e: Exception) -> str:
    return (str(e).splitlines() or [type(e).__name__])[0][:500]


def _arrow(rows: list[dict]):
    """The rows as an arrow table: a column whose values do not share a type becomes text (JSON for objects)."""
    import pyarrow as pa

    cols: dict[str, None] = {}
    for r in rows:
        cols.update(dict.fromkeys(r))
    arrays = {}
    for c in cols:
        values = [r.get(c) for r in rows]
        try:
            arr = pa.array(values)
        except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError, OverflowError):
            arr = pa.array([None if v is None else json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
                            for v in values], pa.string())
        arrays[c] = arr
    return pa.table(arrays) if arrays else pa.table({"_": pa.array([None] * len(rows), pa.null())})


def apply(sql: str, rows: list[dict]):
    """Runs the transform on a batch: ``(kept, failed, schema)``, kept as ``(input index, output row)`` in input
    order, failed as ``(input index, reason)``, and the output's arrow schema (the types the statement gives, for a
    table the stream creates). Rows the WHERE drops are in neither."""
    if not rows:
        return [], [], None
    tree = _parse(sql)
    where = tree.args.get("where")
    keep_sql = f"SELECT ({where.this.sql('duckdb')}) IS TRUE AS keep FROM {BATCH}" if where is not None else None
    kept: list[tuple[int, dict]] = []
    failed: list[tuple[int, str]] = []
    schema: list = []

    def run(table, at: int) -> None:
        try:
            result = _run(sql, table)
            out = result.to_pylist()
            schema.append(result.schema)
            mask = [r["keep"] for r in _run(keep_sql, table).to_pylist()] if keep_sql else [True] * table.num_rows
            idx = [at + i for i, k in enumerate(mask) if k]
            if len(idx) != len(out):
                raise RuntimeError("the transform's output does not line up with its input")
            kept.extend(zip(idx, out))
        except Exception as e:  # noqa: BLE001 - find the rows it fails on; the others go on
            if table.num_rows == 1:
                failed.append((at, f"transform: {_reason(e)}"))
                return
            half = table.num_rows // 2       # copies, not slices: the engine refuses offset nested columns
            run(table.take(list(range(half))), at)
            run(table.take(list(range(half, table.num_rows))), at + half)

    run(_arrow(rows), 0)
    kept.sort(key=lambda x: x[0])
    return kept, failed, (schema[0] if schema else None)


def transform_ops(sql: str, ops: list[tuple[str, dict, Any, Any]], key: list[str],
                  reject: Callable[[Any, Any, str, dict], None]):
    """A stream batch's ``(op, row, partition, offset)`` after the transform, and the output's arrow schema:
    upserts transformed (dropped when filtered, rejected when the transform fails on them or loses a key), deletes
    passed through by key."""
    at = [i for i, op in enumerate(ops) if op[0] == "upsert"]
    kept, failed, schema = apply(sql, [ops[i][1] for i in at])
    out = {i: op for i, op in enumerate(ops) if op[0] != "upsert"}
    for j, reason in failed:
        _, r, p, o = ops[at[j]]
        reject(p, o, reason, r)
    for j, row in kept:
        _, r, p, o = ops[at[j]]
        if key and any(row.get(k) is None for k in key):
            reject(p, o, f"transform: no value for the key {', '.join(key)} in its output", r)
            continue
        out[at[j]] = ("upsert", row, p, o)
    return [out[i] for i in sorted(out)], schema
