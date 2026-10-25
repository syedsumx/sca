"""Certification and approval workflows (also used for AI documentation proposals).

State machine::

    DRAFT → SUBMITTED → IN_REVIEW → APPROVED → CERTIFIED → EXPIRED | REVOKED
                 ↑           ↓   ↘ REJECTED
                 └─ CHANGES_REQUESTED

Rules: quorum of distinct eligible reviewers; separation of duties (requester cannot
approve); any eligible reviewer can veto (REJECTED); entry checks gate APPROVED →
CERTIFIED. Every transition is appended to the workflow history and the audit chain.
"""

from __future__ import annotations

import glob
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal, Optional

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, Table, select, update
from sqlalchemy.engine import Engine

from datahub_enterprise.common.audit import AuditLog
from datahub_enterprise.common.db import JSONText, metadata
from datahub_enterprise.common.gms import MetadataGraph
from datahub_enterprise.common.proposals import DHE_ACTOR, audit_stamp, make_mcp, now_ms

logger = logging.getLogger(__name__)

State = Literal["DRAFT", "SUBMITTED", "IN_REVIEW", "CHANGES_REQUESTED", "APPROVED", "REJECTED",
                "CERTIFIED", "EXPIRED", "REVOKED"]
Kind = Literal["CERTIFICATION", "DOCUMENTATION"]

CERT_STATUS_PROP = "urn:li:structuredProperty:io.dhe.certification.status"
CERT_BY_PROP = "urn:li:structuredProperty:io.dhe.certification.certifiedBy"
CERT_UNTIL_PROP = "urn:li:structuredProperty:io.dhe.certification.validUntil"
CERTIFIED_TAG = "urn:li:tag:Certified"

workflows = Table(
    "dhe_workflows",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("kind", String(32), nullable=False, index=True),
    Column("entity_urn", String(1024), nullable=False, index=True),
    Column("state", String(32), nullable=False, index=True),
    Column("requester", String(512), nullable=False),
    Column("template", String(128), nullable=False),
    Column("doc", JSONText, nullable=False),
    Column("created_at", String(40), nullable=False),
    Column("updated_at", String(40), nullable=False),
    Column("valid_until", String(40), nullable=True),
    Column("version", Integer, nullable=False, default=1),
)


class Template(BaseModel):
    name: str
    kind: Kind = "CERTIFICATION"
    reviewer_groups: list[str] = Field(default_factory=list, description="group urns, or 'owners'")
    quorum: int = Field(default=1, ge=1)
    validity_days: Optional[int] = 180
    sla_hours: Optional[int] = 72
    checks: list[str] = Field(default_factory=lambda: ["has_owner", "has_description"])
    auto_revoke_on_critical_dq: bool = True


DEFAULT_TEMPLATES = {
    "standard-certification": Template(
        name="standard-certification", reviewer_groups=["owners", "urn:li:corpGroup:data-stewards"], quorum=1,
        checks=["has_owner", "has_description", "documented_columns:0.8", "no_open_incidents"]),
    "ai-documentation": Template(
        name="ai-documentation", kind="DOCUMENTATION", reviewer_groups=["owners", "urn:li:corpGroup:data-stewards"],
        quorum=1, validity_days=None, checks=[]),
}


class Vote(BaseModel):
    actor: str
    at: str
    comment: Optional[str] = None


class HistoryEntry(BaseModel):
    at: str
    actor: str
    from_state: Optional[str]
    to_state: str
    comment: Optional[str] = None


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""


class Workflow(BaseModel):
    id: str
    kind: Kind
    entity_urn: str
    state: State
    requester: str
    template: str
    quorum: int
    approvals: list[Vote] = Field(default_factory=list)
    rejections: list[Vote] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    checks: list[CheckResult] = Field(default_factory=list)
    history: list[HistoryEntry] = Field(default_factory=list)
    created_at: str
    updated_at: str
    valid_until: Optional[str] = None


class CreateWorkflowRequest(BaseModel):
    kind: Kind = "CERTIFICATION"
    entity_urn: str
    template: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    submit: bool = True


class TransitionRequest(BaseModel):
    comment: Optional[str] = Field(default=None, max_length=4000)


class WorkflowError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_templates(directory: Optional[str]) -> dict[str, Template]:
    templates = dict(DEFAULT_TEMPLATES)
    if directory and os.path.isdir(directory):
        for path in sorted(glob.glob(os.path.join(directory, "*.y*ml"))):
            with open(path) as fh:
                doc = yaml.safe_load(fh) or {}
            for name, spec in (doc.get("templates") or {}).items():
                templates[name] = Template(name=name, **spec)
    return templates


# --------------------------------------------------------------------------- checks
class EntryChecks:
    """Named checks evaluated against live metadata. ``name[:arg]``, e.g. ``documented_columns:0.8``."""

    def __init__(self, graph: Optional[MetadataGraph], open_incidents: Optional[Callable[[str], int]] = None):
        self.graph = graph
        self.open_incidents = open_incidents or (lambda urn: 0)

    def _aspect(self, urn: str, name: str) -> dict:
        if self.graph is None:
            return {}
        try:
            return self.graph.get_aspect(urn, name) or {}
        except Exception:
            return {}

    def run(self, urn: str, names: list[str]) -> list[CheckResult]:
        out = []
        for spec in names:
            name, _, arg = spec.partition(":")
            fn = getattr(self, f"check_{name}", None)
            if fn is None:
                out.append(CheckResult(name=spec, passed=False, detail="unknown check"))
                continue
            out.append(fn(urn, arg) if arg else fn(urn))
        return out

    def check_has_owner(self, urn: str) -> CheckResult:
        owners = self._aspect(urn, "ownership").get("owners", [])
        return CheckResult(name="has_owner", passed=bool(owners), detail=f"{len(owners)} owner(s)")

    def check_has_description(self, urn: str) -> CheckResult:
        desc = (self._aspect(urn, "editableDatasetProperties").get("description")
                or self._aspect(urn, "datasetProperties").get("description") or "")
        return CheckResult(name="has_description", passed=len(desc.strip()) >= 20,
                           detail=f"{len(desc.strip())} chars")

    def check_documented_columns(self, urn: str, min_ratio: str = "0.8") -> CheckResult:
        fields = [f["fieldPath"] for f in self._aspect(urn, "schemaMetadata").get("fields", [])]
        documented = {f["fieldPath"] for f in self._aspect(urn, "schemaMetadata").get("fields", []) if f.get("description")}
        documented |= {f["fieldPath"] for f in self._aspect(urn, "editableSchemaMetadata").get("editableSchemaFieldInfo", [])
                       if f.get("description")}
        ratio = len(documented & set(fields)) / len(fields) if fields else 1.0
        return CheckResult(name=f"documented_columns:{min_ratio}", passed=ratio >= float(min_ratio),
                           detail=f"{ratio:.0%} of {len(fields)} columns documented")

    def check_no_open_incidents(self, urn: str) -> CheckResult:
        n = self.open_incidents(urn)
        return CheckResult(name="no_open_incidents", passed=n == 0, detail=f"{n} open DQ incident(s)")


# --------------------------------------------------------------------------- service
class WorkflowService:
    def __init__(
        self,
        engine: Engine,
        graph: Optional[MetadataGraph],
        templates: dict[str, Template],
        checks: EntryChecks,
        is_eligible_reviewer: Callable[[str, Workflow, Template], bool],
        audit: Optional[AuditLog] = None,
        on_documentation_approved: Optional[Callable[[Workflow, str], None]] = None,
    ):
        self.engine = engine
        self.graph = graph
        self.templates = templates
        self.checks = checks
        self.is_eligible_reviewer = is_eligible_reviewer
        self.audit = audit
        self.on_documentation_approved = on_documentation_approved
        # Called after a state change is persisted: (workflow, history_entry).
        self.listeners: list[Callable[[Workflow, HistoryEntry], None]] = []

    def _fire(self, wf: Workflow, entries: list[HistoryEntry]) -> None:
        for entry in entries:
            for listener in self.listeners:
                try:
                    listener(wf, entry)
                except Exception:
                    logger.exception("workflow listener failed")

    # ---- persistence -------------------------------------------------------------
    def _row_to_wf(self, row: Any) -> Workflow:
        m = row._mapping
        return Workflow(id=m["id"], kind=m["kind"], entity_urn=m["entity_urn"], state=m["state"],
                        requester=m["requester"], template=m["template"], created_at=m["created_at"],
                        updated_at=m["updated_at"], valid_until=m["valid_until"], **m["doc"])

    def get(self, workflow_id: str) -> Workflow:
        with self.engine.connect() as conn:
            row = conn.execute(select(workflows).where(workflows.c.id == workflow_id)).first()
        if not row:
            raise WorkflowError(f"workflow {workflow_id} not found", 404)
        return self._row_to_wf(row)

    def list(self, state: Optional[str] = None, kind: Optional[str] = None,
             entity_urn: Optional[str] = None, limit: int = 200) -> list[Workflow]:
        stmt = select(workflows).order_by(workflows.c.updated_at.desc()).limit(limit)
        if state:
            stmt = stmt.where(workflows.c.state == state)
        if kind:
            stmt = stmt.where(workflows.c.kind == kind)
        if entity_urn:
            stmt = stmt.where(workflows.c.entity_urn == entity_urn)
        with self.engine.connect() as conn:
            return [self._row_to_wf(r) for r in conn.execute(stmt)]

    def _save(self, wf: Workflow, expected_version: Optional[int]) -> None:
        doc = wf.model_dump(mode="json", include={"quorum", "approvals", "rejections", "payload", "checks", "history"})
        wf.updated_at = _iso_now()
        with self.engine.begin() as conn:
            if expected_version is None:
                conn.execute(workflows.insert().values(
                    id=wf.id, kind=wf.kind, entity_urn=wf.entity_urn, state=wf.state, requester=wf.requester,
                    template=wf.template, doc=doc, created_at=wf.created_at, updated_at=wf.updated_at,
                    valid_until=wf.valid_until, version=1))
                return
            res = conn.execute(update(workflows).where(
                (workflows.c.id == wf.id) & (workflows.c.version == expected_version)).values(
                state=wf.state, doc=doc, updated_at=wf.updated_at, valid_until=wf.valid_until,
                version=expected_version + 1))
            if res.rowcount != 1:
                raise WorkflowError("workflow was modified concurrently; retry", 409)

    def _version(self, workflow_id: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(select(workflows.c.version).where(workflows.c.id == workflow_id)).scalar() or 0)

    def _transition(self, wf: Workflow, to: str, actor: str, comment: Optional[str]) -> None:
        wf.history.append(HistoryEntry(at=_iso_now(), actor=actor, from_state=wf.state, to_state=to, comment=comment))
        if self.audit:
            self.audit.record("governance", f"workflow.{to.lower()}", actor, wf.entity_urn,
                              {"workflow_id": wf.id, "kind": wf.kind, "from": wf.state, "comment": comment})
        wf.state = to  # type: ignore[assignment]

    # ---- commands ------------------------------------------------------------------
    def create(self, req: CreateWorkflowRequest, actor: str) -> Workflow:
        template_name = req.template or ("ai-documentation" if req.kind == "DOCUMENTATION" else "standard-certification")
        template = self.templates.get(template_name)
        if template is None or template.kind != req.kind:
            raise WorkflowError(f"unknown template {template_name!r} for kind {req.kind}", 400)
        active = [w for w in self.list(entity_urn=req.entity_urn, kind=req.kind)
                  if w.state in ("DRAFT", "SUBMITTED", "IN_REVIEW", "CHANGES_REQUESTED")]
        if active and req.kind == "CERTIFICATION":
            raise WorkflowError(f"an open certification request already exists: {active[0].id}")
        now = _iso_now()
        wf = Workflow(id=f"wf_{uuid.uuid4().hex[:16]}", kind=req.kind, entity_urn=req.entity_urn, state="DRAFT",
                      requester=actor, template=template_name, quorum=template.quorum, payload=req.payload,
                      created_at=now, updated_at=now)
        wf.history.append(HistoryEntry(at=now, actor=actor, from_state=None, to_state="DRAFT"))
        if req.submit:
            wf.checks = self.checks.run(wf.entity_urn, template.checks)
            self._transition(wf, "SUBMITTED", actor, None)
        self._save(wf, None)
        self._fire(wf, wf.history[1:])
        return wf

    def act(self, workflow_id: str, action: str, actor: str, comment: Optional[str] = None) -> Workflow:
        wf = self.get(workflow_id)
        version = self._version(workflow_id)
        template = self.templates[wf.template]
        handler = {
            "submit": self._submit, "approve": self._approve, "reject": self._reject,
            "request-changes": self._request_changes, "revoke": self._revoke, "recheck": self._recheck,
        }.get(action)
        if handler is None:
            raise WorkflowError(f"unknown action {action!r}", 400)
        seen = len(wf.history)
        handler(wf, template, actor, comment)
        self._save(wf, version)
        self._fire(wf, wf.history[seen:])
        return wf

    def _require_reviewer(self, wf: Workflow, template: Template, actor: str) -> None:
        if actor == wf.requester:
            raise WorkflowError("separation of duties: the requester cannot review their own request", 403)
        if not self.is_eligible_reviewer(actor, wf, template):
            raise WorkflowError("actor is not an eligible reviewer for this workflow", 403)

    def _submit(self, wf: Workflow, template: Template, actor: str, comment: Optional[str]) -> None:
        if wf.state not in ("DRAFT", "CHANGES_REQUESTED", "EXPIRED"):
            raise WorkflowError(f"cannot submit from {wf.state}")
        if actor != wf.requester and not self.is_eligible_reviewer(actor, wf, template):
            raise WorkflowError("only the requester or a reviewer can resubmit", 403)
        wf.approvals, wf.rejections = [], []
        wf.checks = self.checks.run(wf.entity_urn, template.checks)
        self._transition(wf, "SUBMITTED", actor, comment)

    def _approve(self, wf: Workflow, template: Template, actor: str, comment: Optional[str]) -> None:
        if wf.state not in ("SUBMITTED", "IN_REVIEW"):
            raise WorkflowError(f"cannot approve from {wf.state}")
        self._require_reviewer(wf, template, actor)
        if any(v.actor == actor for v in wf.approvals):
            raise WorkflowError("actor already approved")
        wf.approvals.append(Vote(actor=actor, at=_iso_now(), comment=comment))
        if wf.state == "SUBMITTED":
            self._transition(wf, "IN_REVIEW", actor, comment)
        if len(wf.approvals) >= wf.quorum:
            self._transition(wf, "APPROVED", actor, f"quorum {len(wf.approvals)}/{wf.quorum} reached")
            self._after_approved(wf, template, actor)

    def _after_approved(self, wf: Workflow, template: Template, actor: str) -> None:
        if wf.kind == "DOCUMENTATION":
            if self.on_documentation_approved:
                self.on_documentation_approved(wf, actor)
            wf.payload["applied"] = True
            wf.payload["applied_at"] = _iso_now()
            return
        wf.checks = self.checks.run(wf.entity_urn, template.checks)
        if all(c.passed for c in wf.checks):
            until = None
            if template.validity_days:
                until = (datetime.now(timezone.utc) + timedelta(days=template.validity_days)).isoformat()
            wf.valid_until = until
            self._transition(wf, "CERTIFIED", DHE_ACTOR, "all entry checks passed")
            self._project_certification(wf, certified=True, approver=actor)

    def _recheck(self, wf: Workflow, template: Template, actor: str, comment: Optional[str]) -> None:
        if wf.state != "APPROVED" or wf.kind != "CERTIFICATION":
            raise WorkflowError("recheck is only valid for approved certifications awaiting checks")
        self._after_approved(wf, template, wf.approvals[-1].actor if wf.approvals else actor)

    def _reject(self, wf: Workflow, template: Template, actor: str, comment: Optional[str]) -> None:
        if wf.state not in ("SUBMITTED", "IN_REVIEW"):
            raise WorkflowError(f"cannot reject from {wf.state}")
        self._require_reviewer(wf, template, actor)
        wf.rejections.append(Vote(actor=actor, at=_iso_now(), comment=comment))
        self._transition(wf, "REJECTED", actor, comment)

    def _request_changes(self, wf: Workflow, template: Template, actor: str, comment: Optional[str]) -> None:
        if wf.state not in ("SUBMITTED", "IN_REVIEW"):
            raise WorkflowError(f"cannot request changes from {wf.state}")
        self._require_reviewer(wf, template, actor)
        self._transition(wf, "CHANGES_REQUESTED", actor, comment)

    def _revoke(self, wf: Workflow, template: Template, actor: str, comment: Optional[str]) -> None:
        if wf.state != "CERTIFIED":
            raise WorkflowError(f"cannot revoke from {wf.state}")
        if actor != DHE_ACTOR and not self.is_eligible_reviewer(actor, wf, template):
            raise WorkflowError("only reviewers can revoke a certification", 403)
        self._transition(wf, "REVOKED", actor, comment)
        self._project_certification(wf, certified=False, approver=actor, status="REVOKED")

    # ---- system-driven transitions -------------------------------------------------
    def expire_due(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        count = 0
        for wf in self.list(state="CERTIFIED", kind="CERTIFICATION", limit=10_000):
            if wf.valid_until and datetime.fromisoformat(wf.valid_until) <= now:
                version = self._version(wf.id)
                self._transition(wf, "EXPIRED", DHE_ACTOR, "validity period ended")
                self._project_certification(wf, certified=False, approver=DHE_ACTOR, status="EXPIRED")
                self._save(wf, version)
                count += 1
        return count

    def on_quality_failure(self, entity_urn: str, severity: str, reason: str) -> int:
        """Auto-revoke certifications when a CRITICAL monitor fails (template opt-in)."""
        if severity != "CRITICAL":
            return 0
        revoked = 0
        for wf in self.list(state="CERTIFIED", kind="CERTIFICATION", entity_urn=entity_urn):
            if self.templates[wf.template].auto_revoke_on_critical_dq:
                self.act(wf.id, "revoke", DHE_ACTOR, f"auto-revoked: critical data-quality failure: {reason}")
                revoked += 1
        return revoked

    # ---- projection into DataHub ---------------------------------------------------
    def _project_certification(self, wf: Workflow, certified: bool, approver: str, status: str = "CERTIFIED") -> None:
        if self.graph is None:
            return
        urn = wf.entity_urn
        sp = self.graph.get_aspect(urn, "structuredProperties") or {"properties": []}
        ours = {CERT_STATUS_PROP, CERT_BY_PROP, CERT_UNTIL_PROP}
        props = [p for p in sp.get("properties", []) if p.get("propertyUrn") not in ours]
        stamp = audit_stamp(approver)
        props.append({"propertyUrn": CERT_STATUS_PROP, "values": [{"string": status}], "lastModified": stamp})
        if certified:
            props.append({"propertyUrn": CERT_BY_PROP, "values": [{"string": approver}], "lastModified": stamp})
            if wf.valid_until:
                props.append({"propertyUrn": CERT_UNTIL_PROP, "values": [{"string": wf.valid_until}],
                              "lastModified": stamp})
        tags_aspect = self.graph.get_aspect(urn, "globalTags") or {"tags": []}
        tags = [t for t in tags_aspect.get("tags", []) if t.get("tag") != CERTIFIED_TAG]
        if certified:
            tags.append({"tag": CERTIFIED_TAG, "attribution": {
                "time": now_ms(), "actor": approver, "source": "urn:li:dataHubAction:dhe-certification",
                "sourceDetail": {"workflow_id": wf.id}}})
        self.graph.emit([
            make_mcp(urn, "structuredProperties", {"properties": props}, system_properties={"dhe_source": "governance"}),
            make_mcp(urn, "globalTags", {"tags": tags}, system_properties={"dhe_source": "governance"}),
        ])


def bootstrap_mcps() -> list[dict]:
    """Definitions DHE relies on: certification structured properties, tag, PII glossary terms."""
    entity_types = [f"urn:li:entityType:datahub.{t}" for t in ("dataset", "dashboard", "chart", "dataJob", "dataProduct")]
    mcps = []
    for qn, name, allowed in (
        ("io.dhe.certification.status", "Certification Status", ["CERTIFIED", "REVOKED", "EXPIRED"]),
        ("io.dhe.certification.certifiedBy", "Certified By", None),
        ("io.dhe.certification.validUntil", "Certification Valid Until", None),
    ):
        definition: dict[str, Any] = {
            "qualifiedName": qn, "displayName": name, "valueType": "urn:li:dataType:datahub.string",
            "cardinality": "SINGLE", "entityTypes": entity_types, "immutable": False,
            "description": "Managed by DataHub Enterprise certification workflows; do not edit manually.",
        }
        if allowed:
            definition["allowedValues"] = [{"value": {"string": v}} for v in allowed]
        mcps.append(make_mcp(f"urn:li:structuredProperty:{qn}", "propertyDefinition", definition))
    mcps.append(make_mcp(CERTIFIED_TAG, "tagProperties", {"name": "Certified", "colorHex": "#2e7d32",
                                                          "description": "Certified by a DHE workflow"}))
    for kind in ("Email", "Phone", "SSN", "CreditCard", "IBAN", "IPAddress", "DateOfBirth", "Name", "Address",
                 "Passport"):
        mcps.append(make_mcp(f"urn:li:glossaryTerm:Classification.PII.{kind}", "glossaryTermInfo", {
            "name": kind, "definition": f"Personally identifiable information: {kind}", "termSource": "INTERNAL",
            "customProperties": {"dhe_managed": "true"}}))
    return mcps
