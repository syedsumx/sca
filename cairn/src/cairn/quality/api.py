"""FastAPI router for the quality module (mounted at ``/quality``)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from cairn.common.security import Principal
from cairn.quality.auto import AUTO_PREFIX, DETACHED, load_rules, sweep
from cairn.quality.engine import QualityEngine
from cairn.quality.models import Evaluation, FeedbackRequest, Monitor, MonitorSpec


def build_router(engine: QualityEngine, current_principal, authorize, external=None) -> APIRouter:
    """``authorize(principal, privilege, resource_urn)`` raises HTTPException(403) on deny. ``external``:
    results from other quality tools (quality.external)."""
    router = APIRouter(prefix="/quality", tags=["quality"])
    store = engine.store

    def _get(monitor_id: str) -> Monitor:
        m = store.get_monitor(monitor_id)
        if m is None:
            raise HTTPException(404, f"monitor {monitor_id} not found")
        return m

    @router.get("/auto/rules")
    def auto_rules(p: Principal = Depends(current_principal)):
        return load_rules().model_dump(mode="json")

    @router.post("/auto/sweep")
    def auto_sweep(dry_run: bool = False, p: Principal = Depends(current_principal)):
        """Apply the auto-monitor rules now (the scheduler also does this periodically)."""
        authorize(p, "EDIT_ENTITY_ASSERTIONS", None)
        if engine.graph is None:
            raise HTTPException(503, "Cairn Core not configured")
        return sweep(store, engine, engine.graph, load_rules(), dry_run=dry_run, connections=engine.connections)

    @router.get("/monitors", response_model=list[Monitor])
    def list_monitors(dataset_urn: Optional[str] = None, p: Principal = Depends(current_principal)):
        monitors = store.list_monitors(dataset_urn)
        return [m for m in monitors if authorize(p, "VIEW_ENTITY_PAGE", m.dataset_urn, raise_on_deny=False)]

    @router.post("/monitors", response_model=Monitor, status_code=201)
    def create_monitor(spec: MonitorSpec, p: Principal = Depends(current_principal)):
        authorize(p, "EDIT_ENTITY_ASSERTIONS", spec.dataset_urn)
        m = store.create_monitor(spec, p.urn)
        engine.sync_monitor(m)
        return m

    @router.get("/monitors/{monitor_id}", response_model=Monitor)
    def get_monitor(monitor_id: str, p: Principal = Depends(current_principal)):
        m = _get(monitor_id)
        authorize(p, "VIEW_ENTITY_PAGE", m.dataset_urn)
        return m

    @router.put("/monitors/{monitor_id}", response_model=Monitor)
    def update_monitor(monitor_id: str, spec: MonitorSpec, p: Principal = Depends(current_principal)):
        existing = _get(monitor_id)
        authorize(p, "EDIT_ENTITY_ASSERTIONS", existing.dataset_urn)
        authorize(p, "EDIT_ENTITY_ASSERTIONS", spec.dataset_urn)
        if monitor_id.startswith(AUTO_PREFIX):
            spec.config[DETACHED] = True       # edited by a person: the rule no longer manages it
        m = store.update_monitor(monitor_id, spec)
        engine.sync_monitor(m)  # type: ignore[arg-type]
        return m

    @router.delete("/monitors/{monitor_id}", status_code=204)
    def delete_monitor(monitor_id: str, p: Principal = Depends(current_principal)):
        m = _get(monitor_id)
        authorize(p, "EDIT_ENTITY_ASSERTIONS", m.dataset_urn)
        if monitor_id.startswith(AUTO_PREFIX):
            # Opting a dataset out of a rule: keep a disabled, detached record so the rule does not
            # recreate the monitor on its next sweep.
            spec = MonitorSpec(**m.model_dump(include=set(MonitorSpec.model_fields)))
            spec.enabled = False
            spec.config[DETACHED] = True
            engine.sync_monitor(store.update_monitor(monitor_id, spec))  # type: ignore[arg-type]
            return
        store.delete_monitor(monitor_id)
        engine.remove_monitor(monitor_id)

    @router.post("/monitors/{monitor_id}/run", response_model=Evaluation)
    def run_now(monitor_id: str, p: Principal = Depends(current_principal)):
        m = _get(monitor_id)
        authorize(p, "EDIT_ENTITY_ASSERTIONS", m.dataset_urn)
        return engine.evaluate(m)

    @router.get("/monitors/{monitor_id}/evaluations", response_model=list[Evaluation])
    def evaluations(monitor_id: str, limit: int = Query(100, ge=1, le=1000), p: Principal = Depends(current_principal)):
        m = _get(monitor_id)
        authorize(p, "VIEW_ENTITY_PAGE", m.dataset_urn)
        return store.evaluations(monitor_id, limit)

    @router.post("/evaluations/{evaluation_id}/feedback", response_model=Evaluation)
    def feedback(evaluation_id: str, body: FeedbackRequest, p: Principal = Depends(current_principal)):
        ev = store.get_evaluation(evaluation_id)
        if ev is None:
            raise HTTPException(404, "evaluation not found")
        authorize(p, "EDIT_ENTITY_ASSERTIONS", _get(ev.monitor_id).dataset_urn)
        return store.set_feedback(evaluation_id, body.feedback)

    @router.get("/datasets/{dataset_urn:path}/health")
    def dataset_health(dataset_urn: str, p: Principal = Depends(current_principal)):
        authorize(p, "VIEW_ENTITY_PAGE", dataset_urn)
        monitors = store.list_monitors(dataset_urn)
        rank = {"ERROR": 4, "FAIL": 3, "WARN": 2, "TRAINING": 1, "PASS": 0}
        worst = "PASS"
        open_incidents = 0
        items = []
        for m in monitors:
            last = m.last_evaluation
            if m.enabled and last and rank[last.status] > rank[worst]:
                worst = last.status
            if store.get_state(m.id).get("open_incident_urn"):
                open_incidents += 1
            items.append({"monitor": m.model_dump(exclude={"last_evaluation"}), "last_evaluation": last})
        others = external.for_dataset(dataset_urn) if external is not None else []
        for x in others:                                   # other tools' checks count like monitors
            if rank[x["status"]] > rank[worst]:
                worst = x["status"]
        return {"dataset_urn": dataset_urn, "status": worst if monitors or others else "UNMONITORED",
                "monitors": items, "open_incidents": open_incidents, "external": others}

    @router.post("/external/{tool}")
    async def external_results(tool: str, request: Request, platform: str = "snowflake",
                               dataset: Optional[str] = None, p: Principal = Depends(current_principal)):
        """Results from dbt tests, Great Expectations, Soda, or any tool in the generic format."""
        from starlette.concurrency import run_in_threadpool

        from cairn.quality.external import ExternalError

        authorize(p, "EDIT_ENTITY_ASSERTIONS", None)
        if external is None:
            raise HTTPException(503, "external results are not configured")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "the body is not JSON")
        try:
            return await run_in_threadpool(external.ingest, tool, body, platform, dataset, p.urn)
        except ExternalError as e:
            raise HTTPException(422, str(e))

    return router
