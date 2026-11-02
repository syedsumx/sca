"""Lakehouse table maintenance: Iceberg's own procedures, run through Trino on a schedule.

For each table, the policy that applies (the table's own, else its schema's, else ``*``) says what to do:

* ``optimize``: rewrite small files into bigger ones (``ALTER TABLE … EXECUTE optimize``);
* ``expire_days``: expire snapshots older than that (``expire_snapshots``), so old data files can go;
* ``orphan_days``: delete files no snapshot references, older than that (``remove_orphan_files``).

It runs every ``every_hours`` per table on a run-as-user Trino connection (CAIRN_MAINTENANCE_CONNECTION)
as the Cairn user CAIRN_MAINTENANCE_USER (default svc-maintenance), so Cairn's policy decides as for
anyone: that user needs manage on the tables. Trino keeps its own floors (``iceberg.expire-snapshots.min-retention``
and ``iceberg.remove-orphan-files.min-retention``, 7 days by default). Each operation is recorded, with its
outcome; policy changes are audited.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Table, Text, delete, func, select

from cairn.common.db import metadata

logger = logging.getLogger(__name__)

maintenance_policies = Table(
    "cairn_maintenance_policies", metadata,
    Column("scope", String(512), primary_key=True),      # "*", "<schema>" or "<schema>.<table>"
    Column("optimize", Boolean, nullable=False, default=True),
    Column("optimize_threshold", String(16), nullable=False, default="128MB"),
    Column("expire_days", Integer, nullable=True),
    Column("orphan_days", Integer, nullable=True),
    Column("every_hours", Integer, nullable=False, default=24),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("updated_by", String(256), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

maintenance_runs = Table(
    "cairn_maintenance_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("table_name", String(512), nullable=False, index=True),     # <schema>.<table>
    Column("operation", String(32), nullable=False),
    Column("status", String(16), nullable=False),                      # ok | error
    Column("started_ms", BigInteger, nullable=False, index=True),
    Column("finished_ms", BigInteger, nullable=False),
    Column("detail", Text, nullable=False, default=""),
)

NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_$-]{0,127}$")
SIZE = re.compile(r"^\d{1,6}(B|kB|MB|GB)$")


class MaintenanceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> int:
    return int(time.time() * 1000)


def _policy(r) -> dict:
    return {"scope": r.scope, "optimize": bool(r.optimize), "optimize_threshold": r.optimize_threshold,
            "expire_days": r.expire_days, "orphan_days": r.orphan_days, "every_hours": r.every_hours,
            "enabled": bool(r.enabled), "updated_by": r.updated_by, "updated_ms": r.updated_ms}


class TableMaintenance:
    def __init__(self, engine, execute: Callable[[str], list], catalog: str = "lakehouse", audit=None):
        """``execute(sql)``: runs one statement on Trino as the maintenance user and returns its rows."""
        self.engine, self.execute, self.catalog, self.audit = engine, execute, catalog, audit

    # ---- policies -----------------------------------------------------------------------
    def policies(self) -> list[dict]:
        with self.engine.connect() as conn:
            return [_policy(r) for r in conn.execute(select(maintenance_policies).order_by(maintenance_policies.c.scope))]

    def set_policy(self, scope: str, body: dict, actor: str) -> dict:
        parts = scope.split(".")
        if scope != "*" and not (1 <= len(parts) <= 2 and all(NAME.match(p) for p in parts)):
            raise MaintenanceError("scope is *, a schema or schema.table")
        def days(k):
            v = body.get(k)
            if v is None:
                return None
            if not isinstance(v, int) or not 0 <= v <= 3650:
                raise MaintenanceError(f"{k} is a number of days (0-3650) or null")
            return v
        every = body.get("every_hours", 24)
        if not isinstance(every, int) or not 1 <= every <= 24 * 30:
            raise MaintenanceError("every_hours is between 1 and 720")
        threshold = str(body.get("optimize_threshold") or "128MB")
        if not SIZE.match(threshold):
            raise MaintenanceError("optimize_threshold is a size such as 128MB")
        values = {"optimize": bool(body.get("optimize", True)), "optimize_threshold": threshold,
                  "expire_days": days("expire_days"), "orphan_days": days("orphan_days"), "every_hours": every,
                  "enabled": bool(body.get("enabled", True)), "updated_by": actor, "updated_ms": _now()}
        with self.engine.begin() as conn:
            conn.execute(delete(maintenance_policies).where(maintenance_policies.c.scope == scope))
            conn.execute(maintenance_policies.insert().values(scope=scope, **values))
            if self.audit is not None:
                self.audit.record("lakehouse", "maintenance.policy_set", actor, None, {"scope": scope, **values}, conn=conn)
        return {"scope": scope, **values}

    def delete_policy(self, scope: str, actor: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(maintenance_policies).where(maintenance_policies.c.scope == scope)).rowcount:
                raise MaintenanceError("no such policy", 404)
            if self.audit is not None:
                self.audit.record("lakehouse", "maintenance.policy_deleted", actor, None, {"scope": scope}, conn=conn)

    def policy_for(self, schema: str, table: str) -> Optional[dict]:
        by_scope = {p["scope"]: p for p in self.policies()}
        return by_scope.get(f"{schema}.{table}") or by_scope.get(schema) or by_scope.get("*")

    # ---- running ------------------------------------------------------------------------
    def tables(self) -> list[tuple[str, str]]:
        rows = self.execute(f'SELECT table_schema, table_name FROM "{self.catalog}".information_schema.tables '
                            "WHERE table_type = 'BASE TABLE' AND table_schema <> 'information_schema'")
        return [(r[0], r[1]) for r in rows]

    def maintain(self, schema: str, table: str, policy: Optional[dict] = None) -> list[dict]:
        """Runs the table's policy now; each operation is recorded, and one failing does not stop the rest."""
        if not (NAME.match(schema) and NAME.match(table)):
            raise MaintenanceError("not a table name")
        policy = policy or self.policy_for(schema, table)
        if policy is None:
            raise MaintenanceError(f"no maintenance policy applies to {schema}.{table}", 404)
        target = f'"{self.catalog}"."{schema}"."{table}"'
        ops = []
        if policy["optimize"]:
            ops.append(("optimize", f"ALTER TABLE {target} EXECUTE optimize(file_size_threshold => '{policy['optimize_threshold']}')"))
        if policy["expire_days"] is not None:
            ops.append(("expire_snapshots", f"ALTER TABLE {target} EXECUTE "
                                            f"expire_snapshots(retention_threshold => '{policy['expire_days']}d')"))
        if policy["orphan_days"] is not None:
            ops.append(("remove_orphan_files", f"ALTER TABLE {target} EXECUTE "
                                               f"remove_orphan_files(retention_threshold => '{policy['orphan_days']}d')"))
        out = []
        for op, sql in ops:
            started = _now()
            try:
                self.execute(sql)
                status, detail = "ok", ""
            except Exception as e:  # noqa: BLE001 - recorded, and the next operation still runs
                status, detail = "error", str(e).splitlines()[0][:2000]
                logger.warning("maintenance %s on %s.%s failed: %s", op, schema, table, detail)
            row = {"id": uuid.uuid4().hex, "table_name": f"{schema}.{table}", "operation": op, "status": status,
                   "started_ms": started, "finished_ms": _now(), "detail": detail}
            with self.engine.begin() as conn:
                conn.execute(maintenance_runs.insert().values(**row))
            out.append(row)
        return out

    def last_run(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            return {t: ms for t, ms in conn.execute(select(maintenance_runs.c.table_name, func.max(maintenance_runs.c.started_ms))
                                                    .group_by(maintenance_runs.c.table_name))}

    def run_due(self, now: Optional[int] = None) -> int:
        """Maintains every table whose policy is due (the scheduler's leader calls this)."""
        now = now or _now()
        if not self.policies():
            return 0
        last = self.last_run()
        done = 0
        for schema, table in self.tables():
            p = self.policy_for(schema, table)
            if p is None or not p["enabled"]:
                continue
            if now - last.get(f"{schema}.{table}", 0) < p["every_hours"] * 3_600_000:
                continue
            try:
                self.maintain(schema, table, p)
                done += 1
            except Exception:  # noqa: BLE001 - one table never stops the others
                logger.exception("maintenance of %s.%s failed", schema, table)
        return done

    def runs(self, table: Optional[str] = None, limit: int = 200) -> list[dict]:
        q = select(maintenance_runs).order_by(maintenance_runs.c.started_ms.desc()).limit(limit)
        if table:
            q = q.where(maintenance_runs.c.table_name == table)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]


def build_maintenance_router(m: TableMaintenance, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/maintenance", tags=["lakehouse"])

    def admin(p):
        authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        return p.urn

    def wrap(fn):
        try:
            return fn()
        except MaintenanceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/policies")
    def policies(p=Depends(current_principal)):
        admin(p)
        return m.policies()

    @router.put("/policies/{scope}")
    def set_policy(scope: str, body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: m.set_policy(scope, body, admin(p)))

    @router.delete("/policies/{scope}", status_code=204)
    def delete_policy(scope: str, p=Depends(current_principal)):
        wrap(lambda: m.delete_policy(scope, admin(p)))

    @router.post("/run")
    async def run(body: dict = Body(...), p=Depends(current_principal)):
        admin(p)
        return await run_in_threadpool(lambda: wrap(lambda: m.maintain(str(body.get("schema") or ""),
                                                                        str(body.get("table") or ""))))

    @router.get("/runs")
    def runs(table: Optional[str] = None, p=Depends(current_principal)):
        admin(p)
        return m.runs(table)

    return router
