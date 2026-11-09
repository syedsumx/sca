"""``/v1/flows``: definitions (draft, versions, validate, publish), runs (start, cancel, rerun, backfill, approve), and
Airflow both ways: a flow as a DAG file, the steps of runs Airflow carries out, and ``/v1/airflow/dags`` (Airflow's
DAGs as read-only graphs, converted to draft flows)."""

from __future__ import annotations

from typing import Any, Optional

from cairn.flows import model
from cairn.flows.model import FlowError


def build_flows_router(flows, current_principal, sql_workflows=None) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query
    from fastapi.responses import JSONResponse, PlainTextResponse

    router = APIRouter(prefix="/v1/flows", tags=["flows"])

    def person(p, write: bool = True, run: bool = False) -> str:
        """People and pipeline users; agents (acting for a person) may read, and run with the write scope, but not
        define or publish."""
        if p.is_service or p.via == "extension" or (write and p.via == "agent"):
            raise HTTPException(403, "flows are defined by people and pipeline users")
        if run and p.via == "agent" and "write" not in (p.scopes or ()):
            raise HTTPException(403, "running a flow needs the agent's write scope")
        return p.urn

    def wrap(fn):
        try:
            return fn()
        except FlowError as e:
            return JSONResponse({"detail": str(e), "errors": e.errors}, status_code=e.status)

    def trigger_of(p) -> str:
        return "agent" if p.via == "agent" else "api" if p.via in ("token", "pat") else "manual"

    # ---- without a flow ------------------------------------------------------------------------------------
    @router.get("")
    def list_flows(p=Depends(current_principal)):
        return flows.list(person(p, write=False))

    @router.post("", status_code=201)
    def create(body: Any = Body(...), publish: bool = Query(False), note: Optional[str] = Query(None),
               p=Depends(current_principal)):
        return wrap(lambda: flows.create(person(p), body, publish, note))

    @router.get("/step-types")
    def step_types(p=Depends(current_principal)):
        person(p, write=False)
        return model.catalog()

    @router.post("/validate")
    def validate(body: Any = Body(...), p=Depends(current_principal)):
        person(p, write=False)
        try:
            raw, _ = flows._split(body)
        except FlowError as e:
            return {"valid": False, "errors": e.errors, "flow": None, "yaml": None}
        out = model.validate(raw)
        return {**out, "yaml": model.to_yaml(out["flow"]) if out["valid"] else None}

    @router.get("/approvals")
    def approvals(p=Depends(current_principal)):
        return flows.pending_approvals(person(p, write=False))

    @router.post("/from-sql-workflow/{wid}", status_code=201)
    def from_workflow(wid: str, p=Depends(current_principal)):
        actor = person(p)
        if sql_workflows is None:
            raise HTTPException(503, "SQL workflows are not set up here")
        from cairn.sqlworkspace.workspace import WorkspaceError

        try:
            wf = sql_workflows.get(actor, wid)
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))
        return wrap(lambda: flows.create(actor, flows.from_sql_workflow(wf)))

    # ---- runs ------------------------------------------------------------------------------------------------
    @router.get("/runs/{rid}")
    def run_detail(rid: str, p=Depends(current_principal)):
        return wrap(lambda: flows.run_detail(person(p, write=False), rid))

    @router.get("/runs/{rid}/steps/{key}/log")
    def step_log(rid: str, key: str, p=Depends(current_principal)):
        return wrap(lambda: flows.step_log(person(p, write=False), rid, key))

    @router.post("/runs/{rid}/cancel")
    def cancel(rid: str, p=Depends(current_principal)):
        return wrap(lambda: flows.cancel(person(p, write=False, run=True), rid))

    @router.post("/runs/{rid}/rerun")
    def rerun(rid: str, body: dict = Body(default={}), p=Depends(current_principal)):
        return wrap(lambda: flows.rerun(person(p, write=False, run=True), rid, body.get("from_step")))

    @router.post("/runs/{rid}/steps/{step}/execute")
    def execute(rid: str, step: str, body: dict = Body(default={}), p=Depends(current_principal)):
        """One step of a run Airflow carries out, run in Cairn as the flow's owner (its task asks until it has
        finished): the caller may run the flow (owner, editor or admin)."""
        try:
            tries = int(body.get("try_number") or 1)
        except (TypeError, ValueError):
            raise HTTPException(422, "try_number is a number")
        return wrap(lambda: flows.execute_step(person(p, write=False, run=True), rid, step, tries))

    @router.post("/runs/{rid}/steps/{step}/{decision}")
    def decide(rid: str, step: str, decision: str, body: dict = Body(default={}), p=Depends(current_principal)):
        if decision not in ("approve", "reject"):
            raise HTTPException(404, "approve or reject")
        return wrap(lambda: flows.decide(person(p), rid, step, decision, str(body.get("comment") or "")))

    # ---- one flow ----------------------------------------------------------------------------------------------
    @router.get("/{ref}")
    def get(ref: str, p=Depends(current_principal)):
        return wrap(lambda: flows.get(person(p, write=False), ref))

    @router.put("/{ref}")
    def save(ref: str, body: Any = Body(...), publish: bool = Query(False), note: Optional[str] = Query(None),
             p=Depends(current_principal)):
        return wrap(lambda: flows.save(person(p), ref, body, publish, note))

    @router.delete("/{ref}", status_code=204)
    def remove(ref: str, p=Depends(current_principal)):
        out = wrap(lambda: flows.delete(person(p), ref))
        return out if out is not None else None

    @router.post("/{ref}/publish")
    def publish(ref: str, body: dict = Body(default={}), p=Depends(current_principal)):
        return wrap(lambda: flows.publish(person(p), ref, body.get("note")))

    @router.post("/{ref}/enabled")
    def enabled(ref: str, body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: flows.set_enabled(person(p), ref, bool(body.get("enabled"))))

    @router.put("/{ref}/sharing")
    def sharing(ref: str, body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: flows.share(person(p), ref, body.get("editors") or [], body.get("viewers") or []))

    @router.get("/{ref}/versions")
    def versions(ref: str, p=Depends(current_principal)):
        return wrap(lambda: flows.versions(person(p, write=False), ref))

    @router.get("/{ref}/versions/{version}")
    def version(ref: str, version: int, p=Depends(current_principal)):
        return wrap(lambda: flows.version(person(p, write=False), ref, version))

    @router.get("/{ref}/diff")
    def diff(ref: str, from_: Optional[int] = Query(None, alias="from"), to: Optional[str] = Query(None),
             p=Depends(current_principal)):
        return wrap(lambda: flows.diff(person(p, write=False), ref, from_, to))

    @router.get("/{ref}/yaml", response_class=PlainTextResponse)
    def as_yaml(ref: str, version: Optional[int] = Query(None), p=Depends(current_principal)):
        actor = person(p, write=False)

        def one():
            if version:
                return PlainTextResponse(flows.version(actor, ref, version)["yaml"], media_type="application/yaml")
            f = flows.get(actor, ref)
            if f["draft_yaml"] is None:
                raise FlowError("the draft is not valid; fix it first", 409, f["errors"])
            return PlainTextResponse(f["draft_yaml"], media_type="application/yaml")
        return wrap(one)

    @router.get("/{ref}/airflow")
    def as_airflow(ref: str, version: Optional[str] = Query(None), operators: str = Query("native"),
                   conn_id: str = Query("cairn_default"), dag_id: Optional[str] = Query(None),
                   p=Depends(current_principal)):
        """The flow as an Airflow DAG file (cairn-airflow-provider's operators; no secrets): its published version
        (or ``version``: a number or ``draft``). ``operators``: native (Trino, check, dbt, Spark and DAG triggers on
        Airflow) or cairn (every step delegated to Cairn as the owner)."""
        actor = person(p, write=False)

        def one():
            v: Any = version if version in (None, "draft") else int(version) if str(version).isdigit() else "bad"
            if v == "bad":
                raise FlowError("version is a number or draft")
            name, text = flows.airflow_dag(actor, ref, v, operators, conn_id, dag_id)
            return PlainTextResponse(text, media_type="text/x-python; charset=utf-8",
                                     headers={"Content-Disposition": f'attachment; filename="{name}"'})
        return wrap(one)

    @router.post("/{ref}/airflow-runs", status_code=201)
    def airflow_run(ref: str, body: dict = Body(...), p=Depends(current_principal)):
        """Records a DAG run of an exported flow (Airflow started it) as a run of the flow, once per DAG run."""
        actor = person(p, write=False, run=True)
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPException(422, "params is a mapping")
        return wrap(lambda: flows.airflow_run(actor, ref, str(body.get("dag_id") or ""), str(body.get("dag_run_id") or ""),
                                              body.get("logical_date"), params))

    @router.post("/{ref}/runs", status_code=201)
    def start(ref: str, body: dict = Body(default={}), p=Depends(current_principal)):
        actor = person(p, write=False, run=True)
        return wrap(lambda: flows.run(actor, ref, body.get("params") or {}, body.get("version"), trigger_of(p),
                                      bool(body.get("wait")), min(float(body.get("timeout_s") or 600), 3600)))

    @router.get("/{ref}/runs")
    def runs(ref: str, limit: int = Query(50), p=Depends(current_principal)):
        return wrap(lambda: flows.runs(person(p, write=False), ref, limit))

    @router.post("/{ref}/backfill", status_code=201)
    def backfill(ref: str, body: dict = Body(...), p=Depends(current_principal)):
        return wrap(lambda: {"runs": flows.backfill(person(p), ref, str(body.get("start") or ""), str(body.get("end") or ""),
                                                    body.get("param"), body.get("params") or {})})

    return router


def build_airflow_router(dags, current_principal) -> Any:
    """``/v1/airflow/dags``: Airflow's DAGs the caller may see (owners, team tags, or a flow of theirs), each as a
    read-only graph with its recent runs and live task states, and converted to a draft flow when every task maps."""
    from fastapi import APIRouter, Body, Depends, HTTPException, Query
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/airflow", tags=["flows"])

    def person(p) -> str:
        if p.is_service or p.via in ("extension", "agent"):
            raise HTTPException(403, "Airflow's DAGs are shown to people")
        return p.urn

    async def wrap(fn):
        try:
            return await run_in_threadpool(fn)
        except FlowError as e:
            return JSONResponse({"detail": str(e), "errors": e.errors}, status_code=e.status)

    @router.get("/dags")
    async def list_dags(p=Depends(current_principal)):
        actor = person(p)
        return await wrap(lambda: dags.list(actor))

    @router.get("/dags/{dag_id}")
    async def get_dag(dag_id: str, run: Optional[str] = Query(None), p=Depends(current_principal)):
        actor = person(p)
        return await wrap(lambda: dags.get(actor, dag_id, run))

    @router.post("/dags/{dag_id}/convert", status_code=201)
    async def convert(dag_id: str, body: dict = Body(default={}), p=Depends(current_principal)):
        actor = person(p)
        name = body.get("name")
        return await wrap(lambda: dags.convert(actor, dag_id, str(name) if name else None))

    return router
