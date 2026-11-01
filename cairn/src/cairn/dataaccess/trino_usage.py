"""Who reads what through Trino: Trino's HTTP event listener sends every finished query (its user, and
each table and column it read) and Cairn turns the reads into each dataset's usage statistics
(``datasetUsageStatistics``, one bucket per day): queries, distinct users, the users and how often they
read it, and which columns were read.

* Counts live in Cairn's database (``cairn_trino_usage``), so a day's bucket is the sum of every
  replica's queries. Each query is counted once, even when Trino resends it (``cairn_trino_usage_queries``).
* A query that failed, and Trino's own metadata (``system``, ``information_schema``), are not reads.
* When Core cannot take a bucket, the bucket waits (``cairn_trino_usage_pending``) and is written by the
  retry loop, so a Core outage loses no usage.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import BigInteger, Column, Integer, PrimaryKeyConstraint, String, Table, delete, select, update
from sqlalchemy.exc import IntegrityError

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

trino_usage = Table(
    "cairn_trino_usage", metadata,
    Column("day_ms", BigInteger, nullable=False),
    Column("urn", String(512), nullable=False),
    Column("user", String(256), nullable=False),
    Column("field", String(256), nullable=False),     # "" counts the queries themselves
    Column("count", Integer, nullable=False),
    PrimaryKeyConstraint("day_ms", "urn", "user", "field"),
)
trino_usage_queries = Table(
    "cairn_trino_usage_queries", metadata,
    Column("query_id", String(64), primary_key=True),
    Column("day_ms", BigInteger, nullable=False, index=True),
)
trino_usage_pending = Table(
    "cairn_trino_usage_pending", metadata,
    Column("day_ms", BigInteger, nullable=False),
    Column("urn", String(512), nullable=False),
    PrimaryKeyConstraint("day_ms", "urn"),
)

NOT_READS = {"EXPLAIN", "DESCRIBE"}
METADATA_SCHEMAS = {"information_schema"}
DAY_MS = 86_400_000
KEEP_DAYS = 35


def _day(ts: str) -> int:
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        when = datetime.now(timezone.utc)
    ms = int(when.timestamp() * 1000)
    return ms - ms % DAY_MS


class UsageError(ValueError):
    pass


class TrinoUsage:
    def __init__(self, engine, graph, urn_of: Callable[[str, str, str], str]):
        self.engine, self.graph, self.urn_of = engine, graph, urn_of

    # ---- recording ----------------------------------------------------------------------------
    def reads(self, event: dict) -> tuple[str, str, int, dict[str, set[str]]]:
        """(query id, user urn, day, {dataset urn: columns read}) for a finished query, else UsageError."""
        if not isinstance(event, dict) or not isinstance(event.get("metadata"), dict):
            raise UsageError("not a Trino QueryCompletedEvent")
        meta, ctx = event["metadata"], event.get("context") or {}
        qid, user = str(meta.get("queryId") or ""), str(ctx.get("user") or "")
        if not qid or not user:
            raise UsageError("the event has no query id or user")
        out: dict[str, set[str]] = {}
        if meta.get("queryState") != "FINISHED" or event.get("failureInfo") or ctx.get("queryType") in NOT_READS:
            return qid, f"urn:li:corpuser:{user}", 0, out
        for i in (event.get("ioMetadata") or {}).get("inputs") or []:
            cat, sch, tab = i.get("catalogName"), i.get("schema"), i.get("table")
            if not (cat and sch and tab) or cat == "system" or sch in METADATA_SCHEMAS:
                continue
            cols = out.setdefault(self.urn_of(cat, sch, tab), set())
            cols.update(c["name"] for c in i.get("columns") or [] if isinstance(c, dict) and c.get("name"))
        return qid, f"urn:li:corpuser:{user}", _day(event.get("createTime") or ""), out

    def record(self, event: dict) -> dict:
        qid, user, day, reads = self.reads(event)
        if not reads:
            return {"query": qid, "datasets": 0}
        t = trino_usage
        try:
            with self.engine.begin() as conn:
                # Counted once: Trino resends an event it thinks was not delivered.
                conn.execute(trino_usage_queries.insert().values(query_id=qid, day_ms=day))
                for urn, cols in reads.items():
                    for field in [""] + sorted(cols):
                        key = (t.c.day_ms == day) & (t.c.urn == urn) & (t.c.user == user) & (t.c.field == field)
                        if conn.execute(update(t).where(key).values(count=t.c.count + 1)).rowcount == 0:
                            conn.execute(t.insert().values(day_ms=day, urn=urn, user=user, field=field, count=1))
                    conn.execute(delete(trino_usage_pending).where(
                        (trino_usage_pending.c.day_ms == day) & (trino_usage_pending.c.urn == urn)))
                    conn.execute(trino_usage_pending.insert().values(day_ms=day, urn=urn))
        except IntegrityError:
            return {"query": qid, "datasets": 0, "duplicate": True}
        written = self.flush(list(reads), day)
        return {"query": qid, "datasets": len(reads), "written": written}

    # ---- writing to Core ----------------------------------------------------------------------
    def bucket(self, urn: str, day: int) -> dict:
        t = trino_usage
        with self.engine.connect() as conn:
            rows = conn.execute(select(t.c.user, t.c.field, t.c.count).where((t.c.day_ms == day) & (t.c.urn == urn))).all()
        users: dict[str, int] = {}
        fields: dict[str, int] = {}
        for user, field, n in rows:
            if field == "":
                users[user] = users.get(user, 0) + n
            else:
                fields[field] = fields.get(field, 0) + n
        return {
            "timestampMillis": day, "eventGranularity": {"unit": "DAY", "multiple": 1},
            "uniqueUserCount": len(users), "totalSqlQueries": sum(users.values()),
            "userCounts": [{"user": u, "count": n} for u, n in sorted(users.items(), key=lambda kv: (-kv[1], kv[0]))],
            "fieldCounts": [{"fieldPath": f, "count": n} for f, n in sorted(fields.items(), key=lambda kv: (-kv[1], kv[0]))],
        }

    def flush(self, urns: list[str] | None = None, day: int | None = None) -> int:
        """Writes waiting buckets to Core; those Core cannot take now stay waiting."""
        from cairn.common.proposals import make_mcp

        p = trino_usage_pending
        q = select(p.c.day_ms, p.c.urn)
        if urns is not None:
            q = q.where(p.c.urn.in_(urns) & (p.c.day_ms == day))
        with self.engine.connect() as conn:
            due = conn.execute(q.limit(500)).all()
        written = 0
        for d, urn in due:
            try:
                self.graph.emit([make_mcp(urn, "datasetUsageStatistics", self.bucket(urn, d))])
            except Exception as e:  # Core is down: the retry loop writes it later
                logger.warning("Trino usage for %s could not be written now: %s", urn, e)
                break
            with self.engine.begin() as conn:
                conn.execute(delete(p).where((p.c.day_ms == d) & (p.c.urn == urn)))
            written += 1
        return written

    def prune(self, now_ms: int | None = None) -> None:
        """Forgets counts and query ids older than KEEP_DAYS (Core keeps the buckets)."""
        cutoff = (now_ms or int(time.time() * 1000)) - KEEP_DAYS * DAY_MS
        with self.engine.begin() as conn:
            conn.execute(delete(trino_usage_queries).where(trino_usage_queries.c.day_ms < cutoff))
            conn.execute(delete(trino_usage).where(trino_usage.c.day_ms < cutoff))
