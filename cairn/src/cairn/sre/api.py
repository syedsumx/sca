"""``/v1/sre``: the SRE agent (``MANAGE_SYSTEM_OPERATIONS``; Diagnose on an instance: whoever may see it).

* ``GET /v1/sre/status``: whether it can work now (model, gateway, pause), and the allowed runbook actions.
* ``GET|PUT /v1/sre/settings`` (kind ``sre_agent_settings``); ``/v1/sre/runbooks`` (kind ``sre_runbooks``).
* ``POST /v1/sre/incidents/<id>/investigate``, ``POST .../ask`` ``{"question"}``; ``GET /v1/sre/incidents/<id>``:
  its runs, proposals and postmortem; ``GET /v1/sre/runs/<id>``.
* ``GET /v1/sre/actions``; ``POST /v1/sre/actions/<id>/approve`` (runs it as you) and ``.../reject``.
* ``GET|PUT /v1/sre/incidents/<id>/postmortem``, ``POST .../postmortem/draft``, ``POST .../postmortem/approve``.
* ``POST /v1/sre/instances/<name>/diagnose`` ``{"question"}``: the Postgres helper's report (and an answer)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.sre.agent import PRIVILEGE, SreAgent, SreError
from cairn.sre.pghelper import DiagnosticError

FORWARD = ("authorization", "cookie", "x-cairn-actor")


def build_router(sre: SreAgent, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/v1/sre", tags=["sre"])

    def who(p) -> Any:
        authorize(p, PRIVILEGE, None)
        return p

    def person(p) -> Any:
        if getattr(p, "via", "") in ("agent", "service", "extension"):
            raise HTTPException(403, "the SRE agent's proposals are decided by people")
        return who(p)

    async def call(fn, *a, **kw):
        try:
            return await run_in_threadpool(fn, *a, **kw)
        except (SreError, DiagnosticError) as e:
            raise HTTPException(e.status, str(e))

    def headers_of(request: Request) -> dict:
        return {k: v for k, v in request.headers.items() if k.lower() in FORWARD}

    @router.get("/status")
    async def status(p=Depends(current_principal)):
        who(p)
        return await call(sre.status)

    @router.get("/settings")
    async def get_settings(p=Depends(current_principal)):
        who(p)
        return await call(sre.settings)

    @router.put("/settings")
    async def put_settings(body: dict = Body(...), p=Depends(current_principal)):
        person(p)
        return await call(sre.put_settings, p.urn, body)

    @router.get("/runbooks")
    async def runbooks(p=Depends(current_principal)):
        who(p)
        return {"items": await call(sre.runbooks)}

    @router.post("/runbooks", status_code=201)
    async def create_runbook(body: dict = Body(...), p=Depends(current_principal)):
        person(p)
        return await call(sre.put_runbook, p.urn, body, True)

    @router.get("/runbooks/{rid}")
    async def get_runbook(rid: str, p=Depends(current_principal)):
        who(p)
        return await call(sre.runbook, rid)

    @router.put("/runbooks/{rid}")
    async def put_runbook(rid: str, body: dict = Body(...), p=Depends(current_principal)):
        person(p)
        return await call(sre.put_runbook, p.urn, body, False, rid)

    @router.delete("/runbooks/{rid}", status_code=204)
    async def delete_runbook(rid: str, p=Depends(current_principal)):
        person(p)
        await call(sre.delete_runbook, p.urn, rid)
        return Response(status_code=204)

    @router.get("/incidents/{iid}")
    async def incident(iid: str, p=Depends(current_principal)):
        who(p)
        return await call(sre.incident_view, iid)

    @router.post("/incidents/{iid}/investigate", status_code=202)
    async def investigate(iid: str, wait: bool = Query(False), p=Depends(current_principal)):
        """The agent investigates the incident, acting for you (in the background unless ``wait``)."""
        person(p)
        return await call(sre.investigate, p, iid, wait)

    @router.post("/incidents/{iid}/ask")
    async def ask(iid: str, body: dict = Body(...), p=Depends(current_principal)):
        person(p)
        return await call(sre.ask, p, iid, str(body.get("question") or ""), True)

    @router.get("/runs/{rid}")
    async def run(rid: str, p=Depends(current_principal)):
        who(p)
        return await call(sre.run, rid)

    @router.get("/actions")
    async def actions(status: Optional[str] = Query(None, pattern=r"^(pending|executed|applied|rejected|failed)$"),
                      incident: Optional[str] = Query(None, max_length=40), p=Depends(current_principal)):
        who(p)
        return {"items": await call(sre.actions, incident, status)}

    @router.post("/actions/{aid}/approve")
    async def approve(aid: str, request: Request, p=Depends(current_principal)):
        """Runs the proposal now, as you: your privileges apply (a change request: approved and applied)."""
        person(p)
        h = headers_of(request)
        return await call(sre.approve, p, h, aid)

    @router.post("/actions/{aid}/reject")
    async def reject(aid: str, request: Request, body: dict = Body(default={}), p=Depends(current_principal)):
        person(p)
        h = headers_of(request)
        return await call(sre.reject, p, h, aid, str((body or {}).get("comment") or ""))

    @router.get("/incidents/{iid}/postmortem")
    async def postmortem(iid: str, p=Depends(current_principal)):
        who(p)
        out = await call(sre.postmortem, iid)
        if out is None:
            raise HTTPException(404, "no postmortem yet")
        return out

    @router.post("/incidents/{iid}/postmortem/draft", status_code=202)
    async def draft(iid: str, p=Depends(current_principal)):
        person(p)
        return await call(sre.draft_postmortem, p, iid, True)

    @router.put("/incidents/{iid}/postmortem")
    async def edit(iid: str, body: dict = Body(...), p=Depends(current_principal)):
        person(p)
        return await call(sre.edit_postmortem, p, iid, str(body.get("markdown") or ""))

    @router.post("/incidents/{iid}/postmortem/approve")
    async def approve_pm(iid: str, p=Depends(current_principal)):
        person(p)
        return await call(sre.approve_postmortem, p, iid)

    @router.post("/instances/{name}/diagnose")
    async def diagnose(name: str, body: dict = Body(default={}), p=Depends(current_principal)):
        """Why is it slow, is it healthy: as the SRE agent for you (what you may see of the instance)."""
        if getattr(p, "via", "") in ("agent", "service", "extension"):
            raise HTTPException(403, "Diagnose is for people")
        return await call(sre.diagnose, p, name, str((body or {}).get("question") or ""))

    return router
