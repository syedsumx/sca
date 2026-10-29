"""FastAPI router for AI features (mounted at ``/ai``)."""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from cairn.ai.llm import LLMError
from cairn.ai.usage import LLMBudgetExceeded
from cairn.common.security import Principal
from cairn.governance.certification import CreateWorkflowRequest, WorkflowError


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class SemanticRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=10, ge=1, le=50)
    entity_types: Optional[list[str]] = None
    exclude: list[str] = Field(default_factory=list, max_length=200)   # e.g. the keyword hits already shown
    min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class DocRequest(BaseModel):
    dataset_urn: str


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: Optional[str] = None


class UrnRequest(BaseModel):
    urn: str = Field(pattern=r"^urn:li:")


class RebuildRequest(BaseModel):
    entity_types: Optional[list[str]] = None
    urns: Optional[list[str]] = None


def _llm_status(e: LLMError) -> int:
    return 429 if isinstance(e, LLMBudgetExceeded) else 502


def build_router(services, current_principal) -> APIRouter:
    router = APIRouter(prefix="/ai", tags=["ai"])

    @router.post("/ask")
    def ask(req: AskRequest, p: Principal = Depends(current_principal)):
        if services.nlq is None:
            raise HTTPException(503, "AI catalog access not configured")
        services.ai_actor(p)
        return services.nlq.ask(req.question, services.can_view(p))

    @router.post("/search/semantic")
    def semantic(req: SemanticRequest, p: Principal = Depends(current_principal)):
        if services.semantic is None:
            raise HTTPException(503, "semantic search not configured")
        return {"results": services.semantic.search(req.query, req.limit, req.entity_types, services.can_view(p),
                                                    req.exclude, req.min_score)}

    @router.get("/similar")
    def similar(urn: str = Query(pattern=r"^urn:li:"), limit: int = Query(5, ge=1, le=20),
                p: Principal = Depends(current_principal)):
        if services.semantic is None:
            raise HTTPException(503, "semantic search not configured")
        services.authorize(p, "VIEW_ENTITY_PAGE", urn)
        return {"urn": urn, "results": services.semantic.similar(urn, limit, None, services.can_view(p))}

    @router.get("/index/status")
    def index_status(p: Principal = Depends(current_principal)):
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        if services.semantic is None:
            raise HTTPException(503, "semantic search not configured")
        return services.semantic.status()

    @router.post("/docs/generate", status_code=201)
    def generate_docs(req: DocRequest, p: Principal = Depends(current_principal)):
        if services.docgen is None or services.docgen.llm is None:
            raise HTTPException(503, "documentation generation not configured (no LLM provider)")
        services.authorize(p, "EDIT_ENTITY_DOCS", req.dataset_urn)
        services.ai_actor(p)
        try:
            draft = services.docgen.draft(req.dataset_urn)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except LLMError as e:
            raise HTTPException(_llm_status(e), f"LLM failure: {e}")
        try:
            wf = services.workflows.create(CreateWorkflowRequest(
                kind="DOCUMENTATION", entity_urn=req.dataset_urn, payload={"draft": draft}), p.urn)
        except WorkflowError as e:
            raise HTTPException(e.status, str(e))
        return {"workflow_id": wf.id, "draft": draft}

    @router.post("/chat")
    def chat(req: ChatRequest, p: Principal = Depends(current_principal)):
        if getattr(services, "assistant", None) is None:
            raise HTTPException(503, "assistant not configured")
        try:
            return services.assistant.chat(p, req.message, req.conversation_id)
        except LLMError as e:
            raise HTTPException(_llm_status(e), f"LLM failure: {e}")

    @router.post("/explain/lineage")
    def explain(req: UrnRequest, p: Principal = Depends(current_principal)):
        services.authorize(p, "VIEW_ENTITY_PAGE", req.urn)
        return services.explainer_for(p).explain(req.urn)

    @router.post("/docs/markdown")
    def markdown(req: UrnRequest, p: Principal = Depends(current_principal)):
        from cairn.ai.readme import assemble_markdown

        services.authorize(p, "VIEW_ENTITY_PAGE", req.urn)
        if services.catalog is None:
            raise HTTPException(503, "catalog access not configured")
        draft, workflow_id = None, None
        if services.docgen is not None and services.docgen.llm is not None:
            services.authorize(p, "EDIT_ENTITY_DOCS", req.urn)
            try:
                services.ai_actor(p)
                draft = services.docgen.draft(req.urn)
                wf = services.workflows.create(CreateWorkflowRequest(
                    kind="DOCUMENTATION", entity_urn=req.urn, payload={"draft": draft}), p.urn)
                workflow_id = wf.id
            except (LLMError, WorkflowError, LookupError):
                draft = None
        md = assemble_markdown(req.urn, services.catalog, services.quality.store, services.attributes, draft)
        return {"markdown": md, "workflow_id": workflow_id, "ai_drafted": draft is not None}

    @router.post("/index/rebuild")
    def rebuild(req: RebuildRequest, p: Principal = Depends(current_principal)):
        services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        if services.semantic is None:
            raise HTTPException(503, "semantic search not configured")
        if req.urns:
            return services.semantic.index_many(req.urns)
        return services.semantic.rebuild(req.entity_types)

    @router.post("/feedback", status_code=201)
    def feedback(body: dict, p: Principal = Depends(current_principal)):
        """A thumbs up or down on an assistant answer (kind chat) or an ask-your-data result (kind ask)."""
        from cairn.ai.traces import FeedbackIn

        if services.traces is None:
            raise HTTPException(503, "feedback is not configured")
        try:
            fb = FeedbackIn.model_validate(body)
        except ValueError as e:
            raise HTTPException(422, str(e))
        return services.traces.feedback(p.urn, fb)

    @router.get("/feedback")
    def list_feedback(rating: Optional[str] = Query(None, pattern="^(up|down)$"),
                      kind: Optional[str] = Query(None, pattern="^(chat|ask)$"),
                      p: Principal = Depends(current_principal)):
        services.authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return {"summary": services.traces.feedback_summary(), "items": services.traces.feedback_list(rating, kind)}

    @router.get("/feedback/eval-cases")
    def feedback_eval_cases(p: Principal = Depends(current_principal)):
        """Down-voted questions as ``cairn ai eval`` cases (YAML) to complete and add to the eval suite."""
        from fastapi.responses import PlainTextResponse

        services.authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return PlainTextResponse(services.traces.eval_cases(), media_type="application/yaml")

    @router.get("/usage")
    def usage(group_by: str = "task", days: int = Query(30, ge=1, le=366), mine: bool = False,
              p: Principal = Depends(current_principal)):
        """Model calls, tokens and estimated cost. Everyone may see their own; all usage needs
        MANAGE_SYSTEM_OPERATIONS."""
        if services.ai_usage is None:
            raise HTTPException(503, "AI usage metering not configured")
        if not mine:
            services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None)
        since = int(time.time() * 1000) - days * 86_400_000
        try:
            return services.ai_usage.summary(since, group_by=group_by, actor=p.urn if mine else None)
        except ValueError as e:
            raise HTTPException(400, str(e))

    return router
