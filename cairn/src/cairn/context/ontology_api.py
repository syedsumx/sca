"""``/v1/ontology``: Cairn Ontology (the ontology compiler) through Cairn.

Reads follow catalog visibility (an element is shown only when the caller may see every dataset it is about);
syncing, compiling, cancelling, resuming and importing need ``MANAGE_GLOSSARIES``; accepting a proposal needs
``EDIT_ENTITY_GLOSSARY_TERMS`` on each of its datasets (as in the context inbox); creating the compiler's agent
identity needs ``MANAGE_GLOBAL_SETTINGS``.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from cairn.common.security import Principal
from cairn.context.ontology_compiler import COMPILE_PRIVILEGE, OntologyCompiler, OntologyError


class CompileIn(BaseModel):
    mode: str = Field(default="incremental", pattern="^(full|incremental)$")
    sync: bool = True
    wait: bool = False


class ValidateIn(BaseModel):
    sql: str = Field(min_length=1, max_length=200_000)
    dialect: Optional[str] = Field(default=None, max_length=40)


class AgentIn(BaseModel):
    owner: str = Field(pattern=r"^urn:li:corpuser:")


def build_router(onto: Optional[OntologyCompiler], current_principal, authorize, can_view, context=None) -> APIRouter:
    router = APIRouter(prefix="/v1/ontology", tags=["ontology"])

    def svc() -> OntologyCompiler:
        if onto is None:
            raise HTTPException(503, "Cairn Ontology is not configured (CAIRN_ONTOLOGY_URL)")
        return onto

    def call(fn):
        try:
            return fn()
        except OntologyError as e:
            raise HTTPException(e.status, str(e))

    def manager(p: Principal) -> None:
        if getattr(p, "via", "") == "agent":
            raise HTTPException(403, "compiling the ontology is for people, not agents")
        authorize(p, COMPILE_PRIVILEGE, None)

    @router.get("/status")
    def status(p: Principal = Depends(current_principal)):
        if onto is None:
            return {"configured": False}
        return call(onto.status)

    @router.post("/sync")
    def sync(p: Principal = Depends(current_principal)):
        manager(p)
        return call(lambda: svc().sync(p.urn))

    @router.post("/runs")
    def compile_now(body: CompileIn = Body(default_factory=CompileIn), p: Principal = Depends(current_principal)):
        manager(p)
        return call(lambda: svc().compile(p.urn, body.mode, body.sync, body.wait))

    @router.get("/runs")
    def runs(p: Principal = Depends(current_principal)):
        manager(p)          # a run's statistics count every table, seen or not
        return call(lambda: svc().client.runs())

    @router.get("/runs/{rid}")
    def run(rid: str, p: Principal = Depends(current_principal)):
        manager(p)
        return call(lambda: svc().client.run(rid))

    @router.post("/runs/{rid}/cancel")
    def cancel(rid: str, p: Principal = Depends(current_principal)):
        manager(p)
        return call(lambda: svc().client.cancel(rid))

    @router.post("/runs/{rid}/resume")
    def resume(rid: str, p: Principal = Depends(current_principal)):
        manager(p)
        return call(lambda: svc().client.resume(rid))

    @router.post("/runs/{rid}/import")
    def import_run(rid: str, p: Principal = Depends(current_principal)):
        """A finished run's ontology as proposals in the context inbox (``published`` for the published run)."""
        manager(p)
        return call(lambda: svc().import_run(None if rid == "published" else rid, p.urn))

    @router.get("")
    def ontology(p: Principal = Depends(current_principal)):
        return call(lambda: svc().view(can_view(p)))

    @router.get("/terms")
    def terms(q: str = Query("", max_length=200), p: Principal = Depends(current_principal)):
        return {"terms": call(lambda: svc().terms(can_view(p), q))}

    @router.get("/relationships")
    def relationships(datasets: Optional[list[str]] = Query(None), between: bool = False,
                      p: Principal = Depends(current_principal)):
        return {"relationships": call(lambda: svc().relationships(can_view(p), datasets, between))}

    @router.get("/metrics")
    def metrics(p: Principal = Depends(current_principal)):
        v = call(lambda: svc().view(can_view(p)))
        return {"metrics": [{k: m[k] for k in ("id", "name", "description", "expression", "sql", "source", "tables",
                                               "term_name", "usage")} for m in v["metrics"]]}

    @router.get("/bridges")
    def bridges(p: Principal = Depends(current_principal)):
        return {"bridges": call(lambda: svc().view(can_view(p)))["bridges"]}

    @router.get("/search")
    def search(q: str = Query(..., min_length=1, max_length=500), mode: str = Query("hybrid", pattern="^(name|semantic|hybrid)$"),
               limit: int = Query(10, ge=1, le=50), p: Principal = Depends(current_principal)):
        return {"hits": call(lambda: svc().search(can_view(p), q, mode, limit))}

    @router.get("/graph")
    def graph(format: str = Query("json", pattern="^(json|jsonld|turtle)$"),  # noqa: A002
              p: Principal = Depends(current_principal)):
        out = call(lambda: svc().graph_export(can_view(p), format))
        if format == "turtle":
            return PlainTextResponse(out, media_type="text/turtle")
        if format == "jsonld":
            return Response(out, media_type="application/ld+json")
        return out

    @router.get("/provenance/{kind}/{eid}")
    def provenance(kind: str, eid: str, p: Principal = Depends(current_principal)):
        return call(lambda: svc().provenance(can_view(p), kind, eid))

    @router.post("/validate-sql")
    def validate_sql(body: ValidateIn, p: Principal = Depends(current_principal)):
        return call(lambda: svc().validate_sql(body.sql, can_view(p), body.dialect))

    @router.get("/proposals")
    def proposals(status: Optional[str] = Query("OPEN", pattern="^(OPEN|ACCEPTED|DISMISSED)$"),
                  kind: Optional[str] = Query(None, pattern="^(term|relationship|metric)$"),
                  p: Principal = Depends(current_principal)):
        return {"proposals": svc().proposals(can_view(p), status, kind)}

    @router.post("/proposals/{fid}/{action}")
    def decide(fid: str, action: str, p: Principal = Depends(current_principal)):
        """``accept``: writes the glossary term and its column links, or records the approved join or metric;
        ``reject``: dismisses it (a later run does not bring it back)."""
        if context is None:
            raise HTTPException(503, "the context inbox is not configured")
        try:
            f = context.get(fid)
        except KeyError:
            raise HTTPException(404, "proposal not found")
        if not all(can_view(p)(e) for e in f["entities"] if e.startswith("urn:li:")):
            raise HTTPException(404, "proposal not found")
        if action == "reject":
            manager(p)
            return context.set_status(fid, "DISMISSED", p.urn)
        if action != "accept":
            raise HTTPException(404, "unknown action")
        for e in f["entities"]:
            if e.startswith("urn:li:dataset:"):
                authorize(p, "EDIT_ENTITY_GLOSSARY_TERMS", e)
        try:
            return context.accept(fid, p.urn)
        except ValueError as e:
            raise HTTPException(409, str(e))

    @router.post("/agent")
    def create_agent(body: AgentIn, p: Principal = Depends(current_principal)):
        """The compiler's agent identity (scope ``model``, acting for ``owner``); its key is shown once."""
        if getattr(p, "via", "") == "agent":
            raise HTTPException(403, "agents cannot make agents")
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return call(lambda: svc().create_agent(body.owner, p.urn))

    return router
