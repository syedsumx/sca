"""Change data feed: a lakehouse table's row changes between two of its snapshots, governed like any read.

``GET /v1/lakehouse/tables/<schema>/<table>/changes?since=<snapshot id or ISO time>`` returns the rows inserted
(``_change_type: insert``) and deleted (``delete``) between ``since`` and ``until`` (the current snapshot by
default), and ``until_snapshot`` to pass as the next ``since``. With ``key`` (columns, comma-separated), a
deletion and an insertion with the same key are an update (``update_preimage`` and ``update_postimage``).
``GET .../snapshots`` lists the table's snapshots (with their record counts, and which is current).

**Snapshot browser and restore.** ``GET .../snapshots/<id or ISO time>/rows`` shows the table as it was at that
snapshot. ``POST .../restore`` with ``{"snapshot": <id or ISO time>, "reason": ...}`` makes that snapshot the
table's current one again (Iceberg's ``rollback_to_snapshot``): later snapshots stay in the history, so a restore
is itself undone by restoring the snapshot that was current before it. A restore runs on Trino as the caller, so
it needs ``MANAGE_DATASET`` on the table, like any table procedure, and it is audited (``table.restored``, with
the snapshot before and after, and the reason).

**As the caller.** Both sides are ordinary time-travel reads (``FOR VERSION AS OF``) run on Trino as whoever
asks, so Cairn's policies, column masks and row filters apply exactly as to any other read: the feed shows
only what the caller could see at each snapshot. Trino's own ``table_changes`` stays refused, since a table
function's output escapes masks and row filters. The changes are net between the two snapshots (a row added
and removed in between does not appear), found by comparing the two (``EXCEPT ALL``): exact, at the cost of
reading both.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Optional

NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
MAX_LIMIT = 10_000


class ChangeFeedError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _q(name: str) -> str:
    return '"' + name + '"'


class ChangeFeed:
    def __init__(self, run: Callable[[str, str, int], tuple[list[str], list[list[Any]], bool]], catalog: str = "lakehouse",
                 audit=None):
        """``run(actor, sql, limit)``: (columns, rows, truncated) of a query on Trino as ``actor``."""
        self.run, self.catalog, self.audit = run, catalog, audit

    def _table(self, schema: str, table: str) -> str:
        if not (NAME.match(schema) and NAME.match(table)):
            raise ChangeFeedError("a table is <schema>/<table>")
        return f"{_q(self.catalog)}.{_q(schema.lower())}.{_q(table.lower())}"

    def snapshots(self, actor: str, schema: str, table: str) -> list[dict]:
        t = self._table(schema, table)
        meta, refs = t[:-1] + '$snapshots"', t[:-1] + '$refs"'
        keys = ("total-records", "total-position-deletes", "total-equality-deletes", "added-records", "deleted-records",
                "added-position-deletes")
        cols, rows, _ = self.run(actor, "SELECT snapshot_id, parent_id, committed_at, operation, "
                                        + ", ".join(f"element_at(summary, '{k}')" for k in keys)
                                        + f" FROM {meta} ORDER BY committed_at, snapshot_id", MAX_LIMIT)
        # The current snapshot is the main branch's ($history shows each snapshot once, at its first time current).
        _, cur, _ = self.run(actor, f"SELECT snapshot_id FROM {refs} WHERE name = 'main'", 1)
        current = int(cur[0][0]) if cur else None

        def num(v) -> int:
            return int(v) if v is not None else 0

        def counts(r) -> dict:
            total, pos, eq, added, deleted, added_pos = (num(v) for v in r[4:10])
            # Deletes may be files of row positions (exact) or of values (unknown rows): rows are known for the first.
            return {"records": total - pos if not eq and r[4] is not None else None, "added": added,
                    "deleted": deleted + added_pos}
        # ``id``: the snapshot id as a string too, since a 64-bit id does not survive a JavaScript number.
        return [{"snapshot": int(r[0]), "id": str(int(r[0])), "parent": int(r[1]) if r[1] is not None else None,
                 "committed_at": r[2].isoformat() if hasattr(r[2], "isoformat") else str(r[2]), "operation": r[3],
                 **counts(r), "current": int(r[0]) == current}
                for r in rows]

    def rows(self, actor: str, schema: str, table: str, at: str, limit: int = 100) -> dict:
        """The table as it was at snapshot (or time) ``at``, as the caller sees it (masks and row filters apply)."""
        if not 1 <= limit <= 1000:
            raise ChangeFeedError("limit is 1-1000")
        t = self._table(schema, table)
        sid = self._resolve(self.snapshots(actor, schema, table), at, "snapshot")
        cols, rows, truncated = self.run(actor, f"SELECT * FROM {t} FOR VERSION AS OF {sid}", limit)
        return {"snapshot": sid, "id": str(sid), "columns": list(cols), "rows": rows, "truncated": truncated}

    def restore(self, actor: str, schema: str, table: str, to: str, reason: str = "") -> dict:
        """Makes snapshot ``to`` the table's current one, as the caller (who needs MANAGE_DATASET on it)."""
        reason = str(reason or "").strip()
        if not reason or len(reason) > 500:
            raise ChangeFeedError("reason: why the table is restored (up to 500 characters)")
        t = self._table(schema, table)
        snaps = self.snapshots(actor, schema, table)
        sid = self._resolve(snaps, str(to), "snapshot")
        before = next((s["snapshot"] for s in snaps if s["current"]), None)
        if sid == before:
            raise ChangeFeedError(f"snapshot {sid} is already the table's current one", 409)
        self.run(actor, f"ALTER TABLE {t} EXECUTE rollback_to_snapshot({sid})", 1)
        after = next((s for s in self.snapshots(actor, schema, table) if s["current"]), None)
        if after is None or after["snapshot"] != sid:
            raise ChangeFeedError("the restore did not take: the table's current snapshot is unchanged", 502)
        if self.audit is not None:
            self.audit.record("pipelines", "table.restored", actor, f"table:{schema.lower()}.{table.lower()}",
                              {"from_snapshot": before, "to_snapshot": sid, "reason": reason})
        _, n, _ = self.run(actor, f"SELECT count(*) FROM {t}", 1)
        return {"table": f"{schema.lower()}.{table.lower()}", "from_snapshot": before, "to_snapshot": sid,
                "from_id": str(before) if before is not None else None, "to_id": str(sid), "records": int(n[0][0])}

    def _resolve(self, snaps: list[dict], value: str, what: str) -> int:
        if re.fullmatch(r"-?\d{1,20}", value):
            sid = int(value)
            if sid not in {s["snapshot"] for s in snaps}:
                raise ChangeFeedError(f"{what}: no snapshot {sid} of this table (expired, or another table's)", 404)
            return sid
        try:
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ChangeFeedError(f"{what}: a snapshot id or an ISO time")
        before = [s for s in snaps if datetime.fromisoformat(s["committed_at"].replace("Z", "+00:00")) <= at]
        if not before:
            raise ChangeFeedError(f"{what}: the table has no snapshot at or before {value}", 404)
        return before[-1]["snapshot"]

    def changes(self, actor: str, schema: str, table: str, since: str, until: Optional[str] = None,
                key: Optional[list[str]] = None, limit: int = 1000) -> dict:
        if not 1 <= limit <= MAX_LIMIT:
            raise ChangeFeedError(f"limit is 1-{MAX_LIMIT}")
        key = [k for k in (key or []) if k]
        if not all(NAME.match(k) for k in key):
            raise ChangeFeedError("key: column names")
        t = self._table(schema, table)
        snaps = self.snapshots(actor, schema, table)
        if not snaps:
            raise ChangeFeedError("the table has no snapshots yet", 404)
        old = self._resolve(snaps, since, "since")
        new = self._resolve(snaps, until, "until") if until else snaps[-1]["snapshot"]
        order = {s["snapshot"]: i for i, s in enumerate(snaps)}
        if order[old] > order[new]:
            raise ChangeFeedError("since is after until")
        if old == new:
            return {"since_snapshot": old, "until_snapshot": new, "columns": [], "changes": [], "truncated": False}
        a, b = f"SELECT * FROM {t} FOR VERSION AS OF {new}", f"SELECT * FROM {t} FOR VERSION AS OF {old}"
        sql = (f"SELECT 'insert' AS _change_type, * FROM ({a} EXCEPT ALL {b}) "
               f"UNION ALL SELECT 'delete' AS _change_type, * FROM ({b} EXCEPT ALL {a})")
        cols, rows, truncated = self.run(actor, sql, limit)
        names = list(cols)
        missing = [k for k in key if k not in names]
        if missing:
            raise ChangeFeedError(f"key: no column {missing[0]!r}")
        out = [dict(zip(names, r)) for r in rows]
        if key:
            ins = {tuple(c[k] for k in key): c for c in out if c["_change_type"] == "insert"}
            for c in out:
                if c["_change_type"] == "delete" and tuple(c[k] for k in key) in ins:
                    c["_change_type"] = "update_preimage"
                    ins[tuple(c[k] for k in key)]["_change_type"] = "update_postimage"
        rank = {"delete": 0, "update_preimage": 1, "update_postimage": 2, "insert": 3}
        out.sort(key=lambda c: (tuple(str(c[k]) for k in key), rank[c["_change_type"]]))
        return {"since_snapshot": old, "until_snapshot": new, "columns": names, "changes": out, "truncated": truncated}


def build_change_feed_router(feed: ChangeFeed, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query
    from starlette.concurrency import run_in_threadpool

    from cairn.dataaccess.service import DataAccessError

    router = APIRouter(prefix="/v1/lakehouse/tables", tags=["lakehouse"])

    def person(p):
        if p.is_service:
            raise HTTPException(403, "a table's changes are read as a person or a pipeline user")
        return p.urn

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except ChangeFeedError as e:
            raise HTTPException(e.status, str(e))
        except DataAccessError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/{schema}/{table}/snapshots")
    async def snapshots(schema: str, table: str, p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: feed.snapshots(actor, schema, table))

    @router.get("/{schema}/{table}/snapshots/{at}/rows")
    async def rows(schema: str, table: str, at: str, limit: int = Query(100, ge=1, le=1000), p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: feed.rows(actor, schema, table, at, limit))

    @router.post("/{schema}/{table}/restore")
    async def restore(schema: str, table: str, snapshot: str = Body(...), reason: str = Body(""),
                      p=Depends(current_principal)):
        actor = person(p)
        return await call(lambda: feed.restore(actor, schema, table, snapshot, reason))

    @router.get("/{schema}/{table}/changes")
    async def changes(schema: str, table: str, since: str, until: Optional[str] = None, key: Optional[str] = None,
                      limit: int = Query(1000, ge=1, le=MAX_LIMIT), p=Depends(current_principal)):
        actor = person(p)
        keys = [k.strip() for k in (key or "").split(",") if k.strip()]
        return await call(lambda: feed.changes(actor, schema, table, since, until, keys, limit))

    return router
