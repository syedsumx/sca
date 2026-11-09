"""SQL workflows: scheduled SQL in the product. Steps that depend on each other, run as the workflow's owner.

A workflow is a set of named steps, each optionally ``after`` others (a DAG; cycles are refused), run on a
``cron`` schedule (UTC) or on demand:

* ``derived``: refresh a derived table (``name``; ``full`` for a full rebuild). Only the derived table's
  owner (or an operator) may put it in a workflow; that is how SQL that writes runs on a schedule.
* ``check``: a saved query's measure (``rows``, or a column of the first row) compared with ``op`` and
  ``value``; the step fails when it does not hold (a quality gate between steps).
* ``query``: a saved query (or ``sql`` on a ``connection``), run as the owner, its row count and first 20
  rows kept with the run and sent to the recipients (a scheduled report).

A step whose dependency failed is skipped; ``retries`` (0-3) retries a failing step. Each run is kept
(``GET /v1/sql-workflows/<id>/runs``) with each step's status, rows, error and time. The owner and
``recipients`` are told when a run fails, or after every run with ``notify: always``. Queries run through the
SQL workspace as the owner, so their policies, masks and row filters apply.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Boolean, Column, String, Table, Text, and_, delete, select, update

from cairn.common.db import metadata
from cairn.sqlworkspace.boards import OPS
from cairn.sqlworkspace.workspace import WorkspaceError

logger = logging.getLogger(__name__)

sql_workflows = Table(
    "cairn_sql_workflows", metadata,
    Column("id", String(32), primary_key=True),
    Column("owner", String(256), nullable=False, index=True),
    Column("name", String(256), nullable=False),
    Column("steps", Text, nullable=False),
    Column("cron", String(128), nullable=True),
    Column("recipients", Text, nullable=False, default="[]"),
    Column("notify", String(16), nullable=False, default="failure"),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("next_run_ms", BigInteger, nullable=True, index=True),
    Column("last_status", String(16), nullable=True),
    Column("updated_ms", BigInteger, nullable=False),
)
sql_workflow_runs = Table(
    "cairn_sql_workflow_runs", metadata,
    Column("id", String(32), primary_key=True),
    Column("workflow_id", String(32), nullable=False, index=True),
    Column("trigger", String(16), nullable=False),
    Column("status", String(16), nullable=False),
    Column("started_ms", BigInteger, nullable=False),
    Column("finished_ms", BigInteger, nullable=True),
    Column("steps", Text, nullable=False),
)

TYPES = ("derived", "check", "query")


def _now() -> int:
    return int(time.time() * 1000)


def _next(cron: Optional[str], after_ms: int) -> Optional[int]:
    if not cron:
        return None
    from datetime import datetime, timezone

    from croniter import croniter

    return int(croniter(cron, datetime.fromtimestamp(after_ms / 1000, tz=timezone.utc)).get_next(float) * 1000)


def order(steps: list[dict]) -> list[dict]:
    """The steps in an order where each comes after what it depends on; WorkspaceError on a cycle."""
    by = {s["name"]: s for s in steps}
    done: list[dict] = []
    state: dict[str, int] = {}

    def visit(n: str, path: tuple):
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            raise WorkspaceError(f"steps depend on each other in a cycle: {' -> '.join(path + (n,))}")
        state[n] = 1
        for a in by[n]["after"]:
            visit(a, path + (n,))
        state[n] = 2
        done.append(by[n])
    for s in steps:
        visit(s["name"], ())
    return done


class Workflows:
    def __init__(self, engine, workspace, derived=None, notify: Optional[Callable[..., Any]] = None,
                 operator: Optional[Callable[[str], bool]] = None):
        """``derived``: DerivedTables (refresh); ``notify(type, title, body, recipients, payload)``;
        ``operator(actor)``: may this person put anyone's derived table in a workflow."""
        self.engine, self.ws, self.derived, self.notify = engine, workspace, derived, notify
        self.operator = operator or (lambda actor: False)

    # ---- definitions --------------------------------------------------------------------
    def _check_steps(self, actor: str, raw: Any) -> list[dict]:
        if not isinstance(raw, list) or not 1 <= len(raw) <= 50:
            raise WorkspaceError("steps: 1 to 50 steps")
        steps, names = [], set()
        for i, s in enumerate(raw):
            if not isinstance(s, dict):
                raise WorkspaceError(f"step {i}: an object")
            name, kind = str(s.get("name") or "").strip(), s.get("type")
            if not name or name in names or len(name) > 128:
                raise WorkspaceError(f"step {i}: a name of its own")
            if kind not in TYPES:
                raise WorkspaceError(f"step {name}: type is one of {', '.join(TYPES)}")
            after = s.get("after") or []
            after = [after] if isinstance(after, str) else list(after)
            retries = s.get("retries", 0)
            if not isinstance(retries, int) or not 0 <= retries <= 3:
                raise WorkspaceError(f"step {name}: retries is 0-3")
            step = {"name": name, "type": kind, "after": [str(a) for a in after], "retries": retries}
            if kind == "derived":
                if self.derived is None:
                    raise WorkspaceError("derived tables are not set up here", 503)
                dt = next((d for d in self.derived.list() if d["name"] == s.get("table")), None)
                if dt is None:
                    raise WorkspaceError(f"step {name}: no derived table {s.get('table')!r}", 404)
                if dt["owner"] != actor and not self.operator(actor):
                    raise WorkspaceError(f"step {name}: {dt['name']} is not yours to refresh", 403)
                step.update(table=dt["name"], derived_id=dt["id"], full=bool(s.get("full")))
            else:
                if s.get("query"):
                    q = self.ws.get(actor, str(s["query"]))            # a saved query the owner may open
                    step.update(query=q["id"])
                elif kind == "query" and s.get("sql") and s.get("connection"):
                    step.update(sql=str(s["sql"]), connection=str(s["connection"]))
                else:
                    raise WorkspaceError(f"step {name}: a saved query (or, for a query step, sql and a connection)")
                if kind == "check":
                    op = str(s.get("op") or "")
                    if op not in OPS:
                        raise WorkspaceError(f"step {name}: op is one of {' '.join(OPS)}")
                    try:
                        value = float(s.get("value"))
                    except (TypeError, ValueError):
                        raise WorkspaceError(f"step {name}: value is a number")
                    step.update(measure=str(s.get("measure") or "rows"), op=op, value=value)
            names.add(name)
            steps.append(step)
        for s in steps:
            for a in s["after"]:
                if a not in names:
                    raise WorkspaceError(f"step {s['name']}: no step {a!r} to come after")
        order(steps)
        return steps

    def save(self, actor: str, body: dict, wid: Optional[str] = None) -> dict:
        name = str(body.get("name") or "").strip()
        if not name:
            raise WorkspaceError("a workflow needs a name")
        steps = self._check_steps(actor, body.get("steps"))
        cron = str(body.get("cron") or "").strip() or None
        if cron:
            from croniter import croniter

            if not croniter.is_valid(cron):
                raise WorkspaceError("cron: a five-field cron expression (UTC)")
            nxt = _next(cron, _now())
            if _next(cron, nxt) - nxt < 5 * 60_000:
                raise WorkspaceError("cron: at most every five minutes")
        notify = str(body.get("notify") or "failure")
        if notify not in ("failure", "always"):
            raise WorkspaceError("notify is failure or always")
        rec = [str(r) for r in body.get("recipients") or [] if str(r).startswith("urn:li:corp")][:50]
        values = {"name": name[:256], "steps": json.dumps(steps), "cron": cron, "recipients": json.dumps(rec),
                  "notify": notify, "enabled": bool(body.get("enabled", True)), "next_run_ms": _next(cron, _now()),
                  "updated_ms": _now()}
        with self.engine.begin() as conn:
            if wid is None:
                wid = uuid.uuid4().hex
                conn.execute(sql_workflows.insert().values(id=wid, owner=actor, **values))
            elif not conn.execute(update(sql_workflows).where(and_(sql_workflows.c.id == wid, sql_workflows.c.owner == actor))
                                  .values(**values)).rowcount:
                raise WorkspaceError("no such workflow of yours", 404)
        return self.get(actor, wid)

    @staticmethod
    def _public(r) -> dict:
        return {"id": r.id, "owner": r.owner, "name": r.name, "steps": json.loads(r.steps), "cron": r.cron,
                "recipients": json.loads(r.recipients or "[]"), "notify": r.notify, "enabled": bool(r.enabled),
                "next_run_ms": r.next_run_ms, "last_status": r.last_status, "updated_ms": r.updated_ms}

    def get(self, actor: str, wid: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(sql_workflows).where(sql_workflows.c.id == wid)).first()
        if r is None or (r.owner != actor and not self.operator(actor)):
            raise WorkspaceError("no such workflow", 404)
        return self._public(r)

    def list(self, actor: str) -> list[dict]:
        q = select(sql_workflows).order_by(sql_workflows.c.name)
        if not self.operator(actor):
            q = q.where(sql_workflows.c.owner == actor)
        with self.engine.connect() as conn:
            return [self._public(r) for r in conn.execute(q)]

    def delete(self, actor: str, wid: str) -> None:
        wf = self.get(actor, wid)                      # its owner, or an operator
        if wf["owner"] != actor and not self.operator(actor):
            raise WorkspaceError("no such workflow of yours", 404)
        with self.engine.begin() as conn:
            conn.execute(delete(sql_workflows).where(sql_workflows.c.id == wid))
            conn.execute(delete(sql_workflow_runs).where(sql_workflow_runs.c.workflow_id == wid))

    def runs(self, actor: str, wid: str, limit: int = 50) -> list[dict]:
        self.get(actor, wid)
        with self.engine.connect() as conn:
            rows = conn.execute(select(sql_workflow_runs).where(sql_workflow_runs.c.workflow_id == wid)
                                .order_by(sql_workflow_runs.c.started_ms.desc()).limit(limit)).all()
        return [{"id": r.id, "trigger": r.trigger, "status": r.status, "started_ms": r.started_ms,
                 "finished_ms": r.finished_ms, "steps": json.loads(r.steps)} for r in rows]

    # ---- running --------------------------------------------------------------------------
    def _step(self, owner: str, s: dict) -> dict:
        """One step, through the same executors as Cairn Flows (cairn.flows.steps), as the owner."""
        from cairn.flows import steps as flow_steps

        try:
            if s["type"] == "derived":
                out = flow_steps.refresh_derived(self.derived, self.operator, owner, s.get("table", ""),
                                                 s.get("full", False), "workflow", did=s["derived_id"])
                return {"rows": out.get("rows"), "detail": out.get("detail")}
            if s["type"] == "check":
                out = flow_steps.check(self.ws, owner, s)
                return {"rows": out["rows"], "detail": out["detail"]}
            out = flow_steps.run_query(self.ws, owner, s, 1000)
        except flow_steps.StepFailed as e:
            raise WorkspaceError(str(e))
        return {"rows": out["rows"], "columns": out["columns"], "preview": out["_rows"][:20]}

    def run(self, wid: str, trigger: str = "manual") -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(sql_workflows).where(sql_workflows.c.id == wid)).first()
        if r is None:
            raise WorkspaceError("no such workflow", 404)
        wf = self._public(r)
        rid, started = uuid.uuid4().hex, _now()
        results: dict[str, dict] = {}
        for s in order(wf["steps"]):
            failed_dep = next((a for a in s["after"] if results.get(a, {}).get("status") != "ok"), None)
            if failed_dep is not None:
                results[s["name"]] = {"name": s["name"], "status": "skipped", "error": f"{failed_dep} did not succeed"}
                continue
            t0, attempt, err, out = time.monotonic(), 0, None, {}
            while True:
                try:
                    out = self._step(wf["owner"], s)
                    err = None
                    break
                except Exception as e:  # noqa: BLE001 - the step fails; the run records why
                    err = str(e)[:2000] or type(e).__name__
                    if attempt >= s["retries"]:
                        break
                    attempt += 1
                    time.sleep(min(2 ** attempt, 10))
            results[s["name"]] = {"name": s["name"], "type": s["type"], "status": "error" if err else "ok", "error": err,
                                  "attempts": attempt + 1, "ms": int((time.monotonic() - t0) * 1000), **out}
        status = "ok" if all(v["status"] == "ok" for v in results.values()) else "failed"
        steps = [results[s["name"]] for s in wf["steps"]]
        with self.engine.begin() as conn:
            conn.execute(sql_workflow_runs.insert().values(id=rid, workflow_id=wid, trigger=trigger, status=status,
                                                           started_ms=started, finished_ms=_now(), steps=json.dumps(steps, default=str)))
            conn.execute(update(sql_workflows).where(sql_workflows.c.id == wid).values(last_status=status))
        if self.notify is not None and (status == "failed" or wf["notify"] == "always"):
            lines = [f"{v['name']}: {v['status']}" + (f" ({v['error']})" if v.get("error") else
                                                      f" ({v['rows']} rows)" if v.get("rows") is not None else "")
                     for v in steps]
            try:
                self.notify("sql.workflow_failed" if status == "failed" else "sql.workflow_finished",
                            f"Workflow {wf['name']} {'failed' if status == 'failed' else 'finished'}", "\n".join(lines),
                            [wf["owner"], *[x for x in wf["recipients"] if x != wf["owner"]]],
                            {"workflow": wid, "run": rid, "status": status})
            except Exception as e:  # noqa: BLE001 - the run is recorded either way
                logger.warning("workflow %s: notification failed: %s", wf["name"], e)
        return {"id": rid, "workflow": wid, "status": status, "steps": steps}

    def run_due(self, now: Optional[int] = None) -> int:
        now = now or _now()
        with self.engine.connect() as conn:
            due = conn.execute(select(sql_workflows.c.id, sql_workflows.c.cron).where(
                sql_workflows.c.enabled.is_(True), sql_workflows.c.next_run_ms.is_not(None),
                sql_workflows.c.next_run_ms <= now)).all()
        n = 0
        for wid, cron in due:
            with self.engine.begin() as conn:            # claim it: another scheduler replica skips it
                if not conn.execute(update(sql_workflows).where(and_(sql_workflows.c.id == wid, sql_workflows.c.next_run_ms <= now))
                                    .values(next_run_ms=_next(cron, now))).rowcount:
                    continue
            try:
                self.run(wid, "schedule")
                n += 1
            except Exception:  # noqa: BLE001 - one workflow never stops the others
                logger.exception("workflow %s failed to run", wid)
        return n


def build_workflows_router(wf: Workflows, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/sql-workflows", tags=["sql"])

    def person(p):
        if p.is_service or p.via in ("agent", "extension"):
            raise HTTPException(403, "workflows belong to people and pipeline users")
        return p.urn

    def wrap(fn):
        try:
            return fn()
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_(p=Depends(current_principal)):
        return wf.list(person(p))

    @router.post("", status_code=201)
    def create(body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: wf.save(person(p), body))

    @router.put("/{wid}")
    def change(wid: str, body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: wf.save(person(p), body, wid))

    @router.delete("/{wid}", status_code=204)
    def remove(wid: str, p=Depends(current_principal)):
        wrap(lambda: wf.delete(person(p), wid))

    @router.post("/{wid}/run")
    async def run_now(wid: str, p=Depends(current_principal)):
        actor = person(p)
        wrap(lambda: wf.get(actor, wid))
        return await run_in_threadpool(lambda: wrap(lambda: wf.run(wid, "manual")))

    @router.get("/{wid}/runs")
    def runs(wid: str, p=Depends(current_principal)):
        return wrap(lambda: wf.runs(person(p), wid))

    return router
