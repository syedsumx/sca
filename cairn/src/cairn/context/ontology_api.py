"""``/v1/ontology``: Cairn Ontology (the ontology compiler) through Cairn.

Reads follow catalog visibility (an element is shown only when the caller may see every dataset it is about);
syncing, compiling, estimating, cancelling, resuming, importing and run diffs need ``MANAGE_GLOSSARIES``; deciding a
proposal is a person's act and needs ``MANAGE_GLOSSARIES``, and accepting one ``EDIT_ENTITY_GLOSSARY_TERMS`` on each
of its datasets too (as in the context inbox); creating the compiler's agent identity needs
``MANAGE_GLOBAL_SETTINGS``. The explorer's reads (``/explorer``, ``/terms/{id}``, ``/asset``, ``/search-terms``,
``/catalog``) are in :mod:`cairn.context.ontology_explorer`.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel, Field

from cairn.common.security import Principal
from cairn.context.ontology_compiler import COMPILE_PRIVILEGE, OntologyCompiler, OntologyError
from cairn.context.ontology_explorer import MAX_BATCH, OntologyExplorer, ReviewError


class ScopeIn(BaseModel):
    """A run limited to these tables (dataset urns) and the tables of these schemas (``schema`` or
    ``catalog.schema``); none: every table."""
    tables: Optional[list[str]] = Field(default=None, max_length=100_000)
    schemas: Optional[list[str]] = Field(default=None, max_length=1000)


class CompileIn(ScopeIn):
    mode: str = Field(default="incremental", pattern="^(full|incremental)$")
    sync: bool = True
    wait: bool = False


class EstimateIn(ScopeIn):
    mode: str = Field(default="incremental", pattern="^(full|incremental)$")


class TermEdits(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    definition: Optional[str] = Field(default=None, min_length=1, max_length=5000)
    synonyms: Optional[list[str]] = Field(default=None, max_length=50)


class DecisionIn(BaseModel):
    comment: str = Field(default="", max_length=2000)
    edits: Optional[TermEdits] = None


class BatchIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    action: str = Field(pattern="^(accept|reject)$")
    comment: str = Field(default="", max_length=2000)


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

    explorer = OntologyExplorer(onto, onto.graph, context, onto.audit) if onto is not None else None

    def ex() -> OntologyExplorer:
        svc()
        return explorer  # type: ignore[return-value]

    def call(fn):
        try:
            return fn()
        except OntologyError as e:
            raise HTTPException(e.status, str(e))
        except ReviewError as e:
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
        """A run; ``tables``/``schemas`` limit it to those tables (each one the caller may see), the others keeping
        what the published run made of them."""
        manager(p)
        scope = call(lambda: ex().scope(can_view(p), body.tables, body.schemas))
        return call(lambda: svc().compile(p.urn, body.mode, body.sync, body.wait, tables=scope))

    @router.post("/estimate")
    def estimate(body: EstimateIn = Body(default_factory=EstimateIn), p: Principal = Depends(current_principal)):
        """What a run would compile (the rest reused) and an upper bound of its model calls, before it starts."""
        manager(p)
        scope = call(lambda: ex().scope(can_view(p), body.tables, body.schemas))
        out = call(lambda: svc().client.estimate(body.mode, scope))
        out["scope"] = len(scope) if scope else None
        out.pop("unknown_tables", None)
        return out

    @router.get("/catalog")
    def catalog(p: Principal = Depends(current_principal)):
        """The compiler's tables the caller may see, and their schemas (the compile wizard's pickers)."""
        return call(lambda: ex().catalog(can_view(p)))

    @router.get("/runs/{rid}/diff")
    def run_diff(rid: str, against: Optional[str] = Query(None, max_length=64), p: Principal = Depends(current_principal)):
        """What changed in a completed run since the completed run before it (or ``against``), over what the
        caller may see."""
        manager(p)
        return call(lambda: ex().diff(can_view(p), rid, against))

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

    @router.get("/explorer")
    def explorer_view(p: Principal = Depends(current_principal)):
        """The ontology shaped for the explorer: terms with their domain, schema, review status and confidence;
        relationships between them; bridge tables; metrics; tables."""
        return call(lambda: ex().explorer(can_view(p)))

    @router.get("/terms/{tid}")
    def term_detail(tid: str, p: Principal = Depends(current_principal)):
        """One term: definition, synonyms, domain, owners, mapped columns, relationships with their evidence,
        metrics, provenance (run and inputs), date formats, and its open proposals."""
        return call(lambda: ex().term(can_view(p), tid))

    @router.get("/asset")
    def asset_terms(urn: str = Query(..., min_length=1, max_length=2000), p: Principal = Depends(current_principal)):
        """The business terms of one dataset, per table and per column (404 for a dataset the caller may not see)."""
        return call(lambda: ex().asset(can_view(p), urn))

    @router.get("/search-terms")
    def search_terms(q: str = Query(..., min_length=1, max_length=200), limit: int = Query(10, ge=1, le=50),
                     p: Principal = Depends(current_principal)):
        """Terms by name or synonym, for search results."""
        return {"terms": call(lambda: ex().search_terms(can_view(p), q, limit))}

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
    def graph(format: str = Query("json", pattern="^(json|jsonld|turtle|csv)$"),  # noqa: A002
              p: Principal = Depends(current_principal)):
        if format == "csv":
            return PlainTextResponse(call(lambda: ex().csv(can_view(p))), media_type="text/csv",
                                     headers={"Content-Disposition": 'attachment; filename="ontology.csv"'})
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

    @router.post("/proposals/batch")
    def decide_batch(body: BatchIn, p: Principal = Depends(current_principal)):
        """Accepts or rejects many proposals with one comment, each as if decided alone (and audited); the
        outcome of each, so one refusal does not stop the others."""
        if context is None:
            raise HTTPException(503, "the context inbox is not configured")
        return call(lambda: ex().decide_many(p, authorize, can_view(p), body.ids, body.action, body.comment))

    @router.post("/proposals/{fid}/{action}")
    def decide(fid: str, action: str, body: DecisionIn = Body(default_factory=DecisionIn),
               p: Principal = Depends(current_principal)):
        """``accept``: writes the glossary term and its column links, or records the approved join or metric (a
        term's name, definition and synonyms can be edited as it is accepted); ``reject``: dismisses it (a later
        run does not bring it back). The comment is kept with the proposal and in the audit log."""
        if context is None:
            raise HTTPException(503, "the context inbox is not configured")
        if action not in ("accept", "reject"):
            raise HTTPException(404, "unknown action")
        edits = body.edits.model_dump() if body.edits else None
        return call(lambda: ex().decide(p, authorize, can_view(p), fid, action, body.comment, edits))

    @router.post("/agent")
    def create_agent(body: AgentIn, p: Principal = Depends(current_principal)):
        """The compiler's agent identity (scope ``model``, acting for ``owner``); its key is shown once."""
        if getattr(p, "via", "") == "agent":
            raise HTTPException(403, "agents cannot make agents")
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return call(lambda: svc().create_agent(body.owner, p.urn))

    return router
