"""FastAPI router for AI features (mounted at ``/ai``)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from datahub_enterprise.ai.llm import LLMError
from datahub_enterprise.common.security import Principal
from datahub_enterprise.governance.certification import CreateWorkflowRequest, WorkflowError


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class SemanticRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=50)
    entity_types: Optional[list[str]] = None


class DocRequest(BaseModel):
    dataset_urn: str


class RebuildRequest(BaseModel):
    entity_types: Optional[list[str]] = None
    urns: Optional[list[str]] = None


def build_router(services, current_principal) -> APIRouter:
    router = APIRouter(prefix="/ai", tags=["ai"])

    @router.post("/ask")
    def ask(req: AskRequest, p: Principal = Depends(current_principal)):
        if services.nlq is None:
            raise HTTPException(503, "AI catalog access not configured")
        services.nlq.on_llm_call = services.record_llm_call(p)
        return services.nlq.ask(req.question, services.can_view(p))

    @router.post("/search/semantic")
    def semantic(req: SemanticRequest, p: Principal = Depends(current_principal)):
        if services.semantic is None:
            raise HTTPException(503, "semantic search not configured")
        return {"results": services.semantic.search(req.query, req.limit, req.entity_types, services.can_view(p))}

    @router.post("/docs/generate", status_code=201)
    def generate_docs(req: DocRequest, p: Principal = Depends(current_principal)):
        if services.docgen is None:
            raise HTTPException(503, "documentation generation not configured")
        services.authorize(p, "EDIT_ENTITY_DOCS", req.dataset_urn)
        services.docgen.on_llm_call = services.record_llm_call(p)
        try:
            draft = services.docgen.draft(req.dataset_urn)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except LLMError as e:
            raise HTTPException(502, f"LLM failure: {e}")
        try:
            wf = services.workflows.create(CreateWorkflowRequest(
                kind="DOCUMENTATION", entity_urn=req.dataset_urn, payload={"draft": draft}), p.urn)
        except WorkflowError as e:
            raise HTTPException(e.status, str(e))
        return {"workflow_id": wf.id, "draft": draft}

    @router.post("/index/rebuild")
    def rebuild(req: RebuildRequest, p: Principal = Depends(current_principal)):
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        if services.semantic is None:
            raise HTTPException(503, "semantic search not configured")
        if req.urns:
            return services.semantic.index_many(req.urns)
        return services.semantic.rebuild(req.entity_types)

    return router
