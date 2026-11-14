"""A shared result cache for dashboards, workbooks and metrics: the same answer is not computed twice, and nobody
is ever served rows they would not see.

A result is kept under the query (its SQL, connection and row limit) and a **policy fingerprint**: for each table
it reads, the decisions that shape what a person sees there (whether they may read it, the row filters and column
masks Trino applies to them, the masks Cairn applies to results). Two people share a cached result only when
those decisions are the same for every table; anyone else misses and runs the query as themselves. A row filter
naming the person (``{user}``) makes the result theirs alone. When a decision cannot be made, nothing is cached.
The fingerprint is worked out on each lookup, so a policy change takes effect at once.

A result is dropped when any table it read changes (:class:`cairn.pipelines.changes.DatasetChanges`: pipelines,
dbt, derived tables, input tables) and after ``CAIRN_RESULT_CACHE_SECONDS`` (default 600) for changes Cairn does
not see. Results above ``CAIRN_RESULT_CACHE_MAX_BYTES`` (default 2 MB) are not kept. A hit is audited like a
query (``data.queried`` with ``cached``); the SQL editor never uses the cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, insert, select, update

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

result_cache = Table(
    "cairn_result_cache", metadata,
    Column("key", String(64), primary_key=True),
    Column("urns", Text, nullable=False),
    Column("seq", BigInteger, nullable=False),          # the change log's head when the result was computed
    Column("payload", Text, nullable=False),
    Column("hits", Integer, nullable=False, default=0),
    Column("created_ms", BigInteger, nullable=False),
    Column("expires_ms", BigInteger, nullable=False, index=True),
)


def _now() -> int:
    return int(time.time() * 1000)


class ResultCache:
    def __init__(self, engine, urns_of: Callable[[str, str], list[str]],
                 decisions: Callable[[str, str], Any], changes: Any = None, audit=None):
        """``urns_of(sql, connection)``: the datasets a query reads; ``decisions(actor, urn)``: what shapes what
        ``actor`` sees of ``urn`` (any JSON-able value; it raises when it cannot be decided); ``changes``: the
        change log, or a function returning it (it may be built after the cache)."""
        self.engine, self.urns_of, self.decisions, self.audit = engine, urns_of, decisions, audit
        self._changes = changes
        self.ttl_ms = int(float(os.environ.get("CAIRN_RESULT_CACHE_SECONDS", "600")) * 1000)
        self.max_bytes = int(os.environ.get("CAIRN_RESULT_CACHE_MAX_BYTES", str(2 * 1024 * 1024)))

    @property
    def changes(self):
        c = self._changes
        return c() if callable(c) else c

    def _key(self, actor: str, sql: str, connection: str, limit: int) -> Optional[tuple[str, list[str]]]:
        try:
            urns = sorted(set(self.urns_of(sql, connection)))
            if not urns:
                return None
            seen = [[u, self.decisions(actor, u)] for u in urns]
        except Exception as e:  # noqa: BLE001 - no decision, no cache: the query runs as the person
            logger.debug("result cache: no fingerprint for %s: %s", actor, e)
            return None
        text = json.dumps(seen, sort_keys=True, default=str)
        who = actor if "{user}" in text else ""                # a filter naming the person: theirs alone
        digest = hashlib.sha256(json.dumps([connection, sql, limit, who, text]).encode()).hexdigest()
        return digest, urns

    def _fresh(self, urns: list[str], seq: int) -> bool:
        return self.changes is None or not self.changes.since(urns, seq, 1)

    def get(self, actor: str, sql: str, connection: str, limit: int) -> tuple[Optional[dict], Optional[tuple]]:
        """``(result, None)`` on a hit; ``(None, ticket)`` on a miss, the ticket for :meth:`put` (None: not
        cacheable)."""
        if self.ttl_ms <= 0:
            return None, None
        k = self._key(actor, sql, connection, limit)
        if k is None:
            return None, None
        key, urns = k
        seq = self.changes.head() if self.changes is not None else 0
        with self.engine.connect() as conn:
            r = conn.execute(select(result_cache).where(result_cache.c.key == key)).first()
        if r is not None and r.expires_ms > _now() and self._fresh(urns, r.seq):
            with self.engine.begin() as conn:
                conn.execute(update(result_cache).where(result_cache.c.key == key).values(hits=result_cache.c.hits + 1))
            out = json.loads(r.payload)
            if self.audit is not None:
                self.audit.record("data", "data.queried", actor, connection,
                                  {"tables": urns, "rows": len(out.get("rows") or []), "cached": True,
                                   "masked": out.get("masked_columns") or []})
            return {**out, "cached": True}, None
        return None, (key, urns, seq)

    def put(self, ticket: Optional[tuple], out: dict) -> None:
        if ticket is None:
            return
        key, urns, seq = ticket
        payload = json.dumps(out, default=str)
        if len(payload) > self.max_bytes:
            return
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(delete(result_cache).where((result_cache.c.key == key) | (result_cache.c.expires_ms < now)))
            conn.execute(insert(result_cache).values(key=key, urns=json.dumps(urns), seq=seq, payload=payload, hits=0,
                                                     created_ms=now, expires_ms=now + self.ttl_ms))

    def stats(self) -> dict:
        with self.engine.connect() as conn:
            rows = conn.execute(select(result_cache.c.hits, result_cache.c.payload)).all()
        return {"entries": len(rows), "hits": sum(r.hits for r in rows), "bytes": sum(len(r.payload) for r in rows)}

    def clear(self) -> int:
        with self.engine.begin() as conn:
            return conn.execute(delete(result_cache)).rowcount
