"""FastAPI router for governance (mounted at ``/governance``)."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError

from cairn.common.audit import AuditLog
from cairn.common.security import Principal
from cairn.governance.abac import PolicyBundle
from cairn.governance.certification import (
    CreateWorkflowRequest,
    TransitionRequest,
    Workflow,
    WorkflowError,
    WorkflowService,
)
from cairn.governance.pdp import (
    AuthorizeRequest,
    Decision,
    PolicyDecisionPoint,
    PolicyRepository,
    ResourceRef,
)
from cairn.governance.pii import GraphLineage, classify_schema, plan_propagation, pii_origins_from_aspect


class SimulateRequest(BaseModel):
    actor: str
    privilege: str
    resource_urn: Optional[str] = None
    subject_attributes: dict[str, Any] = Field(default_factory=dict)
    resource_attributes: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)


class ClassifyRequest(BaseModel):
    fields: list[dict[str, Any]] = Field(description="[{fieldPath, nativeDataType?}]")
    samples: dict[str, list[str]] = Field(default_factory=dict)
    auto_apply_threshold: float = 0.9
    suggest_threshold: float = 0.6


class PropagationPlanRequest(BaseModel):
    dataset_urn: str
    max_hops: int = 5


def _resource_ref(urn: Optional[str]) -> Optional[ResourceRef]:
    if not urn:
        return None
    return ResourceRef(type=urn.split(":", 3)[2] if urn.startswith("urn:li:") else "unknown", urn=urn)


def build_router(
    pdp: PolicyDecisionPoint,
    repo: Optional[PolicyRepository],
    workflows: Optional[WorkflowService],
    audit: Optional[AuditLog],
    graph,
    current_principal,
    authorize,
) -> APIRouter:
    router = APIRouter(prefix="/governance", tags=["governance"])

    # ---- PDP -----------------------------------------------------------------------
    @router.post("/v1/authorize", response_model=Decision, response_model_exclude_none=True)
    def authorize_endpoint(req: AuthorizeRequest, explain: bool = False, p: Principal = Depends(current_principal)):
        # Only the Cairn Core plugin (service principal) may ask about arbitrary actors and
        # inject subject attributes; users may only ask about themselves.
        if not p.is_service:
            if req.actor != p.urn:
                raise HTTPException(403, "users may only query decisions for themselves")
            req = req.model_copy(update={"subject_attributes": {}})
        return pdp.decide(req, explain=explain)

    @router.post("/simulate", response_model=Decision)
    def simulate(req: SimulateRequest, p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        return pdp.decide(AuthorizeRequest(
            actor=req.actor, privilege=req.privilege, resource=_resource_ref(req.resource_urn),
            subject_attributes=req.subject_attributes, resource_attributes=req.resource_attributes,
            environment=req.environment,
        ), explain=True)

    @router.get("/policies")
    def get_policies(include_compiled: bool = False, p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        body: dict[str, Any] = {"policies": [x.model_dump(mode="json") for x in pdp.bundle.policies]}
        if include_compiled:
            body["compiled"] = [x.model_dump(mode="json") for x in pdp.policies()]
        return body

    @router.post("/policies/validate")
    def validate_policies(body: dict[str, Any], p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        try:
            PolicyBundle.model_validate(body)
            return {"valid": True, "errors": []}
        except ValidationError as e:
            return {"valid": False, "errors": [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}"
                                               for err in e.errors()]}

    @router.put("/policies")
    def put_policies(bundle: PolicyBundle, p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        version = repo.save(bundle, p.urn) if repo else None
        pdp.set_bundle(bundle)
        return {"version": version, "policies": len(bundle.policies)}

    # ---- PII -----------------------------------------------------------------------
    @router.post("/pii/classify")
    def classify(req: ClassifyRequest, p: Principal = Depends(current_principal)):
        results = classify_schema(req.fields, req.samples, req.auto_apply_threshold, req.suggest_threshold)
        return {"classifications": [r.as_dict() for r in results]}

    @router.post("/pii/propagation/plan")
    def propagation_plan(req: PropagationPlanRequest, p: Principal = Depends(current_principal)):
        authorize(p, "VIEW_ENTITY_PAGE", req.dataset_urn)
        if graph is None:
            raise HTTPException(503, "Cairn Core not configured")
        origins = []
        for aspect, key in (("editableSchemaMetadata", "editableSchemaFieldInfo"), ("schemaMetadata", "fields")):
            a = graph.get_aspect(req.dataset_urn, aspect)
            if a:
                origins += pii_origins_from_aspect(req.dataset_urn, a, key)
        plan = plan_propagation(origins, GraphLineage(graph), req.max_hops)
        return {"origins": [{"field": f, "term": t} for f, t in origins], "plan": [s.as_dict() for s in plan]}

    # ---- workflows -----------------------------------------------------------------
    def _wf() -> WorkflowService:
        if workflows is None:
            raise HTTPException(503, "workflows not configured")
        return workflows

    @router.get("/workflows", response_model=list[Workflow])
    def list_workflows(state: Optional[str] = None, kind: Optional[str] = None, entity_urn: Optional[str] = None,
                       limit: int = Query(200, le=1000), p: Principal = Depends(current_principal)):
        items = _wf().list(state, kind, entity_urn, limit)
        return [w for w in items if authorize(p, "VIEW_ENTITY_PAGE", w.entity_urn, raise_on_deny=False)]

    @router.get("/workflows/{workflow_id}", response_model=Workflow)
    def get_workflow(workflow_id: str, p: Principal = Depends(current_principal)):
        try:
            wf = _wf().get(workflow_id)
        except WorkflowError as e:
            raise HTTPException(e.status, str(e))
        authorize(p, "VIEW_ENTITY_PAGE", wf.entity_urn)
        return wf

    @router.post("/workflows", response_model=Workflow, status_code=201)
    def create_workflow(req: CreateWorkflowRequest, p: Principal = Depends(current_principal)):
        authorize(p, "VIEW_ENTITY_PAGE", req.entity_urn)
        try:
            return _wf().create(req, p.urn)
        except WorkflowError as e:
            raise HTTPException(e.status, str(e))

    @router.post("/workflows/{workflow_id}/{action}", response_model=Workflow)
    def transition(workflow_id: str, action: str, body: Optional[TransitionRequest] = None,
                   p: Principal = Depends(current_principal)):
        try:
            return _wf().act(workflow_id, action, p.urn, body.comment if body else None)
        except WorkflowError as e:
            raise HTTPException(e.status, str(e))

    # ---- audit ---------------------------------------------------------------------
    @router.get("/audit")
    def audit_entries(category: Optional[str] = None, limit: int = Query(100, le=1000),
                      p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        return {"entries": audit.entries(category, limit) if audit else []}

    @router.get("/audit/verify")
    def audit_verify(p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_POLICIES", None)
        return audit.verify_chain() if audit else {"valid": True, "entries_checked": 0}

    return router
