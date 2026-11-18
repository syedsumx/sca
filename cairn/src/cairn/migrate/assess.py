"""Assessing a warehouse before moving off it (L170): what is there, and how much of its SQL converts.

``POST /v1/migrate/assess`` reads a source that Trino reaches as a catalog, as the caller, and answers with:

* **objects**: per schema, its tables and views and their columns; the column types found, with those the lakehouse
  keeps differently (Iceberg has no ``json``, ``char(n)``, small integers or time zones on times) counted; names a
  move would refuse. With ``"count_rows": true``, each table's row count (at most 200 tables; a count is a scan).
* **workload**, when a query log is named (``"query_log": {"table": "catalog.schema.table", "text": "query_text",
  "executions": "runs", "cost": "credits"}``): the statements the warehouse actually ran, exported to a table by its
  owner (Snowflake's ``ACCOUNT_USAGE.QUERY_HISTORY``, Databricks' ``system.query.history``). Each distinct statement
  is run through the converter (``cairn.migrate.convert``), and the answer says what share converts, needs review or
  has no counterpart, **by statements, by executions and by cost**, the reasons in order of cost, and the costliest
  statements that need a person. That is where the work of a migration is.

Nothing is changed and no table's rows are returned: counts, types and SQL text only. The SQL text of the query log
is the source's own; it is returned only for the statements listed as needing work, cut to 300 characters.
Needs ``MANAGE_INGESTION``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

from cairn.migrate import convert as conv
from cairn.migrate.move import IDENT, fq, q
from cairn.migrate.service import MigrateError

logger = logging.getLogger(__name__)

MAX_STATEMENTS = 5000
MAX_COUNTED = 200
# Types Iceberg tables do not have: Trino keeps the column in the nearest one (or refuses the table).
DIFFERENT = [(r"^json$", "kept as varchar"), (r"^char\(", "kept as varchar"), (r"^(tinyint|smallint)$", "kept as integer"),
             (r"^time(\(\d\))? with time zone$", "not supported: cast it at the source"),
             (r"^timestamp\((\d)\)", "kept to the microsecond"), (r"^(interval|ipaddress|hyperloglog|qdigest|tdigest)", "not supported"),
             (r"^(geometry|sphericalgeography)", "kept as varbinary or varchar: check"), (r"^unknown$", "not supported")]


def kept_as(dtype: str) -> Optional[str]:
    d = dtype.lower()
    for pattern, what in DIFFERENT:
        m = re.match(pattern, d)
        if m and not (pattern.startswith(r"^timestamp") and m.group(1) == "6"):
            return what
    return None


class Assessment:
    def __init__(self, run: Callable[[str, str, str], list], audit=None):
        """``run(connection, actor, sql)``: a statement on Trino as ``actor``, its rows."""
        self.run, self.audit = run, audit

    def _objects(self, connection: str, actor: str, source: str, schemas: list[str], count_rows: bool) -> dict:
        where = "table_schema NOT IN ('information_schema', 'pg_catalog')"
        if schemas:
            where = "table_schema IN (" + ", ".join("'" + s + "'" for s in schemas) + ")"
        info = f"{q(source)}.information_schema"
        try:
            tables = self.run(connection, actor, f"SELECT table_schema, table_name, table_type FROM {info}.tables WHERE {where}")
            columns = self.run(connection, actor, "SELECT table_schema, table_name, column_name, data_type "
                                                  f"FROM {info}.columns WHERE {where}")
        except Exception as e:  # noqa: BLE001 - Trino's reason, as for any query
            raise MigrateError(f"the source could not be read: {str(e).splitlines()[0][:300]}", 502)
        per: dict[str, dict] = {}
        kinds = {(s, t): str(k) for s, t, k in tables}
        for (s, _table), k in kinds.items():
            d = per.setdefault(str(s), {"schema": str(s), "tables": 0, "views": 0, "columns": 0})
            d["views" if "VIEW" in k.upper() else "tables"] += 1
        types: dict[str, int] = {}
        differ: dict[str, dict] = {}
        unmovable = set()
        for s, t, c, dtype in columns:
            if (s, t) not in kinds:
                continue
            per[str(s)]["columns"] += 1
            base = re.sub(r"\(.*", "", str(dtype).lower())
            types[base] = types.get(base, 0) + 1
            what = kept_as(str(dtype))
            if what and "VIEW" not in kinds[(s, t)].upper():
                d = differ.setdefault(str(dtype).lower(), {"type": str(dtype).lower(), "columns": 0, "lakehouse": what})
                d["columns"] += 1
            if not (IDENT.match(str(s)) and IDENT.match(str(t)) and IDENT.match(str(c).lower())):
                unmovable.add(f"{s}.{t}")
        out: dict[str, Any] = {
            "schemas": sorted(per.values(), key=lambda d: d["schema"]),
            "totals": {"schemas": len(per), "tables": sum(d["tables"] for d in per.values()),
                       "views": sum(d["views"] for d in per.values()), "columns": sum(d["columns"] for d in per.values())},
            "types": dict(sorted(types.items(), key=lambda kv: -kv[1])),
            "types_kept_differently": sorted(differ.values(), key=lambda d: -d["columns"]),
            "names_a_move_refuses": sorted(unmovable)[:200]}
        if count_rows:
            base_tables = sorted(f"{s}.{t}" for (s, t), k in kinds.items() if "VIEW" not in k.upper()
                                 and IDENT.match(str(s)) and IDENT.match(str(t)))
            rows, failed = [], 0
            for full in base_tables[:MAX_COUNTED]:
                try:
                    rows.append({"table": full, "rows": int(self.run(connection, actor, f"SELECT count(*) FROM {fq(source, full)}")[0][0])})
                except Exception:  # noqa: BLE001 - a table the caller may not read: counted as such
                    failed += 1
            out["rows"] = {"tables": sorted(rows, key=lambda r: -r["rows"]), "total": sum(r["rows"] for r in rows),
                           "not_counted": failed + max(0, len(base_tables) - MAX_COUNTED)}
        return out

    def _workload(self, connection: str, actor: str, dialect: str, log: dict) -> dict:
        table = str(log.get("table") or "").lower()
        cols = {k: str(log.get(k) or "").lower() for k in ("text", "executions", "cost")}
        if table.count(".") != 2 or not all(IDENT.match(p) for p in table.split(".")) or not IDENT.match(cols["text"]) \
                or any(cols[k] and not IDENT.match(cols[k]) for k in ("executions", "cost")):
            raise MigrateError("query_log: table (catalog.schema.table) and text (its column of SQL text); "
                               "executions and cost are optional columns")
        catalog, rest = table.split(".", 1)
        runs = f"sum(CAST({q(cols['executions'])} AS double))" if cols["executions"] else "CAST(count(*) AS double)"
        cost = f"sum(CAST({q(cols['cost'])} AS double))" if cols["cost"] else "CAST(0 AS double)"
        try:
            rows = self.run(connection, actor,
                            f"SELECT {q(cols['text'])}, {runs}, {cost} FROM {fq(catalog, rest)} WHERE {q(cols['text'])} IS NOT NULL "
                            f"GROUP BY 1 ORDER BY 3 DESC, 2 DESC LIMIT {MAX_STATEMENTS + 1}")
        except Exception as e:  # noqa: BLE001
            raise MigrateError(f"the query log could not be read: {str(e).splitlines()[0][:300]}", 502)
        truncated = len(rows) > MAX_STATEMENTS
        by = {s: {"statements": 0, "executions": 0.0, "cost": 0.0} for s in ("converted", "needs_review", "not_converted")}
        reasons: dict[str, dict] = {}
        work = []
        for text, n, c in rows[:MAX_STATEMENTS]:
            n, c = float(n or 0), float(c or 0)
            try:
                sts = conv.convert(str(text), dialect)["statements"]
            except ValueError:
                sts = [{"status": "not_converted", "notes": [{"code": "parse", "message": "not readable", "review": True}]}]
            order = ("not_converted", "needs_review", "converted")
            status = min((s["status"] for s in sts), key=order.index)             # a script is as done as its worst
            b = by[status]
            b["statements"], b["executions"], b["cost"] = b["statements"] + 1, b["executions"] + n, b["cost"] + c
            if status != "converted":
                codes = sorted({nn["code"] for s in sts for nn in s["notes"] if nn["review"]})
                for code in codes:
                    r = reasons.setdefault(code, {"reason": code, "statements": 0, "executions": 0.0, "cost": 0.0})
                    r["statements"], r["executions"], r["cost"] = r["statements"] + 1, r["executions"] + n, r["cost"] + c
                work.append({"sql": " ".join(str(text).split())[:300], "status": status, "reasons": codes,
                             "executions": n, "cost": round(c, 4)})
        total = {k: sum(b[k] for b in by.values()) for k in ("statements", "executions", "cost")}
        share = lambda part, whole: round(100.0 * part / whole, 1) if whole else None  # noqa: E731
        for b in by.values():
            b.update(share_of_statements=share(b["statements"], total["statements"]),
                     share_of_executions=share(b["executions"], total["executions"]),
                     share_of_cost=share(b["cost"], total["cost"]), cost=round(b["cost"], 4))
        for r in reasons.values():
            r["cost"] = round(r["cost"], 4)
        return {"dialect": dialect, "distinct_statements": total["statements"], "executions": total["executions"],
                "cost": round(total["cost"], 4), "truncated": truncated, **by,
                "reasons": sorted(reasons.values(), key=lambda r: (-r["cost"], -r["executions"], r["reason"])),
                "costliest_needing_work": sorted(work, key=lambda w: (-w["cost"], -w["executions"]))[:25]}

    def assess(self, actor: str, body: dict) -> dict:
        connection, source = str(body.get("connection") or ""), str(body.get("source_catalog") or "").lower()
        schemas = [str(s).lower() for s in body.get("schemas") or []]
        if not connection or not IDENT.match(source) or any(not IDENT.match(s) for s in schemas):
            raise MigrateError("connection (Cairn's Trino), source_catalog (the source, as Trino names it) and, "
                               "optionally, schemas")
        out: dict[str, Any] = {"source_catalog": source,
                               "objects": self._objects(connection, actor, source, schemas, bool(body.get("count_rows")))}
        log = body.get("query_log")
        if log:
            dialect = str(body.get("source") or "")
            if dialect not in conv.SOURCES or not isinstance(log, dict):
                raise MigrateError(f"with query_log, source is one of {', '.join(sorted(conv.SOURCES))}")
            out["workload"] = self._workload(connection, actor, dialect, log)
        if self.audit is not None:
            self.audit.record("migrate", "migrate.assessed", actor, None,
                              {"source_catalog": source, **out["objects"]["totals"],
                               "statements": (out.get("workload") or {}).get("distinct_statements")})
        return out


def build_assess_router(a: Assessment, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException

    from cairn.migrate.service import PRIVILEGE

    router = APIRouter(prefix="/v1/migrate", tags=["migrate"])

    @router.post("/assess")
    def assess(body: dict = Body(...), p=Depends(current_principal)):
        """What a source holds, and how much of its query history converts, by statements, executions and cost."""
        authorize(p, PRIVILEGE, None)
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "a source is assessed by a person, as that person on Trino")
        try:
            return a.assess(p.urn, body)
        except MigrateError as e:
            raise HTTPException(e.status, str(e))

    return router
