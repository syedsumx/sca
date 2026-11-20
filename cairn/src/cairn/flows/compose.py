"""Flows that compose (L173): a step that runs another flow, and steps that run what moves data.

* ``flow``: starts a run of another flow and (unless ``wait: false``) waits for it. The parent's owner must be allowed
  to run it; it runs as its own owner does, with ``params`` filled from the parent's templates. Its result is the
  step's output: the child run's id, status and each of its steps' outputs, so later steps read
  ``steps.<name>.output.steps.<child step>``. A failed or cancelled child fails the step; cancelling the parent
  cancels the child. A flow may not reach itself through sub-flows (checked when it is published), and sub-flows nest
  at most ``MAX_DEPTH`` deep (checked when they run).
* ``mirror``: brings a mirror up to date now (``cairn.pipelines.mirror``). Like ``pipe``, the flow's owner needs
  ``MANAGE_SYSTEM_OPERATIONS``.
* ``saas_sync`` and ``reverse_sync``: run a SaaS load or a reverse sync now and wait for it, as the flow's owner
  (who must be allowed to run it).
* ``maintenance``: runs a lakehouse table's maintenance policy now (compaction, snapshot expiry, clustering);
  operators only.

* ``sensor``: waits, without holding a worker, until something is there: a query's measure holds (``sql`` or a saved
  ``query``, with ``measure``, ``op``, ``value``; by default: it returns a row), a table has rows matching ``where``
  (a partition has landed), or a ``volume`` has a file under ``prefix``. Everything is read as the flow's owner. It
  looks every ``poll_s`` and fails when its ``timeout_s`` passes; a query that errors fails at once.

With these one flow covers ingest (mirror, SaaS sync, pipe), transform (derived, dbt, SQL) and activate (reverse
sync), each step still what its owner may do.
"""

from __future__ import annotations

import json
import time
from typing import Any

from cairn.flows import steps
from cairn.flows.steps import EXECUTORS, Backends, Ctx, Done, StepFailed, Wait, need

MAX_DEPTH = 5
KEPT = 20_000


def _flow(b: Backends, ctx: Ctx) -> Any:
    flows = need(getattr(b, "flows", None), "flows")
    s = ctx.step
    child = ctx.state.get("run")
    if child is None:
        depth = int((flows._run_row(ctx.run_id).get("detail") or {}).get("depth") or 0)
        if depth >= MAX_DEPTH:
            raise StepFailed(f"sub-flows nest at most {MAX_DEPTH} deep")
        try:
            run = flows.run(ctx.owner, s["flow"], dict(s.get("params") or {}), trigger="api")
        except Exception as e:  # noqa: BLE001 - refused (not theirs to run, turned off, bad params): the step's failure
            raise StepFailed(f"the flow {s['flow']} was not started: {e}")
        child = run["id"]
        flows._set_detail(child, {**(run.get("detail") or {}), "parent_run": ctx.run_id, "parent_flow": ctx.flow,
                                  "parent_step": s["name"], "depth": depth + 1})
        ctx.log(f"started run {child} of {s['flow']}")
        if not s.get("wait", True):
            return Done({"flow": s["flow"], "run": child, "status": run["status"]})
    run = flows._run_public(child)
    if run["status"] in ("queued", "running", "waiting"):
        return Wait(int(time.time() * 1000) + int(s.get("poll_s", 2)) * 1000, {"run": child})
    if run["status"] != "succeeded":
        failed = next((x for x in run["steps"] if x["status"] == "failed"), None)
        raise StepFailed(f"the run {child} of {s['flow']} {run['status']}"
                         + (f": {failed['key']}: {failed['error']}" if failed else ""))
    outputs = {x["key"]: x["output"] for x in run["steps"] if x["output"] is not None}
    if len(json.dumps(outputs, default=str)) > KEPT:              # a large child: its steps' statuses, not their rows
        outputs = {x["key"]: {"status": x["status"]} for x in run["steps"]}
    return Done({"flow": s["flow"], "run": child, "status": run["status"], "steps": outputs})


def cancel_child(b: Backends, owner: str, state: dict) -> int:
    """Cancels the run a ``flow`` step started; 1 when it was asked. Never raises."""
    child = (state or {}).get("run")
    flows = getattr(b, "flows", None)
    if not child or flows is None:
        return 0
    try:
        flows.cancel(owner, child)
        return 1
    except Exception:  # noqa: BLE001 - it already finished, or is not theirs any more
        return 0


def _operator(b: Backends, ctx: Ctx, what: str) -> None:
    if not b.is_operator(ctx.owner):
        raise StepFailed(f"{what} needs MANAGE_SYSTEM_OPERATIONS, which the flow's owner does not have")


def _mirror(b: Backends, ctx: Ctx) -> Done:
    mirrors = need(getattr(b, "mirrors", None), "mirrors")
    _operator(b, ctx, "running a mirror")
    name = ctx.step["mirror"]
    try:
        rows = mirrors.run(name)
    except Exception as e:  # noqa: BLE001 - the mirror's own error, as its page shows it
        raise StepFailed(f"the mirror {name} did not run: {str(e)[:500]}")
    ctx.log(f"{rows} changes applied")
    return Done({"mirror": name, "changes": int(rows or 0)})


def _sync(b: Backends, ctx: Ctx, attr: str, what: str) -> Done:
    svc = need(getattr(b, attr, None), what)
    name = ctx.step["sync"]
    try:
        out = svc.run_now(ctx.owner, name, wait=True)
    except Exception as e:  # noqa: BLE001 - refused (not theirs, already running) or failed
        raise StepFailed(f"{name} did not run: {str(e)[:500]}")
    out = out if isinstance(out, dict) else {}
    status = str(out.get("status") or out.get("last_status") or "")
    if status in ("failed", "error") or out.get("error"):
        raise StepFailed(f"{name} failed: {str(out.get('error') or out.get('last_error') or status)[:500]}")
    keep = {k: v for k, v in out.items() if isinstance(v, (str, int, float, bool)) or v is None}
    ctx.log(f"{name}: {status or 'done'}")
    return Done({"sync": name, **keep})


def _saas(b: Backends, ctx: Ctx) -> Done:
    return _sync(b, ctx, "saas", "SaaS syncs")


def _reverse(b: Backends, ctx: Ctx) -> Done:
    return _sync(b, ctx, "reverse", "reverse syncs")


def _maintenance(b: Backends, ctx: Ctx) -> Done:
    maint = need(getattr(b, "maintenance", None), "table maintenance")
    _operator(b, ctx, "maintaining a table")
    table = str(ctx.step["table"])
    if table.count(".") != 1:
        raise StepFailed("table: schema.table")
    schema, name = table.split(".")
    try:
        ops = maint.maintain(schema, name, actor=ctx.owner)
    except Exception as e:  # noqa: BLE001 - no policy, or it is being maintained now
        raise StepFailed(f"{table} was not maintained: {str(e)[:500]}")
    failed = [o for o in ops if o.get("status") not in (None, "ok", "success", "skipped")]
    if failed:
        raise StepFailed(f"{table}: {failed[0].get('operation')} failed: {str(failed[0].get('error') or '')[:300]}")
    ctx.log(", ".join(str(o.get("operation")) for o in ops) or "nothing to do")
    return Done({"table": table, "operations": [{"operation": o.get("operation"), "status": o.get("status")} for o in ops]})


def _sensor(b: Backends, ctx: Ctx) -> Any:
    """Looks; done when what it waits for is there, else looks again in ``poll_s``. The step's ``timeout_s`` ends it."""
    from cairn.flows.steps import check

    s = ctx.step
    looks = int(ctx.state.get("looks") or 0) + 1
    if s.get("volume"):
        volumes = need(getattr(b, "volumes", None), "volumes")
        try:
            files = volumes.files(ctx.owner, s["volume"], s.get("prefix") or "")["files"]
        except Exception as e:  # noqa: BLE001 - not theirs to read, or no such volume: it will never arrive
            raise StepFailed(f"the volume {s['volume']} could not be read: {str(e)[:300]}")
        if files:
            return Done({"files": len(files), "first": files[0]["path"], "looks": looks})
        what = f"no file under {s['volume']}/{s.get('prefix') or ''} yet"
    else:
        q = dict(s)
        if s.get("table"):
            where = f" WHERE {s['where']}" if s.get("where") else ""
            q.update(sql=f"SELECT count(*) AS matching FROM {s['table']}{where}", measure="matching", query=None)
        try:
            out = check(need(b.workspace, "the SQL workspace"), ctx.owner, q)
            return Done({"value": out["value"], "detail": out["detail"], "looks": looks})
        except StepFailed as e:
            if not str(e).startswith("check failed"):       # the query itself failed: waiting will not help
                raise
            what = str(e).removeprefix("check failed: ")
    if looks == 1:
        ctx.log(f"waiting: {what}")
    return Wait(int(time.time() * 1000) + int(s.get("poll_s", 30)) * 1000, {"looks": looks})


EXECUTORS.update({"sensor": _sensor, "flow": _flow, "mirror": _mirror, "saas_sync": _saas, "reverse_sync": _reverse,
                  "maintenance": _maintenance})
steps.WAITING = tuple(dict.fromkeys((*steps.WAITING, "flow", "sensor")))
