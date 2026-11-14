"""Trino's lineage, kept when Cairn Core cannot take it: Trino sends each event once and never again, so
an event Cairn cannot write to Core waits in Cairn's database (``cairn_trino_lineage_outbox``) and is
written when Core is back.

* Held events are answered 202, so Trino does not log a failure for lineage Cairn still has.
* A background loop retries due rows with backoff (15 s doubling to 15 min). A replica claims a row
  before it tries, so two replicas never write the same event at once.
* An event Core rejects as invalid is dropped and audited. So is an event still unwritten after
  ``max_age`` (7 days by default).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, func, select, update

from cairn.common.db import JSONText, metadata

logger = logging.getLogger(__name__)

trino_lineage_outbox = Table(
    "cairn_trino_lineage_outbox", metadata,
    Column("id", String(40), primary_key=True),
    Column("events", JSONText, nullable=False),
    Column("received_ms", BigInteger, nullable=False),
    Column("next_ms", BigInteger, nullable=False, index=True),
    Column("attempts", Integer, nullable=False, default=0),
    Column("last_error", Text, nullable=True),
)

BASE_DELAY_MS = 15_000
MAX_DELAY_MS = 15 * 60_000
CLAIM_MS = 5 * 60_000


def _now() -> int:
    return int(time.time() * 1000)


class LineageOutbox:
    def __init__(self, engine, write: Callable[[list], dict], audit=None, max_age_s: int = 7 * 86400,
                 actor: str = "urn:li:corpuser:cairn-trino"):
        self.engine, self.write, self.audit, self.actor = engine, write, audit, actor
        self.max_age_ms = max_age_s * 1000

    def hold(self, events: list, error: str) -> str:
        """Keeps events Core could not take. Raises if the database cannot keep them either."""
        rid = uuid.uuid4().hex
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(trino_lineage_outbox.insert().values(id=rid, events=events, received_ms=now,
                                                              next_ms=now + BASE_DELAY_MS, attempts=0,
                                                              last_error=error[:2000]))
        return rid

    def pending(self) -> dict:
        with self.engine.connect() as conn:
            n, oldest = conn.execute(select(func.count(), func.min(trino_lineage_outbox.c.received_ms))).one()
        return {"pending": int(n or 0), "oldest_ms": oldest}

    def _claim(self, now: int, limit: int) -> list:
        t = trino_lineage_outbox
        with self.engine.connect() as conn:
            due = conn.execute(select(t.c.id, t.c.next_ms).where(t.c.next_ms <= now).order_by(t.c.next_ms).limit(limit)).all()
        claimed = []
        for rid, next_ms in due:
            with self.engine.begin() as conn:
                # Whoever moves next_ms first owns the row until CLAIM_MS passes.
                if conn.execute(update(t).where(t.c.id == rid, t.c.next_ms == next_ms)
                                .values(next_ms=now + CLAIM_MS)).rowcount == 1:
                    row = conn.execute(select(t).where(t.c.id == rid)).one()
                    claimed.append(row)
        return claimed

    def drain(self, limit: int = 50, now: Optional[int] = None) -> dict:
        """Writes due events to Core. Returns what happened."""
        from cairn.sources.openlineage import OpenLineageError

        t = trino_lineage_outbox
        now = _now() if now is None else now
        done = failed = dropped = 0
        for row in self._claim(now, limit):
            try:
                self.write(row.events)
            except OpenLineageError as e:
                self._drop(row, f"rejected: {e}")
                dropped += 1
                continue
            except Exception as e:  # Core still down: try again later
                if now - row.received_ms > self.max_age_ms:
                    self._drop(row, f"expired after {row.attempts + 1} attempts: {e}")
                    dropped += 1
                    continue
                delay = min(MAX_DELAY_MS, BASE_DELAY_MS * 2 ** min(row.attempts + 1, 10))
                with self.engine.begin() as conn:
                    conn.execute(update(t).where(t.c.id == row.id).values(
                        attempts=row.attempts + 1, next_ms=now + delay, last_error=str(e)[:2000]))
                failed += 1
                continue
            with self.engine.begin() as conn:
                conn.execute(delete(t).where(t.c.id == row.id))
            done += 1
        try:
            from cairn.common import metrics

            metrics.TRINO_LINEAGE_HELD.set(self.pending()["pending"])
        except Exception:  # noqa: BLE001 - a metric never stops the drain
            pass
        if done:
            logger.info("wrote %d held Trino lineage batch(es) to Cairn Core", done)
        return {"written": done, "retrying": failed, "dropped": dropped}

    def _drop(self, row, why: str) -> None:
        logger.warning("dropping held Trino lineage %s: %s", row.id, why)
        with self.engine.begin() as conn:
            conn.execute(delete(trino_lineage_outbox).where(trino_lineage_outbox.c.id == row.id))
        if self.audit is not None:
            jobs = sorted({(e.get("job") or {}).get("name", "") for e in row.events if isinstance(e, dict)})[:20]
            self.audit.record("lineage", "trino.lineage_dropped", self.actor, None,
                              {"batch": row.id, "events": len(row.events), "jobs": jobs, "reason": why[:500]})
