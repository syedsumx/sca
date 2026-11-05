"""Query history, profiles and tuning hints for Trino (the shared coordinator and team warehouses).

Trino's HTTP event listener sends every finished query to Cairn (``/v1/trino/<key>/queries``, the same
events that feed usage statistics). Cairn keeps, per query: who ran it, its SQL, its state and error,
when it ran, its wall, CPU and queued time, what it read and returned, its peak memory, the tables it
read, and its plan when Trino includes one. ``CAIRN_QUERY_HISTORY_DAYS`` (30) is how long.

* ``GET /v1/queries``: people see their own queries; operators (``MANAGE_SYSTEM_OPERATIONS``) see
  everyone's. Filters: ``user``, ``state`` (``FINISHED``/``FAILED``), ``since`` (ms), ``table`` (a dataset
  URN), ``text`` (in the SQL), ``slowest``.
* ``GET /v1/queries/<id>``: the profile, with tuning hints drawn from its statistics and SQL: queued
  long (the warehouse is busy), most of the bytes read for few rows returned (add filters on partition
  columns), ``SELECT *`` over wide reads, a cross join, sorting a large result without ``LIMIT``, memory
  near the per-query limit, and the failure explained.

The SQL may hold literals people typed, so only its author and operators see a query.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, select
from sqlalchemy.exc import IntegrityError

from cairn.common.db import metadata

logger = logging.getLogger(__name__)
DAY_MS = 86_400_000

query_history = Table(
    "cairn_query_history", metadata,
    Column("query_id", String(64), primary_key=True),
    Column("user", String(256), nullable=False, index=True),
    Column("source", String(256), nullable=False, default=""),
    Column("state", String(16), nullable=False, index=True),
    Column("error_code", String(128), nullable=True),
    Column("error", Text, nullable=True),
    Column("sql", Text, nullable=False),
    Column("create_ms", BigInteger, nullable=False, index=True),
    Column("end_ms", BigInteger, nullable=True),
    Column("wall_ms", BigInteger, nullable=False, default=0),
    Column("cpu_ms", BigInteger, nullable=False, default=0),
    Column("queued_ms", BigInteger, nullable=False, default=0),
    Column("input_bytes", BigInteger, nullable=False, default=0),
    Column("input_rows", BigInteger, nullable=False, default=0),
    Column("output_rows", BigInteger, nullable=False, default=0),
    Column("peak_memory_bytes", BigInteger, nullable=False, default=0),
    Column("tables", Text, nullable=False, default="[]"),
    Column("plan", Text, nullable=True),
    Column("server", String(256), nullable=False, default=""),
    Column("splits", Integer, nullable=False, default=0),
)


def trino_rates() -> tuple[Optional[float], Optional[float]]:
    """(USD per CPU hour, USD per TB scanned) of Trino (``CAIRN_COST_TRINO_USD_PER_CPU_HOUR``,
    ``CAIRN_COST_TRINO_USD_PER_TB_SCANNED``); either may be unset. Both unset: queries are not priced."""
    def rate(name: str) -> Optional[float]:
        try:
            v = float(os.environ.get(name, ""))
        except ValueError:
            return None
        return v if v >= 0 else None
    return rate("CAIRN_COST_TRINO_USD_PER_CPU_HOUR"), rate("CAIRN_COST_TRINO_USD_PER_TB_SCANNED")


def price(cpu_ms: int, input_bytes: int, rates: Optional[tuple[Optional[float], Optional[float]]] = None) -> Optional[float]:
    """A query's cost: its CPU time and the bytes it scanned at Trino's rates; None when Trino is not priced."""
    cpu, tb = rates if rates is not None else trino_rates()
    if cpu is None and tb is None:
        return None
    return round((cpu or 0) * (cpu_ms or 0) / 3_600_000 + (tb or 0) * (input_bytes or 0) / 1e12, 6)


def _ms(v: Any) -> int:
    """Trino's HTTP event listener sends durations in seconds (0.282); ISO-8601 (PT1.5S) is read too."""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(round(v * 1000))
    s = str(v)
    if s.startswith("PT"):
        import re

        m = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", s)
        if m:
            h, mi, sec = (float(x or 0) for x in m.groups())
            return int((h * 3600 + mi * 60 + sec) * 1000)
    try:
        return int(round(float(s) * 1000))
    except ValueError:
        return 0


def _ts(v: Any) -> Optional[int]:
    if not v:
        return None
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


class QueryHistory:
    def __init__(self, engine, urn_of=None, retention_days: Optional[int] = None, query_max_memory: Optional[int] = None):
        self.engine, self.urn_of = engine, urn_of
        self.retention_days = retention_days or int(os.environ.get("CAIRN_QUERY_HISTORY_DAYS", "30"))
        # Trino's per-query memory limit, for the "near the limit" hint (bytes; CAIRN_TRINO_QUERY_MAX_MEMORY).
        self.query_max_memory = query_max_memory or _bytes(os.environ.get("CAIRN_TRINO_QUERY_MAX_MEMORY", ""))
        self._pruned = 0.0

    def record(self, event: dict) -> Optional[str]:
        meta, ctx, st = event.get("metadata") or {}, event.get("context") or {}, event.get("statistics") or {}
        qid, user = str(meta.get("queryId") or ""), str(ctx.get("user") or "")
        if not qid or not user:
            return None
        failure = event.get("failureInfo") or {}
        tables = []
        for i in (event.get("ioMetadata") or {}).get("inputs") or []:
            cat, sch, tab = i.get("catalogName"), i.get("schema"), i.get("table")
            if cat and sch and tab and cat != "system":
                tables.append(self.urn_of(cat, sch, tab) if self.urn_of else f"{cat}.{sch}.{tab}")
        row = {"query_id": qid, "user": user, "source": str(ctx.get("source") or "")[:256],
               "state": "FAILED" if failure or meta.get("queryState") == "FAILED" else str(meta.get("queryState") or "FINISHED")[:16],
               "error_code": ((failure.get("errorCode") or {}).get("name") if isinstance(failure.get("errorCode"), dict)
                              else failure.get("errorCode")) or None,
               "error": (str(failure.get("failureMessage") or "")[:4000] or None) if failure else None,
               "sql": str(meta.get("query") or "")[:100_000],
               "create_ms": _ts(event.get("createTime")) or int(time.time() * 1000), "end_ms": _ts(event.get("endTime")),
               "wall_ms": _ms(st.get("wallTime")), "cpu_ms": _ms(st.get("cpuTime")), "queued_ms": _ms(st.get("queuedTime")),
               "input_bytes": int(st.get("physicalInputBytes") or st.get("totalBytes") or 0),
               "input_rows": int(st.get("physicalInputRows") or st.get("totalRows") or 0),
               "output_rows": int(st.get("outputRows") or 0),
               "peak_memory_bytes": int(st.get("peakUserMemoryBytes") or 0),
               "tables": json.dumps(sorted(set(tables))), "plan": (str(meta.get("plan"))[:200_000] if meta.get("plan") else None),
               "server": str(ctx.get("serverAddress") or "")[:256], "splits": int(st.get("completedSplits") or 0)}
        try:
            with self.engine.begin() as conn:
                conn.execute(query_history.insert().values(**row))
        except IntegrityError:
            return qid                                       # Trino resent it
        if time.monotonic() - self._pruned > 3600:
            self._pruned = time.monotonic()
            with self.engine.begin() as conn:
                conn.execute(delete(query_history).where(
                    query_history.c.create_ms < int(time.time() * 1000) - self.retention_days * DAY_MS))
        return qid

    def reading(self, table_urn: str, since_ms: int, limit: int = 5000) -> list[tuple[str, int]]:
        """(SQL, bytes read) of the finished queries since ``since_ms`` that read ``table_urn``, newest first."""
        needle = json.dumps(table_urn)
        with self.engine.connect() as conn:
            rows = conn.execute(select(query_history.c.sql, query_history.c.input_bytes, query_history.c.tables).where(
                query_history.c.create_ms >= since_ms, query_history.c.state == "FINISHED",
                query_history.c.tables.contains(needle, autoescape=True))
                .order_by(query_history.c.create_ms.desc()).limit(limit)).all()
        return [(r.sql, int(r.input_bytes or 0)) for r in rows if table_urn in json.loads(r.tables or "[]")]

    @staticmethod
    def _public(r: dict) -> dict:
        out = {k: r[k] for k in ("query_id", "user", "source", "state", "error_code", "error", "create_ms", "end_ms",
                                 "wall_ms", "cpu_ms", "queued_ms", "input_bytes", "input_rows", "output_rows",
                                 "peak_memory_bytes", "splits")}
        out["sql"] = r["sql"]
        out["tables"] = json.loads(r["tables"] or "[]")
        out["cost_usd"] = price(r["cpu_ms"], r["input_bytes"])
        return out

    def list(self, actor_user: str, everyone: bool, user: Optional[str] = None, state: Optional[str] = None,
             since: Optional[int] = None, table: Optional[str] = None, text: Optional[str] = None,
             slowest: bool = False, limit: int = 100) -> list[dict]:
        t = query_history
        q = select(t)
        if not everyone:
            q = q.where(t.c.user == actor_user)
        elif user:
            q = q.where(t.c.user == user)
        if state:
            q = q.where(t.c.state == state.upper())
        if since:
            q = q.where(t.c.create_ms >= since)
        if table:
            q = q.where(t.c.tables.contains(json.dumps(table)))
        if text:
            q = q.where(t.c.sql.contains(text))
        q = q.order_by(t.c.wall_ms.desc() if slowest else t.c.create_ms.desc()).limit(max(1, min(limit, 1000)))
        with self.engine.connect() as conn:
            return [self._public(dict(r._mapping)) for r in conn.execute(q)]

    def get(self, qid: str, actor_user: str, everyone: bool) -> Optional[dict]:
        with self.engine.connect() as conn:
            r = conn.execute(select(query_history).where(query_history.c.query_id == qid)).first()
        if r is None:
            return None
        r = dict(r._mapping)
        if not everyone and r["user"] != actor_user:
            return None
        out = self._public(r)
        out["plan"] = r["plan"]
        out["hints"] = self.hints(r)
        return out

    def hints(self, r: dict) -> list[dict]:
        out: list[dict] = []

        def hint(kind: str, text: str):
            out.append({"kind": kind, "text": text})

        wall, queued = r["wall_ms"] or 0, r["queued_ms"] or 0
        if queued >= 5000 and queued >= 0.3 * max(wall, 1):
            hint("queued", f"Waited {queued / 1000:.0f} s of {wall / 1000:.0f} s in the queue: the warehouse was busy. "
                           "Run it when the load is lower, on a warehouse with more workers, or in another resource group.")
        gb = (r["input_bytes"] or 0) / 1e9
        if gb >= 1 and (r["output_rows"] or 0) <= 1000 and (r["input_rows"] or 0) > 1000 * max(r["output_rows"] or 1, 1):
            hint("filter", f"Read {gb:.1f} GB ({r['input_rows']:,} rows) to return {r['output_rows']:,}: filter on the "
                           "tables' partition columns (or a sort order) so fewer files are read, or keep a derived table.")
        sql = r["sql"] or ""
        try:
            import sqlglot
            from sqlglot import exp

            tree = sqlglot.parse_one(sql, read="trino")
        except Exception:  # noqa: BLE001 - hints from statistics only
            tree = None
        if tree is not None:
            if gb >= 0.1 and any(isinstance(e, exp.Star) for sel in tree.find_all(exp.Select) for e in sel.expressions):
                hint("columns", "SELECT * reads every column: name the columns you need, so Trino reads less of each file.")

            def cross(j) -> bool:
                if isinstance(j.this, (exp.Unnest, exp.Lateral)):
                    return False                          # take no condition by design
                # CROSS JOIN, or FROM a, b with no condition.
                return str(j.args.get("kind") or "").upper() == "CROSS" or (not j.args.get("on") and not j.args.get("using"))
            if any(cross(j) for j in tree.find_all(exp.Join)):
                hint("cross_join", "A cross join multiplies its inputs' rows: join on a condition.")
            if tree.args.get("order") is not None and tree.args.get("limit") is None and (r["output_rows"] or 0) > 100_000:
                hint("order", f"Sorted {r['output_rows']:,} rows without LIMIT: add a LIMIT, or sort in the tool that reads them.")
        if r.get("plan") and "CrossJoin" in (r["plan"] or "") and not any(h["kind"] == "cross_join" for h in out):
            hint("cross_join", "The plan has a cross join: check that every join has a condition.")
        if self.query_max_memory and (r["peak_memory_bytes"] or 0) >= 0.8 * self.query_max_memory:
            hint("memory", f"Peak memory {r['peak_memory_bytes'] / 1e9:.1f} GB is near the per-query limit: "
                           "reduce what is joined or grouped at once, or turn on spill for this warehouse.")
        code = (r.get("error_code") or "").upper()
        if code in ("EXCEEDED_USER_MEMORY_LIMIT", "EXCEEDED_GLOBAL_MEMORY_LIMIT", "EXCEEDED_LOCAL_MEMORY_LIMIT"):
            hint("failed_memory", "Failed for lack of memory: filter earlier, join smaller inputs first, or use a bigger warehouse.")
        elif code == "PERMISSION_DENIED":
            hint("failed_access", "Refused by Cairn's policies: request access to the table from its page in Cairn.")
        elif code in ("EXCEEDED_TIME_LIMIT", "QUERY_QUEUE_FULL"):
            hint("failed_capacity", "The warehouse was at capacity: try again later or use another warehouse.")
        return out


def _bytes(v: str) -> Optional[int]:
    v = (v or "").strip().upper()
    if not v:
        return None
    units = {"TB": 1e12, "GB": 1e9, "MB": 1e6, "KB": 1e3, "B": 1}
    for u, f in units.items():
        if v.endswith(u):
            try:
                return int(float(v[: -len(u)]) * f)
            except ValueError:
                return None
    try:
        return int(v)
    except ValueError:
        return None


def build_queries_router(history: QueryHistory, current_principal, authorize):
    from fastapi import APIRouter, Depends, HTTPException

    router = APIRouter(prefix="/v1/queries", tags=["trino"])

    def scope(p) -> tuple[str, bool]:
        everyone = bool(authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))
        return p.urn.removeprefix("urn:li:corpuser:"), everyone

    @router.get("")
    def list_queries(user: Optional[str] = None, state: Optional[str] = None, since: Optional[int] = None,
                     table: Optional[str] = None, text: Optional[str] = None, slowest: bool = False, limit: int = 100,
                     p=Depends(current_principal)):
        me, everyone = scope(p)
        return {"queries": history.list(me, everyone, user, state, since, table, text, slowest, limit)}

    @router.get("/{qid}")
    def profile(qid: str, p=Depends(current_principal)):
        me, everyone = scope(p)
        q = history.get(qid, me, everyone)
        if q is None:
            raise HTTPException(404, "no such query (or not yours)")
        return q

    return router
