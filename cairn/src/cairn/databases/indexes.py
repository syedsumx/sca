"""Indexes of Cairn's operational databases, as resources (``/v1/databases/<name>/indexes``, kind
``postgres_indexes``): how an index the SRE agent suggests (L142) reaches a database: as a change request a person
approves and applies, never by the agent.

* ``GET /v1/databases/<name>/indexes`` lists the indexes on the database's tables (``VIEW_DATABASE``, ``USE_DATABASE``
  or ``MANAGE_DATABASE``); each says whether it backs a constraint or has expressions (``managed`` is false then:
  those are the schema's own, not resources).
* ``PUT /v1/databases/<name>/indexes/<index>`` ``{"table", "columns", "method", "unique"}`` builds one with
  ``CREATE INDEX CONCURRENTLY`` (writes go on while it builds); the same definition again is a no-op, a different
  one is refused (an index is replaced, not changed). ``DELETE`` drops one ``CONCURRENTLY``. Both need
  ``MANAGE_DATABASE`` on ``urn:li:pgDatabase:<name>`` and are audited (``database.index_created``,
  ``database.index_dropped``). An index that failed to build (left invalid) is dropped again.

Nothing here takes SQL from a caller: names are checked and quoted, the method is one of a list."""

from __future__ import annotations

import logging
import re
from typing import Any

from cairn.dataaccess.databases import DatabaseError, Databases, urn

logger = logging.getLogger(__name__)

IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
METHODS = ("btree", "hash", "gin", "gist", "brin")
LIST_SQL = """
SELECT n.nspname, t.relname, i.relname, am.amname, ix.indisunique, ix.indisvalid,
       array(SELECT a.attname::text FROM unnest(ix.indkey) WITH ORDINALITY k(attnum, ord)
             JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum ORDER BY k.ord),
       (ix.indexprs IS NOT NULL OR ix.indpred IS NOT NULL),
       EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.oid),
       pg_relation_size(i.oid)
FROM pg_index ix JOIN pg_class i ON i.oid = ix.indexrelid JOIN pg_class t ON t.oid = ix.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_am am ON am.oid = i.relam
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND n.nspname NOT LIKE 'pg\\_toast%%'
ORDER BY n.nspname, t.relname, i.relname"""


def _ident(v: Any, what: str) -> str:
    s = str(v or "")
    if not IDENT.match(s):
        raise DatabaseError(f"{what}: lowercase letters, digits and '_' (up to 63), starting with a letter or '_'", 422)
    return s


def table_name(v: Any) -> tuple[str, str]:
    parts = str(v or "").split(".")
    if len(parts) == 1:
        return "public", _ident(parts[0], "table")
    if len(parts) == 2:
        return _ident(parts[0], "table's schema"), _ident(parts[1], "table")
    raise DatabaseError("table: <table> or <schema>.<table>", 422)


class Indexes:
    def __init__(self, databases: Databases):
        self.dbs = databases

    def _see(self, actor: str, name: str):
        row = self.dbs._row(name)
        if not any(self.dbs.allowed(actor, p, urn(name)) for p in ("VIEW_DATABASE", "USE_DATABASE", "MANAGE_DATABASE")):
            raise DatabaseError("no such database", 404)
        return row

    def _read(self, conn, name: str) -> list[dict]:
        out = []
        for r in conn.execute(LIST_SQL).fetchall():
            out.append({"database": name, "name": r[2], "table": f"{r[0]}.{r[1]}", "method": r[3], "unique": r[4],
                        "valid": r[5], "columns": list(r[6]), "expression": r[7], "constraint": r[8],
                        "size_bytes": r[9], "managed": not r[7] and not r[8]})
        return out

    def list(self, actor: str, name: str) -> list[dict]:
        row = self._see(actor, name)
        with self.dbs._admin(name, row.instance) as conn:
            return self._read(conn, name)

    def put(self, actor: str, name: str, index: str, body: dict) -> dict:
        from psycopg import sql

        row = self.dbs._row(name)
        self.dbs._need(actor, "MANAGE_DATABASE", urn(name))
        index = _ident(index, "index")
        schema, table = table_name(body.get("table"))
        cols = body.get("columns")
        if not isinstance(cols, list) or not 1 <= len(cols) <= 8:
            raise DatabaseError("columns: a list of 1 to 8 column names", 422)
        cols = [_ident(c, "column") for c in cols]
        method = str(body.get("method") or "btree")
        if method not in METHODS:
            raise DatabaseError(f"method: one of {', '.join(METHODS)}", 422)
        unique = bool(body.get("unique", False))
        want = {"table": f"{schema}.{table}", "columns": cols, "method": method, "unique": unique}
        with self.dbs._admin(name, row.instance) as conn:
            have = next((i for i in self._read(conn, name) if i["name"] == index), None)
            if have is not None:
                if {k: have[k] for k in want} == want and have["valid"]:
                    return have
                if have["valid"]:
                    raise DatabaseError(f"an index {index} exists with another definition: delete it first (an index "
                                        "is replaced, not changed)", 409)
            exists = conn.execute("SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN "
                                  "pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relname = %s "
                                  "AND a.attname = ANY(%s) AND a.attnum > 0 AND NOT a.attisdropped",
                                  (schema, table, cols)).fetchall()
            if len(exists) != len(set(cols)):
                raise DatabaseError(f"{schema}.{table} has no such column (or no such table)", 422)
            if have is not None:                             # an invalid one from a failed build: built again
                conn.execute(sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}.{}").format(sql.Identifier(schema),
                                                                                        sql.Identifier(index)))
            stmt = sql.SQL("CREATE {}INDEX CONCURRENTLY {} ON {}.{} USING {} ({})").format(
                sql.SQL("UNIQUE ") if unique else sql.SQL(""), sql.Identifier(index), sql.Identifier(schema),
                sql.Identifier(table), sql.SQL(method), sql.SQL(", ").join(sql.Identifier(c) for c in cols))
            conn.execute("SET statement_timeout = 0")
            try:
                conn.execute(stmt)
            except Exception as e:  # noqa: BLE001 - a failed concurrent build leaves an invalid index: dropped
                try:
                    conn.execute(sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}.{}").format(sql.Identifier(schema),
                                                                                            sql.Identifier(index)))
                except Exception:  # noqa: BLE001
                    logger.warning("could not drop the invalid index %s.%s", schema, index)
                raise DatabaseError(f"the index was not built: {str(e).splitlines()[0][:300]}", 409) from e
            made = next(i for i in self._read(conn, name) if i["name"] == index)
        self.dbs._audit("database.index_created", actor, name, {"index": index, **want})
        return made

    def delete(self, actor: str, name: str, index: str) -> None:
        from psycopg import sql

        row = self.dbs._row(name)
        self.dbs._need(actor, "MANAGE_DATABASE", urn(name))
        index = _ident(index, "index")
        with self.dbs._admin(name, row.instance) as conn:
            have = next((i for i in self._read(conn, name) if i["name"] == index), None)
            if have is None:
                raise DatabaseError("no such index", 404)
            if not have["managed"]:
                raise DatabaseError("that index backs a constraint or has expressions: change the schema instead", 409)
            schema = have["table"].split(".")[0]
            conn.execute(sql.SQL("DROP INDEX CONCURRENTLY IF EXISTS {}.{}").format(sql.Identifier(schema),
                                                                                    sql.Identifier(index)))
        self.dbs._audit("database.index_dropped", actor, name, {"index": index})


def build_indexes_router(ix: Indexes, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/databases", tags=["databases"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except DatabaseError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/{name}/indexes")
    async def list_(name: str, p=Depends(current_principal)):
        return {"indexes": await call(lambda: ix.list(p.urn, name))}

    @router.put("/{name}/indexes/{index}")
    async def put(name: str, index: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: ix.put(p.urn, name, index, body))

    @router.delete("/{name}/indexes/{index}", status_code=204)
    async def delete(name: str, index: str, p=Depends(current_principal)):
        await call(lambda: ix.delete(p.urn, name, index))
        return Response(status_code=204)

    return router
