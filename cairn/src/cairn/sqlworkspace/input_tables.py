"""Input tables: lakehouse tables people type into, from Cairn, governed like any write.

``POST /sql/input-tables`` with ``connection`` (a Trino connection with ``run_as_user``), ``table``
(``schema.table``) and ``columns`` (``[{"name": "target", "type": "number"}]``; types ``text``, ``number``,
``integer``, ``boolean``, ``date``, ``timestamp``) creates an Iceberg table with those columns and three of
Cairn's: ``_row_id`` (each row's id), ``_updated_by`` and ``_updated_at``.

* **Rows**: ``GET …/<id>/rows`` reads them as the viewer (masks apply); ``POST …/<id>/rows`` adds rows;
  ``PATCH …/<id>/rows/<row_id>`` changes cells, with ``expected_updated_at`` (the row's ``_updated_at`` as read):
  if someone changed the row since, nothing is written and the answer is 409; ``DELETE …/<id>/rows`` removes rows.
* **Governed as the person.** Every statement runs on Trino as the person writing, so Cairn's policies decide
  there (creating needs the right to create the table; writing needs ``WRITE_DATASET`` on it). Values are
  checked against their column's type and written as typed literals, never as SQL.
* **Audited**: creating a table and each change (``input.created``, ``input.rows_added``, ``input.row_changed``,
  ``input.rows_deleted``) with the row ids and columns changed, not the values. Every commit is a change of the
  table, so workbooks, derived tables and materialized views reading it follow.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, String, Table, Text, delete, insert, select

from cairn.common.db import metadata
from cairn.sqlworkspace.formulas import ident, literal

TYPES = {"text": "varchar", "number": "double", "integer": "bigint", "boolean": "boolean", "date": "date",
         "timestamp": "timestamp(6) with time zone"}
NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
TABLE = re.compile(r"^[a-z][a-z0-9_]{0,62}\.[a-z][a-z0-9_]{0,62}$")
SYSTEM = ("_row_id", "_updated_by", "_updated_at")
MAX_ROWS = 500

input_tables = Table(
    "cairn_input_tables", metadata,
    Column("id", String(32), primary_key=True),
    Column("connection", String(128), nullable=False),
    Column("table_name", String(200), nullable=False, unique=True),
    Column("columns", Text, nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("created_by", String(256), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)


class InputTableError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def typed_literal(value: Any, kind: str, column: str) -> str:
    """``value`` as a SQL literal of the column's type; anything that is not one is refused."""
    if value is None or value == "":
        return f"CAST(NULL AS {TYPES[kind]})"
    try:
        if kind == "text":
            if not isinstance(value, (str, int, float)) or isinstance(value, bool):
                raise ValueError
            text = str(value)
            if len(text) > 10_000:
                raise InputTableError(f"{column}: at most 10,000 characters")
            return literal(text)
        if kind in ("number", "integer"):
            if isinstance(value, bool):
                raise ValueError
            n = float(value) if kind == "number" else int(str(value))
            if kind == "number" and not math.isfinite(n):
                raise ValueError
            return f"CAST({n!r} AS double)" if kind == "number" else f"CAST({n} AS bigint)"
        if kind == "boolean":
            if isinstance(value, bool):
                return "TRUE" if value else "FALSE"
            if str(value).lower() in ("true", "false"):
                return str(value).upper()
            raise ValueError
        if kind == "date":
            return f"DATE {literal(date.fromisoformat(str(value)).isoformat())}"
        if kind == "timestamp":
            return stamp_literal(str(value))
    except (ValueError, TypeError, OverflowError):
        raise InputTableError(f"{column}: {value!r} is not a {kind}")
    raise InputTableError(f"{column}: unknown type {kind}")


def stamp_literal(text: str) -> str:
    """An ISO time with its zone as a ``timestamp(6) with time zone`` literal (microseconds kept, so a value read
    back compares equal)."""
    t = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError("no time zone")
    return f"TIMESTAMP '{t.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')} UTC'"


class InputTables:
    def __init__(self, engine, run: Callable[[str, str, str], tuple[list, list]], read: Callable[[str, str, str, Optional[str], int], dict],
                 audit=None, register: Optional[Callable[[str, str, list, str, str], None]] = None):
        """``run(connection, actor, sql)``: a statement on Trino as ``actor`` (columns, rows); ``read(actor, sql,
        connection, urn, limit)``: a governed read (masks applied); ``register(connection, table, columns, owner,
        description)``: puts the new table in Cairn's catalog (owned by its creator), so it can be read at once."""
        self.engine, self.run, self.read, self.audit, self.register = engine, run, read, audit, register

    def _get(self, tid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(input_tables).where(input_tables.c.id == tid)).first()
        if r is None:
            raise InputTableError("no such input table", 404)
        return {**dict(r._mapping), "columns": json.loads(r.columns)}

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [{**dict(r._mapping), "columns": json.loads(r.columns)}
                    for r in conn.execute(select(input_tables).order_by(input_tables.c.table_name))]

    def create(self, actor: str, body: dict) -> dict:
        connection, table = str(body.get("connection") or ""), str(body.get("table") or "").lower()
        if not TABLE.match(table):
            raise InputTableError("table: schema.table (lowercase letters, digits and _)")
        cols = body.get("columns")
        if not isinstance(cols, list) or not 1 <= len(cols) <= 100:
            raise InputTableError("columns: 1 to 100 of {name, type}")
        seen = set()
        for c in cols:
            name, kind = str((c or {}).get("name") or ""), (c or {}).get("type")
            if not NAME.match(name) or name in SYSTEM or name in seen:
                raise InputTableError(f"column {name!r}: a new lowercase name (letters, digits, _)")
            if kind not in TYPES:
                raise InputTableError(f"column {name}: type is one of {', '.join(TYPES)}")
            seen.add(name)
        with self.engine.connect() as conn:
            if conn.execute(select(input_tables.c.id).where(input_tables.c.table_name == table)).first():
                raise InputTableError(f"{table} is already an input table", 409)
        schema, name = table.split(".")
        defs = [f"{ident(c['name'])} {TYPES[c['type']]}" for c in cols]
        defs += ['"_row_id" varchar', '"_updated_by" varchar', '"_updated_at" timestamp(6) with time zone']
        self.run(connection, actor, f"CREATE TABLE {ident(schema)}.{ident(name)} ({', '.join(defs)})")
        description = str(body.get("description") or "")
        if self.register is not None:
            self.register(connection, table, [{"name": c["name"], "type": c["type"]} for c in cols], actor, description)
        tid = uuid.uuid4().hex
        with self.engine.begin() as conn:
            conn.execute(insert(input_tables).values(
                id=tid, connection=connection, table_name=table, created_by=actor, created_ms=int(time.time() * 1000),
                columns=json.dumps([{"name": c["name"], "type": c["type"]} for c in cols]),
                description=description))
        self._audit(actor, "input.created", table, {"columns": [c["name"] for c in cols]})
        return self._get(tid)

    def rows(self, actor: str, tid: str, limit: int = 1000) -> dict:
        t = self._get(tid)
        schema, name = t["table_name"].split(".")
        names = ", ".join(ident(c["name"]) for c in t["columns"])
        sql = (f'SELECT "_row_id", {names}, "_updated_by", "_updated_at" FROM {ident(schema)}.{ident(name)} '
               'ORDER BY "_updated_at", "_row_id"')
        out = self.read(actor, sql, t["connection"], None, max(1, min(limit, 10_000)))
        return {**out, "table": t}

    def add(self, actor: str, tid: str, rows: list) -> dict:
        t = self._get(tid)
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
            raise InputTableError(f"rows: 1 to {MAX_ROWS}")
        cols = t["columns"]
        known = {c["name"] for c in cols}
        values, ids = [], []
        for r in rows:
            if not isinstance(r, dict) or set(r) - known:
                raise InputTableError(f"a row has columns of the table only ({', '.join(sorted(known))})")
            rid = uuid.uuid4().hex
            ids.append(rid)
            values.append("(" + ", ".join([typed_literal(r.get(c["name"]), c["type"], c["name"]) for c in cols]
                                          + [literal(rid), literal(actor), "current_timestamp"]) + ")")
        schema, name = t["table_name"].split(".")
        names = ", ".join([ident(c["name"]) for c in cols] + ['"_row_id"', '"_updated_by"', '"_updated_at"'])
        self.run(t["connection"], actor, f"INSERT INTO {ident(schema)}.{ident(name)} ({names}) VALUES {', '.join(values)}")
        self._audit(actor, "input.rows_added", t["table_name"], {"rows": ids})
        return {"added": ids}

    def change(self, actor: str, tid: str, row_id: str, values: dict, expected_updated_at: Optional[str]) -> dict:
        t = self._get(tid)
        types = {c["name"]: c["type"] for c in t["columns"]}
        if not isinstance(values, dict) or not values or set(values) - set(types):
            raise InputTableError(f"values: columns of the table ({', '.join(sorted(types))})")
        if not re.fullmatch(r"[0-9a-f]{32}", row_id or ""):
            raise InputTableError("no such row", 404)
        sets = [f"{ident(k)} = {typed_literal(v, types[k], k)}" for k, v in values.items()]
        sets += [f'"_updated_by" = {literal(actor)}', '"_updated_at" = current_timestamp']
        where = f'"_row_id" = {literal(row_id)}'
        if expected_updated_at:
            try:
                where += f' AND "_updated_at" = {stamp_literal(str(expected_updated_at))}'
            except ValueError:
                raise InputTableError("expected_updated_at: the row's _updated_at as read")
        schema, name = t["table_name"].split(".")
        _, rows = self.run(t["connection"], actor, f"UPDATE {ident(schema)}.{ident(name)} SET {', '.join(sets)} WHERE {where}")
        changed = int(rows[0][0]) if rows and rows[0] else 0
        if not changed:
            _, there = self.run(t["connection"], actor,
                                f'SELECT count(*) FROM {ident(schema)}.{ident(name)} WHERE "_row_id" = {literal(row_id)}')
            if there and int(there[0][0]):
                raise InputTableError("someone changed this row since you read it: read it again", 409)
            raise InputTableError("no such row", 404)
        self._audit(actor, "input.row_changed", t["table_name"], {"row": row_id, "columns": sorted(values)})
        return {"changed": row_id}

    def remove(self, actor: str, tid: str, row_ids: list) -> dict:
        t = self._get(tid)
        if not isinstance(row_ids, list) or not row_ids or len(row_ids) > MAX_ROWS or \
                not all(isinstance(r, str) and re.fullmatch(r"[0-9a-f]{32}", r) for r in row_ids):
            raise InputTableError(f"rows: 1 to {MAX_ROWS} row ids")
        schema, name = t["table_name"].split(".")
        _, rows = self.run(t["connection"], actor, f'DELETE FROM {ident(schema)}.{ident(name)} WHERE "_row_id" IN '
                                                   f"({', '.join(literal(r) for r in row_ids)})")
        n = int(rows[0][0]) if rows and rows[0] else 0
        self._audit(actor, "input.rows_deleted", t["table_name"], {"rows": row_ids, "deleted": n})
        return {"deleted": n}

    def forget(self, actor: str, tid: str, is_operator: bool) -> None:
        """Stops listing the table as an input table (its data stays in the lakehouse)."""
        t = self._get(tid)
        if t["created_by"] != actor and not is_operator:
            raise InputTableError("only its creator or an operator removes an input table", 403)
        with self.engine.begin() as conn:
            conn.execute(delete(input_tables).where(input_tables.c.id == tid))
        self._audit(actor, "input.forgotten", t["table_name"], {})

    def _audit(self, actor: str, action: str, table: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("sql", action, actor, table, details)


def build_input_tables_router(it: InputTables, current_principal, is_operator) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    from cairn.dataaccess.service import DataAccessError
    from cairn.sqlworkspace.workspace import WorkspaceError

    router = APIRouter(prefix="/sql/input-tables", tags=["sql"])

    def person(p) -> str:
        if p.is_service:
            raise HTTPException(403, "input tables are written by people")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except (InputTableError, WorkspaceError, DataAccessError) as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    async def list_(p=Depends(current_principal)):
        person(p)
        return await call(it.list)

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: it.create(actor, body))

    @router.get("/{tid}/rows")
    async def rows(tid: str, limit: int = 1000, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: it.rows(actor, tid, limit))

    @router.post("/{tid}/rows", status_code=201)
    async def add(tid: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: it.add(actor, tid, body.get("rows")))

    @router.patch("/{tid}/rows/{row_id}")
    async def change(tid: str, row_id: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: it.change(actor, tid, row_id, body.get("values"), body.get("expected_updated_at")))

    @router.delete("/{tid}/rows")
    async def remove(tid: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: it.remove(actor, tid, body.get("rows")))

    @router.delete("/{tid}", status_code=204)
    async def forget(tid: str, p=Depends(current_principal)):
        actor = person(p)
        await call(lambda: it.forget(actor, tid, is_operator(p)))

    return router
