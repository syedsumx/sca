"""Moving tables into the lakehouse, and proving they arrived whole (L170).

A **move** copies tables from a source that Trino reaches as a catalog (Snowflake through its connector, Databricks'
Delta tables on object storage through the Delta Lake connector, PostgreSQL and the other federated databases) into
Iceberg tables in Cairn's lakehouse, and verifies each one.

* ``POST /v1/migrate/moves`` ``{"connection": "<Cairn's Trino>", "source_catalog": "delta", "schemas": ["sales"],
  "tables": ["sales.orders"], "target_schema_prefix": ""}`` plans it: the source's tables (read from its
  ``information_schema`` as the caller) with their columns, each to ``<lakehouse>.<prefix><schema>.<table>``.
  Nothing is copied yet.
* ``POST /v1/migrate/moves/<id>/run`` copies and verifies the next tables (``max_tables``, 20 by default; call it
  again until ``remaining`` is 0). Each table: ``CREATE TABLE ... AS SELECT *`` on Trino **as the caller**, then one
  query on each side for the row count and an order-independent checksum of every column. Equal: ``verified``.
  Different: ``mismatch``, with the columns that differ; the copy is kept for a person to look at, never reported
  as moved. ``"replace": true`` copies a table again (the source changed, or a mismatch was understood).
* ``GET /v1/migrate/moves/<id>`` shows every table's state.

A move never copies less than the table. A table to which a column mask or a row filter applies **for the caller**
is refused: the copy would hold masked values or miss rows, and would then be read as the real table. Move it as
someone the policies let read it whole. A target that already exists is never overwritten unless ``replace``.
Types Iceberg lacks are converted by Trino where it can; a table it cannot create is ``failed`` with Trino's reason.

Needs ``MANAGE_INGESTION`` (and, on Trino, the right to read the source and create the target, as for any query).
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, insert, select, update

from cairn.common.db import metadata
from cairn.migrate.service import MigrateError

logger = logging.getLogger(__name__)

IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,127}$")
MAX_TABLES = 2000
STATES = ("pending", "verified", "mismatch", "failed", "refused")

moves = Table(
    "cairn_migrate_moves", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("connection", String(128), nullable=False),
    Column("source_catalog", String(128), nullable=False),
    Column("target_catalog", String(128), nullable=False),
    Column("tables", Text, nullable=False),          # JSON [{source, target, columns, state, rows, error, ...}]
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)


def q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def fq(catalog: str, table: str) -> str:
    schema, name = table.split(".", 1)
    return f"{q(catalog)}.{q(schema)}.{q(name)}"


class Moves:
    def __init__(self, engine, run: Callable[[str, str, str], list], restricted: Callable[[str, str, str, str], bool],
                 target_catalog: str = "lakehouse", audit=None):
        """``run(connection, actor, sql)``: a statement on Trino as ``actor``, its rows;
        ``restricted(actor, catalog, schema, table)``: does a mask or row filter apply to the actor there (or can it
        not be decided)."""
        self.engine, self.run, self.restricted, self.target_catalog, self.audit = engine, run, restricted, target_catalog, audit

    # ---- plan ------------------------------------------------------------------------------------------
    def plan(self, actor: str, body: dict) -> dict:
        connection, source = str(body.get("connection") or ""), str(body.get("source_catalog") or "").lower()
        prefix = str(body.get("target_schema_prefix") or "").lower()
        schemas = [str(s).lower() for s in body.get("schemas") or []]
        wanted = [str(t).lower() for t in body.get("tables") or []]
        if not connection or not IDENT.match(source) or source == self.target_catalog:
            raise MigrateError("connection (Cairn's Trino) and source_catalog (the source, as Trino names it) are needed")
        if prefix and not IDENT.match(prefix):
            raise MigrateError("target_schema_prefix: lowercase letters, digits and _")
        if not schemas and not wanted:
            raise MigrateError("schemas or tables: what to move")
        if any(not IDENT.match(s) for s in schemas) or any(
                t.count(".") != 1 or not all(IDENT.match(p) for p in t.split(".")) for t in wanted):
            raise MigrateError("schemas are names, tables are schema.table (lowercase letters, digits and _)")
        names = sorted(set(schemas) | {t.split(".")[0] for t in wanted})
        inlist = ", ".join("'" + s + "'" for s in names)
        try:
            rows = self.run(connection, actor,
                            "SELECT c.table_schema, c.table_name, c.column_name, c.data_type "
                            f"FROM {q(source)}.information_schema.columns c "
                            f"JOIN {q(source)}.information_schema.tables t ON t.table_schema = c.table_schema AND "
                            f"t.table_name = c.table_name WHERE t.table_type = 'BASE TABLE' AND c.table_schema IN ({inlist}) "
                            "ORDER BY c.table_schema, c.table_name, c.ordinal_position")
        except Exception as e:  # noqa: BLE001 - Trino's reason, as for any query
            raise MigrateError(f"the source's tables could not be listed: {str(e).splitlines()[0][:300]}", 502)
        found: dict[str, list] = {}
        for schema, table, column, dtype in rows:
            full = f"{schema}.{table}"
            if schema in schemas or full in wanted:
                found.setdefault(full, []).append([str(column), str(dtype)])
        missing = [t for t in wanted if t not in found]
        if missing:
            raise MigrateError(f"not in {source} (or not yours to see): {', '.join(missing)}", 404)
        if not found or len(found) > MAX_TABLES:
            raise MigrateError(f"between 1 and {MAX_TABLES} tables at a time ({len(found)} found)")
        tables = []
        for full, columns in sorted(found.items()):
            schema, name = full.split(".", 1)
            entry = {"source": full, "target": f"{prefix}{schema}.{name}", "columns": columns, "state": "pending"}
            if not all(IDENT.match(p) for p in (schema, name)) or not all(IDENT.match(c[0].lower()) for c in columns):
                entry.update(state="refused", error="a schema, table or column name is not lowercase letters, digits "
                                                    "and _: rename it in a view at the source, or move it by hand")
            tables.append(entry)
        now = int(time.time() * 1000)
        row = {"id": uuid.uuid4().hex, "owner": actor, "connection": connection, "source_catalog": source,
               "target_catalog": self.target_catalog, "tables": json.dumps(tables), "created_ms": now, "updated_ms": now}
        with self.engine.begin() as conn:
            conn.execute(insert(moves).values(**row))
            if self.audit is not None:
                self.audit.record("migrate", "migrate.move_planned", actor, None,
                                  {"move": row["id"], "source_catalog": source, "tables": len(tables)}, conn=conn)
        return self._public(row)

    def _row(self, mid: str, actor: str, everyone: bool) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(moves).where(moves.c.id == mid)).first()
        if r is None or not (everyone or r.owner == actor):
            raise MigrateError("no such move", 404)
        return dict(r._mapping)

    @staticmethod
    def _public(row: dict) -> dict:
        tables = json.loads(row["tables"])
        counts = {s: sum(1 for t in tables if t["state"] == s) for s in STATES}
        return {"id": row["id"], "owner": row["owner"], "connection": row["connection"],
                "source_catalog": row["source_catalog"], "target_catalog": row["target_catalog"], "tables": tables,
                "summary": {"tables": len(tables), **counts, "remaining": counts["pending"]},
                "created_ms": row["created_ms"], "updated_ms": row["updated_ms"]}

    def get(self, mid: str, actor: str, everyone: bool) -> dict:
        return self._public(self._row(mid, actor, everyone))

    # ---- copy and verify -------------------------------------------------------------------------------
    def _fingerprint(self, connection: str, actor: str, catalog: str, table: str, columns: list,
                     as_types: Optional[dict] = None) -> list:
        """The row count and each column's checksum (order-independent; NULL for no rows), as text. ``as_types``:
        columns read as another type first (the source's, as the type the lakehouse keeps them in)."""
        as_types = as_types or {}
        parts = ", ".join(f"to_hex(checksum(CAST({q(c)} AS {as_types[c]})))" if c in as_types else f"to_hex(checksum({q(c)}))"
                          for c, _ in columns)
        (row,) = self.run(connection, actor, f"SELECT count(*), {parts} FROM {fq(catalog, table)}")
        return [str(v) if v is not None else None for v in row]

    def _move_one(self, row: dict, t: dict, actor: str, replace: bool) -> dict:
        connection, src, dst = row["connection"], row["source_catalog"], row["target_catalog"]
        schema, name = t["source"].split(".", 1)
        t = {k: v for k, v in t.items() if k not in ("error", "differs")}
        try:
            if self.restricted(actor, src, schema, name):
                return {**t, "state": "refused", "error": "a column mask or row filter applies to you on this table: the "
                                                          "copy would not be the table. Move it as someone who reads it whole"}
        except Exception as e:  # noqa: BLE001 - fail closed
            return {**t, "state": "refused", "error": f"the policies on this table could not be decided ({type(e).__name__})"}
        started = time.time()
        try:
            tschema = t["target"].split(".", 1)[0]
            self.run(connection, actor, f"CREATE SCHEMA IF NOT EXISTS {q(dst)}.{q(tschema)}")
            cols = ", ".join(q(c) for c, _ in t["columns"])
            create = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE"
            self.run(connection, actor, f"{create} {fq(dst, t['target'])} AS SELECT {cols} FROM {fq(src, t['source'])}")
        except Exception as e:  # noqa: BLE001 - Trino's reason on the table
            why = str(e).splitlines()[0][:400]
            if "already exists" in why.lower():
                why += " (not overwritten: run with replace to copy it again)"
            return {**t, "state": "failed", "error": why}
        try:
            # Iceberg lacks some types (a timestamp's precision, small integers): Trino keeps those columns in the
            # nearest one. Both sides are compared in the type the lakehouse keeps, and the conversions are listed.
            kept = {str(r[0]): str(r[1]) for r in self.run(connection, actor, f"SHOW COLUMNS FROM {fq(dst, t['target'])}")}
            converted = {c: kept[c] for c, typ in t["columns"] if c in kept and kept[c] != typ}
            a = self._fingerprint(connection, actor, src, t["source"], t["columns"], converted)
            b = self._fingerprint(connection, actor, dst, t["target"], t["columns"])
        except Exception as e:  # noqa: BLE001 - copied but not proven: never "verified"
            return {**t, "state": "failed", "error": f"copied, but not verified: {str(e).splitlines()[0][:300]}"}
        out = {**t, "rows": int(b[0] or 0), "source_rows": int(a[0] or 0), "seconds": round(time.time() - started, 1),
               "converted_types": {c: f"{typ} → {kept[c]}" for c, typ in t["columns"] if c in converted}}
        if a == b:
            return {**out, "state": "verified"}
        differs = (["row count"] if a[0] != b[0] else []) + [c for (c, _), x, y in zip(t["columns"], a[1:], b[1:]) if x != y]
        return {**out, "state": "mismatch", "differs": differs,
                "error": "the copy is not the source (it changed while copying, or a type lost values): "
                         + ", ".join(differs[:20])}

    def run_next(self, mid: str, actor: str, everyone: bool, max_tables: int = 20, replace: bool = False,
                 only: Optional[list] = None) -> dict:
        row = self._row(mid, actor, everyone)
        if not 1 <= int(max_tables) <= 200:
            raise MigrateError("max_tables: between 1 and 200")
        tables = json.loads(row["tables"])
        only = {str(x).lower() for x in only or []}
        # Pending tables; with replace, also those that did not verify (and verified ones only when named).
        again = ("mismatch", "failed", "verified") if only else ("mismatch", "failed")
        todo = [i for i, t in enumerate(tables) if (not only or t["source"] in only)
                and (t["state"] == "pending" or (replace and t["state"] in again))]
        for i in todo[:int(max_tables)]:
            tables[i] = self._move_one(row, tables[i], actor, replace)
            with self.engine.begin() as conn:                # each table as it ends: a stopped run loses nothing
                conn.execute(update(moves).where(moves.c.id == mid).values(tables=json.dumps(tables),
                                                                           updated_ms=int(time.time() * 1000)))
                if self.audit is not None:
                    t = tables[i]
                    self.audit.record("migrate", "migrate.table_moved", actor, None,
                                      {"move": mid, "source": f"{row['source_catalog']}.{t['source']}",
                                       "target": f"{row['target_catalog']}.{t['target']}", "state": t["state"],
                                       "rows": t.get("rows")}, conn=conn)
        return self._public({**row, "tables": json.dumps(tables), "updated_ms": int(time.time() * 1000)})


def build_moves_router(mv: Moves, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    from cairn.migrate.service import PRIVILEGE

    router = APIRouter(prefix="/v1/migrate/moves", tags=["migrate"])

    def person(p):
        authorize(p, PRIVILEGE, None)
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "tables are moved by a person, as that person on Trino")
        return p

    def operator(p) -> bool:
        return bool(authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))

    def call(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except MigrateError as e:
            raise HTTPException(e.status, str(e))

    @router.post("")
    def plan(body: dict = Body(...), p=Depends(current_principal)):
        """Plans a move: the source's tables and where each goes. Nothing is copied."""
        return call(mv.plan, person(p).urn, body)

    @router.get("/{mid}")
    def get(mid: str, p=Depends(current_principal)):
        authorize(p, PRIVILEGE, None)
        return call(mv.get, mid, p.urn, operator(p))

    @router.post("/{mid}/run")
    def run(mid: str, max_tables: int = Body(20), replace: bool = Body(False), tables: Optional[list] = Body(None),
            p=Depends(current_principal)):
        """Copies and verifies the next tables, as the caller. Call again until ``remaining`` is 0."""
        p = person(p)
        return call(mv.run_next, mid, p.urn, operator(p), max_tables, replace, tables)

    return router
