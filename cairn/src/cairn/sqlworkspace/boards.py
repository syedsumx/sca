"""Dashboards and SQL alerts, over saved queries and workbook elements.

* A **dashboard** is a named, ordered set of tiles shown with their charts: a saved query
  (``{"query": id}``) or a workbook element (``{"element": {...}, "connection": c}``). Opening it runs every
  tile as the viewer, so a shared dashboard shows each person only what they may see.
* **Interactive**: dashboard ``controls`` (``values``, ``range``, ``date_range``, ``text``) filter every tile
  with that column (or the tiles listed in ``targets``); clicking a value in a tile cross-filters the others
  (``selection``); an element tile with ``drill`` (a list of columns, such as region → country → city)
  goes one level down per value clicked. ``POST …/render`` with ``{"controls", "selection", "drill"}``
  runs the tiles so filtered. Filters reach SQL only as quoted columns and literals: a saved query is
  wrapped (``SELECT * FROM (<query>) WHERE …``), an element gets them as its own filters.
* An **alert** checks a saved query on a schedule, as its owner, and tells the owner (and anyone listed)
  when its condition starts to hold and when it stops: ``rows`` (the number of rows) or a column of the
  first row, compared with ``op`` and ``value``. A query that fails is reported as an error once.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, Float, Integer, String, Table, Text, and_, delete, or_, select, update

from cairn.common.db import metadata
from cairn.sqlworkspace.formulas import ident
from cairn.sqlworkspace.workbooks import WorkbookError, filter_sql
from cairn.sqlworkspace.workspace import WorkspaceError, _chart

logger = logging.getLogger(__name__)

dashboards = Table(
    "cairn_dashboards", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("name", String(256), nullable=False),
    Column("description", String(2000), nullable=False, default=""),
    Column("shared", Boolean, nullable=False, default=False),
    Column("tiles", Text, nullable=False),          # [{"query": id | "element": {...}, "title", "width", "chart"}]
    Column("controls", Text, nullable=False, default="[]"),
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


def _board(r, level: int = 0) -> dict:
    from cairn.sqlworkspace.sharing import role_name

    return {"id": r.id, "owner": r.owner, "role": role_name(level), "name": r.name, "description": r.description, "shared": bool(r.shared),
            "tiles": json.loads(r.tiles), "controls": json.loads(r.controls or "[]"), "updated_ms": r.updated_ms}


CONTROL_KINDS = ("values", "range", "date_range", "text")
CONTROL_ID = re.compile(r"^[a-z0-9_-]{1,40}$")


def _scalar(v: Any, what: str) -> Any:
    if v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 1000):
        return v
    raise WorkspaceError(f"{what}: text, a number, true/false or null")


def control_filters(control: dict, value: Any) -> list[dict]:
    """The filters a control's value stands for (none when it is empty)."""
    col, kind = control["column"], control["kind"]
    if value is None or value == "" or value == []:
        return []
    if kind == "values":
        if not isinstance(value, list) or len(value) > 1000:
            raise WorkspaceError(f"{control['id']}: a list of values")
        return [{"column": col, "op": "in", "values": [_scalar(v, control["id"]) for v in value]}]
    if kind in ("range", "date_range"):
        if not isinstance(value, list) or len(value) != 2:
            raise WorkspaceError(f"{control['id']}: [from, to] (either may be null)")
        low, high = (_scalar(v, control["id"]) for v in value)
        return ([{"column": col, "op": ">=", "value": low}] if low not in (None, "") else []) + \
            ([{"column": col, "op": "<=", "value": high}] if high not in (None, "") else [])
    return [{"column": col, "op": "contains", "value": str(_scalar(value, control["id"]))}]


def _alert(r) -> dict:
    return {"id": r.id, "owner": r.owner, "name": r.name, "query": r.query, "measure": r.measure, "op": r.op,
            "value": r.value, "every_minutes": r.every_minutes, "recipients": json.loads(r.recipients or "[]"),
            "state": r.state, "last_value": r.last_value, "last_error": r.last_error,
            "last_checked_ms": r.last_checked_ms, "next_check_ms": r.next_check_ms, "enabled": bool(r.enabled)}


class Boards:
    def __init__(self, engine, workspace, notify: Optional[Callable[..., Any]] = None, workbooks=None):
        """``notify(type, title, body, recipients, payload)`` tells people about an alert; ``workbooks``
        (:class:`cairn.sqlworkspace.workbooks.Workbooks`) runs element tiles."""
        self.engine, self.ws, self.notify, self.wb = engine, workspace, notify, workbooks
        self.metrics = None          # cairn.sqlworkspace.metrics.Metrics, for metric tiles (set when it is built)
        self.sharing = None          # cairn.sqlworkspace.sharing.Sharing (people and groups)
        self.on_change = None        # (kind, id) after a save or delete

    # ---- dashboards ---------------------------------------------------------------------
    def save_dashboard(self, actor: str, body: dict, did: Optional[str] = None) -> dict:
        name = str(body.get("name") or "").strip()
        tiles = body.get("tiles") or []
        if not name or not isinstance(tiles, list) or not tiles:
            raise WorkspaceError("a dashboard needs a name and at least one tile")
        from cairn.sqlworkspace.sharing import OWNER, ROLES, access

        current = None
        if did is not None:
            with self.engine.connect() as conn:
                current = conn.execute(select(dashboards).where(dashboards.c.id == did)).first()
            level = access(self.sharing, actor, "dashboard", current)
            if level < ROLES["EDITOR"]:
                raise WorkspaceError("no such dashboard", 404) if not level else \
                    WorkspaceError("only its owner and editors change a dashboard", 403)
        # Tiles already on the board stay (an editor need not be able to open the owner's queries); new ones
        # are checked as the person saving.
        keep = {t.get("query") for t in json.loads(current.tiles)} - {None} if current is not None else set()
        clean = [self._tile(actor, t, keep) for t in tiles[:24]]
        controls = self._controls(body.get("controls") or [], len(clean))
        values = {"name": name[:256], "description": str(body.get("description") or "")[:2000],
                  "shared": bool(body.get("shared")), "tiles": json.dumps(clean), "controls": json.dumps(controls),
                  "updated_ms": _now()}
        with self.engine.begin() as conn:
            if did is None:
                did = uuid.uuid4().hex
                conn.execute(dashboards.insert().values(id=did, owner=actor, **values))
            else:
                if level < OWNER:
                    values["shared"] = bool(current.shared)     # who sees it is the owner's to decide
                if not conn.execute(update(dashboards).where(dashboards.c.id == did).values(**values)).rowcount:
                    raise WorkspaceError("no such dashboard", 404)
        if self.on_change is not None:
            self.on_change("dashboard", did)
        return self.dashboard(actor, did)

    def dashboards(self, actor: str) -> list[dict]:
        from cairn.sqlworkspace.sharing import access

        visible = [dashboards.c.owner == actor, dashboards.c.shared]
        if self.sharing is not None:
            visible.append(dashboards.c.id.in_(self.sharing.ids(actor, "dashboard")))
        with self.engine.connect() as conn:
            rows = conn.execute(select(dashboards).where(or_(*visible)).order_by(dashboards.c.updated_ms.desc())).all()
        return [_board(r, access(self.sharing, actor, "dashboard", r)) for r in rows]

    def dashboard(self, actor: str, did: str) -> dict:
        from cairn.sqlworkspace.sharing import access

        with self.engine.connect() as conn:
            r = conn.execute(select(dashboards).where(dashboards.c.id == did)).first()
        level = access(self.sharing, actor, "dashboard", r)
        if not level:
            raise WorkspaceError("no such dashboard", 404)
        return _board(r, level)

    def delete_dashboard(self, actor: str, did: str) -> None:
        with self.engine.begin() as conn:
            if not conn.execute(delete(dashboards).where(and_(dashboards.c.id == did, dashboards.c.owner == actor))).rowcount:
                raise WorkspaceError("no such dashboard of yours", 404)
            if self.sharing is not None:
                self.sharing.drop("dashboard", did, conn)
        if self.on_change is not None:
            self.on_change("dashboard", did)

    def _tile(self, actor: str, t: Any, keep: frozenset | set = frozenset()) -> dict:
        if not isinstance(t, dict):
            raise WorkspaceError("a tile is an object")
        out: dict[str, Any] = {"width": 2 if t.get("width") == 2 else 1}
        if t.get("chart") is not None:
            out["chart"] = json.loads(_chart(t.get("chart")))
        if t.get("metric") is not None:
            if self.metrics is None:
                raise WorkspaceError("metric tiles are not available here")
            from cairn.sqlworkspace.metrics import MetricError

            try:
                self.metrics.compile(actor, t["metric"])               # metrics that exist, a query that compiles
            except (MetricError, WorkbookError) as e:
                raise WorkspaceError(f"tile: {e}", e.status)
            q = t["metric"]
            return {**out, "metric": q,
                    "title": str(t.get("title") or ", ".join(q.get("metrics") or []))[:256]}
        if t.get("element") is not None:
            if self.wb is None:
                raise WorkspaceError("workbook tiles are not available here")
            connection = str(t.get("connection") or "")
            try:
                self.wb.compile(actor, connection, t["element"])       # a valid element over a table the owner may query
            except WorkbookError as e:
                raise WorkspaceError(f"tile: {e}", e.status)
            drill = t.get("drill") or []
            if not isinstance(drill, list) or len(drill) > 6 or not all(isinstance(c, str) and c for c in drill):
                raise WorkspaceError("drill: up to 6 column names, from the top level down")
            if drill and [str(g).lower() for g in (t["element"].get("group_by") or [])[:1]] != [drill[0].lower()]:
                raise WorkspaceError("drill: the element is grouped by the first drill column")
            return {**out, "element": t["element"], "connection": connection, "drill": drill,
                    "title": str(t.get("title") or (t["element"].get("source") or {}).get("table") or "Tile")[:256]}
        qid = str(t.get("query") or "")
        q = self.ws.get(actor, qid, through_dashboard=qid in keep)  # a new tile: a query the person saving may open
        return {**out, "query": q["id"], "title": str(t.get("title") or q["name"])[:256]}

    @staticmethod
    def _controls(raw: Any, tiles: int) -> list[dict]:
        if not isinstance(raw, list) or len(raw) > 12:
            raise WorkspaceError("controls: up to 12")
        out, seen = [], set()
        for c in raw:
            if not isinstance(c, dict):
                raise WorkspaceError("a control is an object")
            cid = str(c.get("id") or "")
            if not CONTROL_ID.match(cid) or cid in seen:
                raise WorkspaceError("control id: a new short name (a-z, 0-9, _ and -)")
            seen.add(cid)
            column = str(c.get("column") or "").strip()
            if not column or len(column) > 128 or '"' in column:
                raise WorkspaceError(f"{cid}: column is a column name")
            kind = c.get("kind")
            if kind not in CONTROL_KINDS:
                raise WorkspaceError(f"{cid}: kind is one of {', '.join(CONTROL_KINDS)}")
            targets = c.get("targets")
            if targets is not None and (not isinstance(targets, list) or
                                        not all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < tiles
                                                for i in targets)):
                raise WorkspaceError(f"{cid}: targets are tile positions (from 0)")
            source = c.get("source")
            if kind == "values":
                if not isinstance(source, dict) or not source.get("connection") or not source.get("table"):
                    raise WorkspaceError(f"{cid}: a values control lists the values of source {{connection, table}}")
                source = {"connection": str(source["connection"]), "table": str(source["table"])}
            control = {"id": cid, "label": str(c.get("label") or column)[:128], "column": column, "kind": kind,
                       "targets": targets, "source": source if kind == "values" else None, "default": None}
            if c.get("default") is not None:
                control_filters(control, c.get("default"))                 # checked like a value
                control["default"] = c["default"]
            out.append(control)
        return out

    def render(self, actor: str, did: str, state: Optional[dict] = None) -> dict:
        """Every tile run as the viewer, filtered by the controls' values, the selection (cross-filter) and its
        drill level; a tile they may not see shows its error, not its data."""
        board = self.dashboard(actor, did)
        state = state if isinstance(state, dict) else {}
        values = state.get("controls") if isinstance(state.get("controls"), dict) else {}
        selection = state.get("selection") or []
        drill = state.get("drill") if isinstance(state.get("drill"), dict) else {}
        cache = not state.get("fresh")                        # "fresh": run every tile again, past the cache
        if not isinstance(selection, list) or len(selection) > 12:
            raise WorkspaceError("selection: up to 12 {column, value, from}")
        wanted: list[tuple[dict, Optional[list]]] = []          # (filter, tiles it is for; None: every tile)
        for c in board["controls"]:
            v = values.get(c["id"], c.get("default"))
            wanted += [(f, c["targets"]) for f in control_filters(c, v)]
        for sel in selection:
            if not isinstance(sel, dict) or not isinstance(sel.get("column"), str):
                raise WorkspaceError("selection: {column, value, from}")
            v = _scalar(sel.get("value"), "selection")
            f = {"column": sel["column"], "op": "is_null"} if v is None else {"column": sel["column"], "op": "=", "value": v}
            frm = sel.get("from")
            wanted.append((f, [i for i in range(len(board["tiles"])) if i != frm]))
        tiles = []
        for i, t in enumerate(board["tiles"]):
            filters = [f for f, only in wanted if only is None or i in only]
            try:
                if t.get("metric") is not None:
                    tiles.append(self._run_metric(actor, t, filters))
                elif t.get("element") is not None:
                    tiles.append(self._run_element(actor, t, filters, drill.get(str(i)) or [], cache))
                else:
                    tiles.append(self._run_query(actor, t, filters, cache))
            except Exception as e:  # noqa: BLE001 - one tile's error shows in that tile
                if not hasattr(e, "status"):
                    raise
                tiles.append({**t, "error": str(e), "status": e.status})
        return {**board, "tiles": tiles}

    def _run_element(self, actor: str, t: dict, filters: list[dict], path: list, cache: bool = True) -> dict:
        el = json.loads(json.dumps(t["element"]))
        table = (el.get("source") or {}).get("table", "")
        have = {c.lower() for c in self.wb.columns_of(actor, t["connection"], table)}
        have |= {str((c or {}).get("name") or "").lower() for c in el.get("columns") or []}
        levels = t.get("drill") or []
        if not isinstance(path, list) or (levels and len(path) >= len(levels)):
            raise WorkspaceError(f"drill: at most {max(len(levels) - 1, 0)} values down")
        if path:
            filters = filters + [{"column": levels[k], "op": "=", "value": _scalar(v, "drill")} if v is not None
                                 else {"column": levels[k], "op": "is_null"} for k, v in enumerate(path)]
            top, now = levels[0], levels[len(path)]
            el["group_by"] = [now] + [g for g in el.get("group_by") or [] if g.lower() != top.lower()][0:]
            el["sort"] = [{**s, "column": now} if str(s.get("column", "")).lower() == top.lower() else s
                          for s in el.get("sort") or []]
        applied = [f for f in filters if str(f["column"]).lower() in have]
        el["filters"] = list(el.get("filters") or []) + [{**f, "on": "rows"} for f in applied]
        out = self.wb.run(actor, t["connection"], el, cache=cache)
        sql = out.pop("sql")
        chart = t.get("chart") or {"type": "table", "x": "", "y": []}
        if path and chart.get("x", "").lower() == levels[0].lower():
            chart = {**chart, "x": levels[len(path)]}
        return {**t, "chart": chart, "result": out, "sql": sql, "applied": applied,
                "level": {"path": path, "column": levels[len(path)] if levels else None}}

    def _run_metric(self, actor: str, t: dict, filters: list[dict]) -> dict:
        """A metric tile: the metric query, with the controls and clicks on the metric's table's columns added."""
        q = dict(t["metric"])
        defs = [self.metrics.get(n) for n in q.get("metrics") or []]
        have = {c.lower() for c in self.wb.columns_of(actor, defs[0]["connection"], defs[0]["table"])} if defs else set()
        applied = [f for f in filters if str(f["column"]).lower() in have]
        q["filters"] = list(q.get("filters") or []) + applied
        out = self.metrics.query(actor, q)
        dims = q.get("dimensions") or []
        default = ({"type": "line", "x": "period", "y": q["metrics"]} if q.get("grain") else
                   {"type": "bar", "x": dims[0], "y": q["metrics"]} if dims else {"type": "number", "x": "", "y": q["metrics"][:1]})
        return {**t, "chart": t.get("chart") or default, "result": out, "applied": applied}

    def _run_query(self, actor: str, t: dict, filters: list[dict], cache: bool = True) -> dict:
        q = self.ws.get(actor, t["query"], through_dashboard=True)    # a tile of a board the viewer opened
        applied: list[dict] = []
        if filters:
            inner = q["sql"].strip().rstrip(";")
            columns = self.ws.run(actor, f"SELECT * FROM ({inner}) AS tile", q["connection"], 1)["columns"]
            by = {c.lower(): c for c in columns}
            applied = [f for f in filters if str(f["column"]).lower() in by]
        if applied:
            where = " AND ".join(filter_sql(f, lambda c: ident(by[str(c).lower()])) for f in applied)
            out = self.ws.run(actor, f"SELECT * FROM ({inner}) AS tile WHERE {where}", q["connection"], 1000,
                              cache=cache)
        else:
            out = self.ws.run(actor, q["sql"], q["connection"], 1000, cache=cache)
        return {**t, "chart": t.get("chart") or q["chart"], "result": out, "applied": applied}

    def control_values(self, actor: str, did: str, cid: str) -> list:
        """A values control's choices: the column's values in its source, as the viewer sees them."""
        board = self.dashboard(actor, did)
        c = next((x for x in board["controls"] if x["id"] == cid), None)
        if c is None or c["kind"] != "values":
            raise WorkspaceError("no such values control", 404)
        if self.wb is None:
            raise WorkspaceError("workbooks are not available here")
        try:
            out = self.wb.run(actor, c["source"]["connection"], {
                "source": {"table": c["source"]["table"]}, "group_by": [c["column"]],
                "aggregates": [{"name": "n", "formula": "Count()"}], "sort": [{"column": c["column"]}], "limit": 500})
        except WorkbookError as e:
            raise WorkspaceError(str(e), e.status)
        return [r[0] for r in out.get("rows") or []]

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

    @router.get("/dashboards/{did}")
    def get_board(did: str, p=Depends(current_principal)):
        return wrap(lambda: boards.dashboard(person(p), did))

    @router.delete("/dashboards/{did}", status_code=204)
    def delete_board(did: str, p=Depends(current_principal)):
        wrap(lambda: boards.delete_dashboard(person(p), did))

    @router.get("/dashboards/{did}/render")
    async def render(did: str, p=Depends(current_principal)):
        actor = person(p)
        return await run_in_threadpool(lambda: wrap(lambda: boards.render(actor, did)))

    @router.post("/dashboards/{did}/render")
    async def render_with(did: str, body: dict = Body(...), p=Depends(current_principal)):
        actor = person(p)
        return await run_in_threadpool(lambda: wrap(lambda: boards.render(actor, did, body)))

    @router.get("/dashboards/{did}/controls/{cid}/values")
    async def control_values(did: str, cid: str, p=Depends(current_principal)):
        actor = person(p)
        return await run_in_threadpool(lambda: wrap(lambda: boards.control_values(actor, did, cid)))

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
