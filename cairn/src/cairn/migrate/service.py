"""Migration from Snowflake and Databricks as an API (L170): convert a script, and validate side by side.

* ``POST /v1/migrate/convert`` ``{"source": "snowflake" | "databricks", "sql": "...", "connection": "<Cairn's>"}``:
  the script in Cairn's dialect, statement by statement, with what needs review and what has no counterpart
  (``cairn.migrate.convert``). With ``connection``, each converted **query** is also planned on Trino as the caller
  (never run), and one Trino refuses is marked for review with Trino's reason. ``?format=markdown`` returns the report.
* ``POST /v1/migrate/validate`` ``{"source": ..., "source_connection": "<the warehouse>", "connection": "<Cairn's>",
  "queries": [{"sql": "<in the source's dialect>", "target_sql": "<optional: Cairn's>", "ordered": false}]}``: runs
  each query on the source warehouse and its translation on Cairn, both as the caller and under the caller's policies
  (the governed query path), and compares the answers (``cairn.migrate.validate``).

Both need ``MANAGE_INGESTION``. Queries are read-only single statements; results are compared up to ``limit`` rows
(1 000 by default, 10 000 at most): a result cut at the limit is reported as ``partial`` and is not a match.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from cairn.migrate import convert as conv
from cairn.migrate.validate import compare

logger = logging.getLogger(__name__)

MAX_QUERIES = 200
PRIVILEGE = "MANAGE_INGESTION"


class MigrateError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


class Migration:
    def __init__(self, data_access, audit=None):
        self.data_access, self.audit = data_access, audit

    def _check(self, actor: str, connection: Optional[str]):
        if not connection:
            return None

        def check(sql: str) -> Optional[str]:
            from cairn.dataaccess.service import DataAccessError

            if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
                raise LookupError("only queries are planned")
            try:
                self.data_access.explain(actor, sql, connection)
                return None
            except DataAccessError as e:
                return str(e)
        return check

    def convert(self, actor: str, source: str, sql: str, connection: Optional[str] = None) -> dict:
        try:
            out = conv.convert(sql, str(source or ""), self._check(actor, connection))
        except ValueError as e:
            raise MigrateError(str(e))
        if self.audit is not None:
            self.audit.record("migrate", "migrate.converted", actor, None, {"source": source, **out["summary"]})
        return out

    def validate(self, actor: str, source: str, source_connection: str, connection: str, queries: list,
                 limit: int = 1000) -> dict:
        from cairn.dataaccess.service import DataAccessError

        if source not in conv.SOURCES:
            raise MigrateError(f"source: one of {', '.join(sorted(conv.SOURCES))}")
        if not source_connection or not connection or source_connection == connection:
            raise MigrateError("source_connection (the warehouse) and connection (Cairn's) are two connections")
        if not isinstance(queries, list) or not 1 <= len(queries) <= MAX_QUERIES:
            raise MigrateError(f"queries: between 1 and {MAX_QUERIES}")
        if not 1 <= int(limit) <= 10_000:
            raise MigrateError("limit: between 1 and 10000")
        results = []
        for i, q in enumerate(queries):
            q = q if isinstance(q, dict) else {"sql": q}
            sql = str(q.get("sql") or "")
            r: dict[str, Any] = {"index": i + 1, "sql": sql, "match": False}
            results.append(r)
            target = q.get("target_sql")
            if not target:
                try:
                    st = conv.convert(sql, source)["statements"]
                except ValueError as e:
                    r["error"] = str(e)
                    continue
                if len(st) != 1 or not st[0]["sql"]:
                    r["error"] = "not one statement that converts: give target_sql"
                    continue
                target, r["conversion"] = st[0]["sql"], {"status": st[0]["status"], "notes": st[0]["notes"]}
            r["target_sql"] = target
            try:
                a = self.data_access.query(actor, sql, source_connection, limit=int(limit))
            except DataAccessError as e:
                r["error"] = f"the source: {e}"
                continue
            try:
                b = self.data_access.query(actor, str(target), connection, limit=int(limit))
            except DataAccessError as e:
                r["error"] = f"Cairn: {e}"
                continue
            r.update(compare(a["columns"], a["rows"], b["columns"], b["rows"], bool(q.get("ordered"))))
            if a.get("truncated") or b.get("truncated"):
                r["partial"], r["match"] = True, False
                r["differences"].append(f"the answer has more than {limit} rows: compare an aggregate, or raise limit")
        summary = {"queries": len(results), "matching": sum(1 for r in results if r["match"]),
                   "different": sum(1 for r in results if not r["match"] and "error" not in r),
                   "failed": sum(1 for r in results if "error" in r)}
        if self.audit is not None:
            self.audit.record("migrate", "migrate.validated", actor, None,
                              {"source": source, "source_connection": source_connection, "connection": connection, **summary})
        return {"source": source, "results": results, "summary": summary}


def build_migrate_router(m: Migration, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from fastapi.responses import PlainTextResponse

    router = APIRouter(prefix="/v1/migrate", tags=["migrate"])

    def allowed(p):
        authorize(p, PRIVILEGE, None)
        return p

    def call(fn, *a):
        try:
            return fn(*a)
        except MigrateError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/convert")
    def convert(source: str = Body(...), sql: str = Body(...), connection: Optional[str] = Body(None),
                format: str = "json", p=Depends(current_principal)):
        """A Snowflake or Databricks script in Cairn's dialect, with what needs review and what does not convert."""
        out = call(m.convert, allowed(p).urn, source, sql, connection)
        return PlainTextResponse(conv.report(out), media_type="text/markdown") if format == "markdown" else out

    @router.post("/validate")
    def validate(source: str = Body(...), source_connection: str = Body(...), connection: str = Body(...),
                 queries: list = Body(...), limit: int = Body(1000), p=Depends(current_principal)):
        """Runs each query on the source warehouse and its translation on Cairn, and compares the answers."""
        return call(m.validate, allowed(p).urn, source, source_connection, connection, queries, limit)

    return router
