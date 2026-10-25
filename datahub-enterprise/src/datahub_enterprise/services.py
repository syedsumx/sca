"""Service container: builds every DHE component from :class:`Settings`.

All entry points (HTTP apps, scheduler, MCP server, actions) obtain their
dependencies here, so wiring lives in exactly one place and tests can inject fakes.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from datahub_enterprise.agent.gateway import GatewayKeys, GatewayService
from datahub_enterprise.ai.catalog import Catalog
from datahub_enterprise.ai.docgen import DocGenerator, apply_documentation
from datahub_enterprise.ai.embeddings import build_embedder, build_vector_store
from datahub_enterprise.ai.llm import LLMProvider, build_provider
from datahub_enterprise.ai.nlq import NaturalLanguageQuery
from datahub_enterprise.ai.semantic import SemanticIndex
from datahub_enterprise.common.audit import AuditLog
from datahub_enterprise.common.config import Settings
from datahub_enterprise.common.db import get_engine, init_db
from datahub_enterprise.common.gms import GmsClient, MetadataGraph
from datahub_enterprise.common.security import IdentityResolver, Principal
from datahub_enterprise.governance.attributes import AttributeResolver
from datahub_enterprise.governance.certification import EntryChecks, Template, Workflow, WorkflowService, load_templates
from datahub_enterprise.governance.pdp import (
    AuthorizeRequest,
    PolicyDecisionPoint,
    PolicyRepository,
    ResourceRef,
    load_bundle_from_dir,
    sync_native_policies,
)
from datahub_enterprise.quality.collectors import ConnectionRegistry, DataHubSignalCollector, SqlCollector
from datahub_enterprise.quality.engine import QualityEngine, webhook_notifier
from datahub_enterprise.quality.store import QualityStore

logger = logging.getLogger(__name__)


class Forbidden(Exception):
    def __init__(self, message: str):
        super().__init__(message)


@dataclass
class Services:
    settings: Settings
    engine: Any
    graph: Optional[MetadataGraph]
    audit: AuditLog
    identity: IdentityResolver
    attributes: AttributeResolver
    pdp: PolicyDecisionPoint
    policy_repo: PolicyRepository
    quality: QualityEngine
    workflows: WorkflowService
    gateway: Optional[GatewayService]
    catalog: Optional[Catalog]
    llm: Optional[LLMProvider]
    nlq: Optional[NaturalLanguageQuery]
    semantic: Optional[SemanticIndex]
    docgen: Optional[DocGenerator]
    _threads: list = field(default_factory=list)

    # ---- authorization helper shared by every DHE API ------------------------------
    def authorize(self, principal: Principal, privilege: str, resource_urn: Optional[str],
                  raise_on_deny: bool = True) -> bool:
        if principal.is_service:
            return True
        resource = None
        if resource_urn:
            resource = ResourceRef(type=resource_urn.split(":", 3)[2] if resource_urn.startswith("urn:li:")
                                   else "unknown", urn=resource_urn)
        decision = self.pdp.decide(AuthorizeRequest(actor=principal.urn, privilege=privilege, resource=resource,
                                                    environment={"channel": "dhe-api"}))
        allowed = decision.decision == "ALLOW"
        if not allowed and raise_on_deny:
            raise Forbidden(f"{privilege} denied on {resource_urn or 'platform'}: {decision.reason}")
        return allowed

    def can_view(self, principal: Principal):
        return lambda urn: self.authorize(principal, "VIEW_ENTITY_PAGE", urn, raise_on_deny=False)

    def record_llm_call(self, principal: Optional[Principal]):
        def _record(task: str, usage: Any) -> None:
            self.audit.record("ai", task, principal.urn if principal else "unknown", None, {
                "model": getattr(usage, "model", None), "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0)})
        return _record

    # ---- background maintenance ----------------------------------------------------
    def start_background(self, native_policy_interval: int = 120, workflow_interval: int = 600) -> None:
        """Native-policy import retries until GMS (and its token) is available."""
        stop = threading.Event()

        def policy_loop() -> None:
            while not stop.is_set():
                if self.graph is not None and self.settings.authz.import_native_policies:
                    n = sync_native_policies(self.pdp, self.graph)
                    if n >= 0:
                        logger.info("imported %d native DataHub policies", n)
                    stop.wait(native_policy_interval if n >= 0 else 15)
                else:
                    stop.wait(native_policy_interval)

        def workflow_loop() -> None:
            while not stop.wait(workflow_interval):
                try:
                    expired = self.workflows.expire_due()
                    if expired:
                        logger.info("expired %d certifications", expired)
                except Exception:
                    logger.exception("certification expiry sweep failed")

        for target in (policy_loop, workflow_loop):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)
        self._threads.append(stop)

    def stop_background(self) -> None:
        for t in self._threads:
            if isinstance(t, threading.Event):
                t.set()


def build_services(settings: Settings, graph: Optional[MetadataGraph] = None, llm: Optional[LLMProvider] = None,
                   with_gateway: bool = True, with_ai: bool = True) -> Services:
    engine = get_engine(settings.database_url)
    init_db(engine)
    audit = AuditLog(engine)
    if graph is None and settings.gms.url:
        graph = GmsClient(settings.gms.url, settings.gms.token, settings.gms.timeout_seconds)

    identity = IdentityResolver(settings.gms.url, settings.gms.frontend_url, settings.service_token,
                                settings.dev_trust_actor_header)
    attributes = AttributeResolver(graph)
    pdp = PolicyDecisionPoint(attributes, settings.authz.mode, settings.authz.root_users,
                              settings.authz.decision_cache_ttl_seconds, audit)
    policy_repo = PolicyRepository(engine, audit)
    file_bundle, rbac = load_bundle_from_dir(settings.authz.policy_dir)
    pdp.set_rbac(rbac)
    pdp.set_bundle(policy_repo.latest() or file_bundle)

    quality_store = QualityStore(engine)
    connections = ConnectionRegistry.from_file(os.environ.get("DHE_QUALITY_CONNECTIONS"))
    collectors = {"datahub": DataHubSignalCollector(graph), "sql": SqlCollector(connections)} if graph else \
        {"sql": SqlCollector(connections)}

    templates = load_templates(os.environ.get("DHE_WORKFLOW_DIR"))
    checks = EntryChecks(graph, lambda urn: sum(
        1 for m in quality_store.list_monitors(urn) if quality_store.get_state(m.id).get("open_incident_urn")))

    def eligible(actor: str, wf: Workflow, template: Template) -> bool:
        subject = attributes.subject(actor)
        if set(subject.get("groups", [])) & set(template.reviewer_groups):
            return True
        if "owners" in template.reviewer_groups:
            owners = set(attributes.resource(wf.entity_urn).get("owners", []))
            if actor in owners or owners & set(subject.get("groups", [])):
                return True
        privilege = f"DHE_APPROVE_{wf.kind}"
        return pdp.decide(AuthorizeRequest(actor=actor, privilege=privilege,
                                           resource=ResourceRef(type=wf.entity_urn.split(":", 3)[2],
                                                                urn=wf.entity_urn))).decision == "ALLOW"

    def on_doc_approved(wf: Workflow, approver: str) -> None:
        if graph is None:
            raise RuntimeError("GMS not configured")
        apply_documentation(graph, wf.entity_urn, wf.payload["draft"], approver)
        attributes.invalidate(wf.entity_urn)

    workflows = WorkflowService(engine, graph, templates, checks, eligible, audit, on_doc_approved)

    base_notifier = webhook_notifier()

    def notifier(monitor, ev, kind):
        base_notifier(monitor, ev, kind)
        if kind == "failing":
            workflows.on_quality_failure(monitor.dataset_urn, monitor.severity, ev.message)

    quality = QualityEngine(quality_store, graph, collectors, notifier, audit)

    gateway = None
    if with_gateway:
        gateway = GatewayService(engine, GatewayKeys(settings.gateway.key_dir), graph, audit,
                                 settings.gateway.max_clock_skew_seconds, settings.gateway.task_lease_seconds)

    catalog = nlq = semantic = docgen = None
    if with_ai and graph is not None:
        catalog = Catalog(graph)
        llm = llm if llm is not None else build_provider(settings)
        nlq = NaturalLanguageQuery(catalog, llm, settings.ai.model_nlq)
        embedder = build_embedder(settings)
        semantic = SemanticIndex(catalog, embedder, build_vector_store(engine, embedder.dim))
        docgen = DocGenerator(catalog, llm, settings.ai.model_docgen)

    return Services(settings, engine, graph, audit, identity, attributes, pdp, policy_repo, quality, workflows,
                    gateway, catalog, llm, nlq, semantic, docgen)
