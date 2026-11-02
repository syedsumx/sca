"""Dashboards and SQL alerts, over saved queries.

* A **dashboard** is a named, ordered set of saved queries shown with their charts. Opening it runs every
  tile as the viewer, so a shared dashboard shows each person only what they may see.
* An **alert** checks a saved query on a schedule, as its owner, and tells the owner (and anyone listed)
  when its condition starts to hold and when it stops: ``rows`` (the number of rows) or a column of the
  first row, compared with ``op`` and ``value``. A query that fails is reported as an error once.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Float, Integer, String, Table, Text, and_, delete, or_, select, update

from cairn.common.db import metadata
from cairn.sqlworkspace.workspace import WorkspaceError

logger = logging.getLogger(__name__)

dashboards = Table(
    "cairn_dashboards", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("name", String(256), nullable=False),
    Column("description", String(2000), nullable=False, default=""),
    Column("shared", Boolean, nullable=False, default=False),
    Column("tiles", Text, nullable=False),          # [{"query": id, "title": str, "width": 1|2}]
    Column("updated_ms", BigInteger, nullable=False),
)

sql_alerts = Table(
    "cairn_sql_alerts", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("name", String(256), nullable=False),
    Column("query", String(32), nullable=False),
    Column("measure", String(256), nullable=False),   # "rows" or a column name
    Column("op", String(4), nullable=False),
    Column("value", Float, nullable=False),
    Column("every_minutes", Integer, nullable=False),
    Column("recipients", Text, nullable=False, default="[]"),
    Column("state", String(16), nullable=False, default="unknown"),   # ok | triggered | error | unknown
    Column("last_value", Float, nullable=True),
    Column("last_error", String(1000), nullable=True),
    Column("last_checked_ms", BigInteger, nullable=True),
    Column("next_check_ms", BigInteger, nullable=False, index=True),
    Column("enabled", Boolean, nullable=False, default=True),
)

OPS: dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b, "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
}


def _now() -> int:
    return int(time.time() * 1000)


def _board(r) -> dict:
    return {"id": r.id, "owner": r.owner, "name": r.name, "description": r.description, "shared": bool(r.shared),
            "tiles": json.loads(r.tiles), "updated_ms": r.updated_ms}


def _alert(r) -> dict:
    return {"id": r.id, "owner": r.owner, "name": r.name, "query": r.query, "measure": r.measure, "op": r.op,
            "value": r.value, "every_minutes": r.every_minutes, "recipients": json.loads(r.recipients or "[]"),
            "state": r.state, "last_value": r.last_value, "last_error": r.last_error,
            "last_checked_ms": r.last_checked_ms, "next_check_ms": r.next_check_ms, "enabled": bool(r.enabled)}


class Boards:
    def __init__(self, engine, workspace, notify: Optional[Callable[..., Any]] = None):
        """``notify(type, title, body, recipients, payload)`` tells people about an alert."""
        self.engine, self.ws, self.notify = engine, workspace, notify

    # ---- dashboards ---------------------------------------------------------------------
    def save_dashboard(self, actor: str, body: dict, did: Optional[str] = None) -> dict:
        name = str(body.get("name") or "").strip()
        tiles = body.get("tiles") or []
        if not name or not isinstance(tiles, list) or not tiles:
            raise WorkspaceError("a dashboard needs a name and at least one tile")
        clean = []
        for t in tiles[:24]:
            q = self.ws.get(actor, str((t or {}).get("query") or ""))      # each tile: a query the owner may open
            clean.append({"query": q["id"], "title": str(t.get("title") or q["name"])[:256],
                          "width": 2 if t.get("width") == 2 else 1})
        values = {"name": name[:256], "description": str(body.get("description") or "")[:2000],
                  "shared": bool(body.get("shared")), "tiles": json.dumps(clean), "updated_ms": _now()}
        with self.engine.begin() as conn:
            if did is None:
                did = uuid.uuid4().hex
                conn.execute(dashboards.insert().values(id=did, owner=actor, **values))
            elif not conn.execute(update(dashboards).where(and_(dashboards.c.id == did, dashboards.c.owner == actor))
                                  .values(**values)).rowcount:
                raise WorkspaceError("no such dashboard of yours", 404)
        return self.dashboard(actor, did)

    def dashboards(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(dashboards).where(or_(dashboards.c.owner == actor, dashboards.c.shared))
                                .order_by(dashboards.c.updated_ms.desc())).all()
        return [_board(r) for r in rows]

    def dashboard(self, actor: str, did: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(dashboards).where(dashboards.c.id == did)).first()
        if r is None or (r.owner != actor and not r.shared):
            raise WorkspaceError("no such dashboard", 404)
        return _board(r)

    def delete_dashboard(self, actor: str, did: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(dashboards).where(and_(dashboards.c.id == did, dashboards.c.owner == actor))).rowcount:
                raise WorkspaceError("no such dashboard of yours", 404)

    def render(self, actor: str, did: str) -> dict:
        """Every tile run as the viewer; a tile they may not see shows its error, not its data."""
        board = self.dashboard(actor, did)
        tiles = []
        for t in board["tiles"]:
            try:
                out = self.ws.run_saved(actor, t["query"], 1000)
                q = out.pop("query")
                tiles.append({**t, "chart": q["chart"], "result": out})
            except WorkspaceError as e:
                tiles.append({**t, "error": str(e), "status": e.status})
        return {**board, "tiles": tiles}

    # ---- alerts ---------------------------------------------------------------------------
    def save_alert(self, actor: str, body: dict, aid: Optional[str] = None) -> dict:
        q = self.ws.get(actor, str(body.get("query") or ""))
        op, measure = str(body.get("op") or ""), str(body.get("measure") or "rows").strip()
        if op not in OPS:
            raise WorkspaceError(f"op is one of {' '.join(OPS)}")
        try:
            value, every = float(body.get("value")), int(body.get("every_minutes") or 60)
        except (TypeError, ValueError):
            raise WorkspaceError("value is a number, every_minutes a whole number")
        if not 5 <= every <= 7 * 24 * 60:
            raise WorkspaceError("every_minutes is between 5 and 10080")
        recipients = [str(r) for r in (body.get("recipients") or [])][:50]
        values = {"name": str(body.get("name") or q["name"])[:256], "query": q["id"], "measure": measure[:256], "op": op,
                  "value": value, "every_minutes": every, "recipients": json.dumps(recipients),
                  "enabled": bool(body.get("enabled", True))}
        with self.engine.begin() as conn:
            if aid is None:
                aid = uuid.uuid4().hex
                conn.execute(sql_alerts.insert().values(id=aid, owner=actor, state="unknown", next_check_ms=_now(), **values))
            elif not conn.execute(update(sql_alerts).where(and_(sql_alerts.c.id == aid, sql_alerts.c.owner == actor))
                                  .values(**values)).rowcount:
                raise WorkspaceError("no such alert of yours", 404)
        return self.alert(actor, aid)

    def alerts(self, actor: str) -> list[dict]:
        with self.engine.connect() as conn:
            return [_alert(r) for r in conn.execute(select(sql_alerts).where(sql_alerts.c.owner == actor)).all()]

    def alert(self, actor: str, aid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(sql_alerts).where(and_(sql_alerts.c.id == aid, sql_alerts.c.owner == actor))).first()
        if r is None:
            raise WorkspaceError("no such alert of yours", 404)
        return _alert(r)

    def delete_alert(self, actor: str, aid: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(sql_alerts).where(and_(sql_alerts.c.id == aid, sql_alerts.c.owner == actor))).rowcount:
                raise WorkspaceError("no such alert of yours", 404)

    def check(self, aid: str, now: Optional[int] = None) -> dict:
        """Runs one alert's query as its owner and records (and tells) any change of state."""
        now = now or _now()
        with self.engine.connect() as conn:
            r = conn.execute(select(sql_alerts).where(sql_alerts.c.id == aid)).first()
        a = _alert(r)
        value, error = None, None
        try:
            out = self.ws.run_saved(a["owner"], a["query"], 1000)
            if a["measure"] == "rows":
                value = float(len(out["rows"]))
            else:
                if a["measure"] not in out["columns"]:
                    raise WorkspaceError(f"the query has no column {a['measure']!r}")
                cell = out["rows"][0][out["columns"].index(a["measure"])] if out["rows"] else None
                value = float(cell) if cell is not None else None
            state = "triggered" if value is not None and OPS[a["op"]](value, a["value"]) else "ok"
        except (WorkspaceError, ValueError, TypeError) as e:
            state, error = "error", str(e)[:1000]
        with self.engine.begin() as conn:
            conn.execute(update(sql_alerts).where(sql_alerts.c.id == aid).values(
                state=state, last_value=value, last_error=error, last_checked_ms=now,
                next_check_ms=now + a["every_minutes"] * 60_000))
        if state != a["state"] and self.notify is not None and not (a["state"] == "unknown" and state == "ok"):
            who = [a["owner"], *a["recipients"]]
            measure = "rows" if a["measure"] == "rows" else a["measure"]
            if state == "triggered":
                self.notify("sql.alert_triggered", f"Alert: {a['name']}",
                            f"{measure} is {value:g} ({a['op']} {a['value']:g}).", who, {"alert": aid, "value": value})
            elif state == "ok":
                self.notify("sql.alert_resolved", f"Resolved: {a['name']}",
                            f"{measure} is {value:g}: the condition no longer holds.", who, {"alert": aid, "value": value})
            else:
                self.notify("sql.alert_triggered", f"Alert failed: {a['name']}", f"The query failed: {error}", who,
                            {"alert": aid, "error": error})
        return {**a, "state": state, "last_value": value, "last_error": error, "last_checked_ms": now}

    def run_due(self, now: Optional[int] = None) -> int:
        """Checks every enabled alert that is due (the scheduler's leader calls this each tick)."""
        now = now or _now()
        with self.engine.connect() as conn:
            due = conn.execute(select(sql_alerts.c.id).where(and_(sql_alerts.c.enabled, sql_alerts.c.next_check_ms <= now))
                               ).scalars().all()
        for aid in due:
            try:
                self.check(aid, now)
            except Exception:  # noqa: BLE001 - one alert never stops the others
                logger.exception("sql alert %s failed", aid)
        return len(due)


def build_boards_router(boards: Boards, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/sql", tags=["sql"])

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "dashboards and alerts are for people")
        return p.urn

    def wrap(fn):
        try:
            return fn()
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/dashboards")
    def list_boards(p=Depends(current_principal)):
        return boards.dashboards(person(p))

    @router.post("/dashboards", status_code=201)
    def create_board(body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: boards.save_dashboard(person(p), body))

    @router.put("/dashboards/{did}")
    def change_board(did: str, body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: boards.save_dashboard(person(p), body, did))

    @router.delete("/dashboards/{did}", status_code=204)
    def delete_board(did: str, p=Depends(current_principal)):
        wrap(lambda: boards.delete_dashboard(person(p), did))

    @router.get("/dashboards/{did}/render")
    async def render(did: str, p=Depends(current_principal)):
        actor = person(p)
        return await run_in_threadpool(lambda: wrap(lambda: boards.render(actor, did)))

    @router.get("/alerts")
    def list_alerts(p=Depends(current_principal)):
        return boards.alerts(person(p))

    @router.post("/alerts", status_code=201)
    def create_alert(body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: boards.save_alert(person(p), body))

    @router.put("/alerts/{aid}")
    def change_alert(aid: str, body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: boards.save_alert(person(p), body, aid))

    @router.delete("/alerts/{aid}", status_code=204)
    def delete_alert(aid: str, p=Depends(current_principal)):
        wrap(lambda: boards.delete_alert(person(p), aid))

    @router.post("/alerts/{aid}/check")
    async def check_now(aid: str, p=Depends(current_principal)):
        actor = person(p)
        await run_in_threadpool(lambda: wrap(lambda: boards.alert(actor, aid)))
        return await run_in_threadpool(lambda: boards.check(aid))

    return router
