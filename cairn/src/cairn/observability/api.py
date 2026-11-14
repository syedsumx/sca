"""``/v1/observability``: the health map, request journeys, logs, SLOs, alert rules, alerts, silences, incidents
and deploys (``MANAGE_SYSTEM_OPERATIONS``). Agents (the SRE agent) use the same API with their credentials."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.observability.service import ObservabilityError, ObservabilityService

PRIVILEGE = "MANAGE_SYSTEM_OPERATIONS"


def build_router(svc: ObservabilityService, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/v1/observability", tags=["observability"])

    def who(p) -> Any:
        authorize(p, PRIVILEGE, None)
        return p

    def agent(p) -> bool:
        return getattr(p, "via", "") in ("agent", "agent_key") or bool(getattr(p, "agent", None))

    async def call(fn, *a, **kw):
        try:
            return await run_in_threadpool(fn, *a, **kw)
        except ObservabilityError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/overview")
    async def overview(p=Depends(current_principal)):
        who(p)
        return await call(svc.overview)

    @router.get("/health")
    async def health(window: str = Query("5m", pattern=r"^\d+[smh]$"), p=Depends(current_principal)):
        who(p)
        return await call(svc.health, window)

    @router.get("/traces")
    async def traces(service: str = Query("", max_length=128), errors: bool = False,
                     min_duration_ms: Optional[int] = Query(None, ge=0), since_minutes: int = Query(60, ge=1, le=10080),
                     limit: int = Query(20, ge=1, le=100), p=Depends(current_principal)):
        who(p)
        return await call(svc.traces, service, errors, min_duration_ms, since_minutes, limit)

    @router.get("/journey/{trace_id}")
    async def journey(trace_id: str, p=Depends(current_principal)):
        who(p)
        return await call(svc.journey, trace_id)

    @router.get("/logs")
    async def logs(trace_id: str = Query("", max_length=64), service: str = Query("", max_length=128),
                   q: str = Query("", max_length=200), level: str = Query("", max_length=16),
                   since_minutes: int = Query(60, ge=1, le=10080), limit: int = Query(200, ge=1, le=1000),
                   p=Depends(current_principal)):
        who(p)
        return await call(svc.logs, trace_id, service, q, level, since_minutes, limit)

    # ---- SLOs, alert rules, silences (resource types) --------------------------------------------------------
    @router.get("/slos")
    async def list_slo(p=Depends(current_principal)):
        who(p)
        return {"items": await call(svc.list, "slo")}

    @router.post("/slos", status_code=201)
    async def create_slo(body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.create, "slo", p.urn, body)

    @router.get("/slos/{oid}")
    async def get_slo(oid: str, p=Depends(current_principal)):
        who(p)
        return await call(svc.get, "slo", oid)

    @router.put("/slos/{oid}")
    async def put_slo(oid: str, body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.replace, "slo", p.urn, oid, body)

    @router.delete("/slos/{oid}", status_code=204)
    async def delete_slo(oid: str, p=Depends(current_principal)):
        who(p)
        await call(svc.remove, "slo", p.urn, oid)

    @router.get("/alert-rules")
    async def list_alert_rule(p=Depends(current_principal)):
        who(p)
        return {"items": await call(svc.list, "alert_rule")}

    @router.post("/alert-rules", status_code=201)
    async def create_alert_rule(body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.create, "alert_rule", p.urn, body)

    @router.get("/alert-rules/{oid}")
    async def get_alert_rule(oid: str, p=Depends(current_principal)):
        who(p)
        return await call(svc.get, "alert_rule", oid)

    @router.put("/alert-rules/{oid}")
    async def put_alert_rule(oid: str, body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.replace, "alert_rule", p.urn, oid, body)

    @router.delete("/alert-rules/{oid}", status_code=204)
    async def delete_alert_rule(oid: str, p=Depends(current_principal)):
        who(p)
        await call(svc.remove, "alert_rule", p.urn, oid)

    @router.get("/silences")
    async def list_silence(p=Depends(current_principal)):
        who(p)
        return {"items": await call(svc.list, "silence")}

    @router.post("/silences", status_code=201)
    async def create_silence(body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.create, "silence", p.urn, body)

    @router.get("/silences/{oid}")
    async def get_silence(oid: str, p=Depends(current_principal)):
        who(p)
        return await call(svc.get, "silence", oid)

    @router.put("/silences/{oid}")
    async def put_silence(oid: str, body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.replace, "silence", p.urn, oid, body)

    @router.delete("/silences/{oid}", status_code=204)
    async def delete_silence(oid: str, p=Depends(current_principal)):
        who(p)
        await call(svc.remove, "silence", p.urn, oid)

    @router.get("/slos/{oid}/status")
    async def slo_status(oid: str, p=Depends(current_principal)):
        who(p)
        return await call(svc.slo_status, oid)

    @router.get("/slos/{oid}/history")
    async def slo_history(oid: str, hours: int = Query(24, ge=1, le=24 * 30), p=Depends(current_principal)):
        who(p)
        return await call(svc.slo_history, oid, hours)

    @router.post("/alert-rules/test")
    async def test_rule(body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.test_rule, body)

    # ---- alerts ---------------------------------------------------------------------------------------------
    @router.get("/alerts")
    async def alerts(state: Optional[str] = Query(None, pattern=r"^(open|pending|firing|resolved)$"),
                     limit: int = Query(200, ge=1, le=1000), p=Depends(current_principal)):
        who(p)
        return {"items": await call(svc.alerts, state, limit)}

    @router.post("/alerts/{aid}/acknowledge")
    async def ack(aid: str, p=Depends(current_principal)):
        who(p)
        return await call(svc.acknowledge_alert, p.urn, aid)

    @router.post("/evaluate")
    async def evaluate(p=Depends(current_principal)):
        """Evaluates every rule and SLO now (the notifier does it every CAIRN_OBS_EVALUATE_SECONDS)."""
        who(p)
        return await call(svc.evaluate)

    # ---- incidents ------------------------------------------------------------------------------------------
    @router.get("/incidents")
    async def incidents(status: Optional[str] = Query(None, pattern=r"^(active|open|acknowledged|resolved)$"),
                        limit: int = Query(100, ge=1, le=500), p=Depends(current_principal)):
        who(p)
        return {"items": await call(svc.incidents, status, limit)}

    @router.post("/incidents", status_code=201)
    async def open_incident(body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.open_incident, p.urn, body, agent(p))

    @router.get("/incidents/{iid}")
    async def incident(iid: str, p=Depends(current_principal)):
        who(p)
        return await call(svc.incident, iid)

    @router.get("/incidents/{iid}/context")
    async def context(iid: str, p=Depends(current_principal)):
        who(p)
        return await call(svc.incident_context, iid)

    @router.post("/incidents/{iid}/notes")
    async def note(iid: str, body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.note, p.urn, iid, body, agent(p))

    async def _act(iid: str, action: str, request: Request, p) -> Any:
        try:
            body = await request.json() if await request.body() else {}
        except ValueError:
            raise HTTPException(400, "the body is not JSON")
        return await call(svc.set_incident, p.urn, iid, action, body if isinstance(body, dict) else {}, agent(p))

    @router.post("/incidents/{iid}/acknowledge")
    async def ack_incident(iid: str, request: Request, p=Depends(current_principal)):
        who(p)
        return await _act(iid, "acknowledge", request, p)

    @router.post("/incidents/{iid}/resolve")
    async def resolve_incident(iid: str, request: Request, p=Depends(current_principal)):
        """``{"summary": "..."}``: what happened and what fixed it."""
        who(p)
        return await _act(iid, "resolve", request, p)

    @router.post("/incidents/{iid}/reopen")
    async def reopen_incident(iid: str, request: Request, p=Depends(current_principal)):
        who(p)
        return await _act(iid, "reopen", request, p)

    @router.post("/incidents/{iid}/assign")
    async def assign_incident(iid: str, request: Request, p=Depends(current_principal)):
        """``{"assignee": "urn:li:corpuser:..."}`` (null: nobody)."""
        who(p)
        return await _act(iid, "assign", request, p)

    # ---- deploys --------------------------------------------------------------------------------------------
    @router.get("/deploys")
    async def deploys(limit: int = Query(100, ge=1, le=1000), p=Depends(current_principal)):
        who(p)
        return {"items": await call(svc.deploys, limit)}

    @router.post("/deploys", status_code=201)
    async def record_deploy(body: dict = Body(...), p=Depends(current_principal)):
        who(p)
        return await call(svc.record_deploy, p.urn, body)

    return router

