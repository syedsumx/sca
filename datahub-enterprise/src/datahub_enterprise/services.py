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
    # ---- active metadata layer ------------------------------------------------------
    personas: Optional[Any] = None
    notify: Optional[Any] = None
    slack: Optional[Any] = None
    collab: Optional[Any] = None
    marketplace: Optional[Any] = None
    context: Optional[Any] = None
    embed: Optional[Any] = None
    assistant: Optional[Any] = None
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

    def can_view_as(self, actor: str):
        return self.can_view(Principal(urn=actor, via="delegated"))

    def explicitly_denied(self, principal: Principal, privilege: str, resource_urn: Optional[str]) -> bool:
        """True only for an explicit DENY (strict denies such as purposes); NOT_APPLICABLE is not a deny."""
        if principal.is_service:
            return False
        resource = ResourceRef(type=resource_urn.split(":", 3)[2], urn=resource_urn) \
            if resource_urn and resource_urn.startswith("urn:li:") else None
        return self.pdp.decide(AuthorizeRequest(actor=principal.urn, privilege=privilege, resource=resource,
                                                environment={"channel": "dhe-api"})).decision == "DENY"

    def subject_of(self, actor: str) -> dict:
        """Resolved subject (groups, attributes) plus current persona memberships."""
        import time

        subject = dict(self.attributes.subject(actor))
        if self.personas is not None:
            subject["personas"] = self.personas.memberships(subject, int(time.time() * 1000))
        return subject

    def is_admin(self, actor: str) -> bool:
        return self.authorize(Principal(urn=actor, via="delegated"), "MANAGE_POLICIES", None, raise_on_deny=False)

    def trust(self, urns: list[str]) -> dict[str, dict]:
        """Trust badges per asset: certification status + worst current quality status."""
        out: dict[str, dict] = {}
        rank = {"FAILING": 3, "ERROR": 2, "WARNING": 1, "HEALTHY": 0}
        for urn in urns:
            cert = None
            try:
                cert = self.attributes.resource(urn).get("certification")
            except Exception:
                pass
            status = None
            store = self.quality.store
            for m in store.list_monitors(urn):
                ev = m.last_evaluation
                st = {"FAIL": "FAILING", "WARN": "WARNING", "PASS": "HEALTHY", "ERROR": "ERROR"}.get(
                    ev.status if ev else "")
                if st in rank and (status is None or rank[st] > rank[status]):
                    status = st
            out[urn] = {"certification": cert, "quality_status": status}
        return out

    def summaries(self, urns: list[str]) -> dict[str, dict]:
        """Display data per urn. Without catalog access, names are derived from the urn so callers can
        still tell "unresolvable right now" apart from "deleted" (absent from a successful lookup)."""
        if not urns:
            return {}
        if self.catalog is not None:
            try:
                return self.catalog.summaries(urns)
            except Exception:
                logger.exception("entity summary lookup failed; falling back to urn-derived names")
        return {u: _urn_summary(u) for u in urns}

    def explainer_for(self, principal: Principal):
        from datahub_enterprise.ai.lineage_explainer import LineageExplainer

        return LineageExplainer(self.catalog, self.quality.store, self.context, self.llm,
                                self.settings.ai.model, self.can_view(principal))

    def tools_for(self, principal: Principal):
        """MCP tool implementations bound to an explicit principal (used by the assistant)."""
        from datahub_enterprise.ai.mcp_server import DheTools

        class _Scoped(DheTools):
            @staticmethod
            def principal() -> Principal:
                return principal

        return _Scoped(self)

    def record_llm_call(self, principal: Optional[Principal]):
        def _record(task: str, usage: Any) -> None:
            self.audit.record("ai", task, principal.urn if principal else "unknown", None, {
                "model": getattr(usage, "model", None), "input_tokens": getattr(usage, "input_tokens", 0),
                "output_tokens": getattr(usage, "output_tokens", 0)})
        return _record

    # ---- background maintenance ----------------------------------------------------
    def start_background(self, native_policy_interval: int = 120, workflow_interval: int = 600,
                         layer_reload_interval: float = 10.0) -> None:
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

        def layers_loop() -> None:
            # Personas, purposes, grants and the active bundle are edited through `dhe api`; other
            # processes (dhe-pdp behind the GMS plugin, dhe-gms-proxy, the scheduler) share the database.
            while not stop.wait(layer_reload_interval):
                try:
                    if self.personas is not None:
                        self.personas.reload()
                    latest = self.policy_repo.latest()
                    if latest is not None and latest != self.pdp.bundle:
                        self.pdp.set_bundle(latest)
                except Exception:
                    logger.exception("policy layer reload failed")

        for target in (policy_loop, workflow_loop, layers_loop):
            t = threading.Thread(target=target, daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)
        self._threads.append(stop)

    def stop_background(self) -> None:
        for t in self._threads:
            if isinstance(t, threading.Event):
                t.set()


def _urn_summary(urn: str) -> dict:
    etype = urn.split(":", 3)[2] if urn.startswith("urn:li:") else "unknown"
    name, platform = urn.rsplit(":", 1)[-1], None
    if etype == "dataset" and "," in urn:
        platform = urn.split("urn:li:dataPlatform:", 1)[-1].split(",", 1)[0]
        name = urn.split(",")[1]
    return {"urn": urn, "type": etype.upper(), "name": name, "platform": platform, "description": None,
            "domain": None}


def build_services(settings: Settings, graph: Optional[MetadataGraph] = None, llm: Optional[LLMProvider] = None,
                   with_gateway: bool = True, with_ai: bool = True) -> Services:
    engine = get_engine(settings.database_url)
    init_db(engine)
    audit = AuditLog(engine)
    if graph is None and settings.gms.url:
        graph = GmsClient(settings.gms.url, settings.gms.token, settings.gms.timeout_seconds,
                          token_file=settings.gms.token_file)

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

    services = Services(settings, engine, graph, audit, identity, attributes, pdp, policy_repo, quality, workflows,
                        gateway, catalog, llm, nlq, semantic, docgen)
    _build_active_metadata(services)
    return services


def _build_active_metadata(s: Services) -> None:
    """Personas & Purposes, notifications, collaboration, marketplace, context mining, embeds, assistant."""
    from datahub_enterprise.ai.assistant import Assistant
    from datahub_enterprise.collab.service import CollabService
    from datahub_enterprise.context.miner import ContextStore
    from datahub_enterprise.embed.service import EmbedService
    from datahub_enterprise.governance.personas import PersonaEngine
    from datahub_enterprise.marketplace.service import MarketplaceService
    from datahub_enterprise.notify.channels import EmailSender, SlackSender, TeamsSender, WebhookSender
    from datahub_enterprise.notify.hub import Event, NotificationHub, Route
    from datahub_enterprise.notify.slack import SlackInteractions

    cfg = s.settings.active
    s.personas = PersonaEngine(s.engine, s.pdp, s.audit, s.attributes)

    def email_of(actor: str) -> Optional[str]:
        if not actor.startswith("urn:li:corpuser:"):
            return None
        try:
            email = s.attributes.subject(actor).get("attributes", {}).get("email")
        except Exception:
            email = None
        username = actor.split(":", 3)[3]
        return email or (username if "@" in username else None)

    def owners(urn: str) -> list[str]:
        try:
            return list(s.attributes.resource(urn).get("owners", []))
        except Exception:
            return []

    def domains_of(urn: str) -> list[str]:
        try:
            return list(s.attributes.resource(urn).get("domains", []))
        except Exception:
            return []

    def followers(urn: str) -> list[str]:
        out = s.collab.followers(urn) if s.collab else []
        if s.marketplace is not None and urn.startswith("urn:li:dataProduct:"):
            out += [a for a in s.marketplace.subscribers(urn) if a not in out]
        return out

    hub = NotificationHub(s.engine, cfg.public_url, cfg.action_link_secret, s.audit, followers, owners, domains_of)
    hub.channels["webhook"] = WebhookSender()
    hub.channels["teams"] = TeamsSender()
    slack_sender = None
    if cfg.slack_bot_token:
        slack_sender = SlackSender(cfg.slack_bot_token, email_of)
        hub.channels["slack"] = slack_sender
    if cfg.smtp_url:
        hub.channels["email"] = EmailSender(cfg.smtp_url, cfg.email_from, email_of)
    if cfg.teams_webhook_url and not hub.list_routes():
        hub.put_routes([Route(id="teams-default", channel="teams", target=cfg.teams_webhook_url,
                              event_types=["quality.incident.raised", "certification.requested", "asset.deprecated",
                                           "context.drift"])])
    s.notify = hub

    def entity_name(urn: str) -> str:
        return (s.summaries([urn]).get(urn) or {}).get("name") or urn

    s.collab = CollabService(s.engine, hub, entity_name)
    s.context = ContextStore(s.engine, s.graph, s.collab, s.audit, hub)
    if s.graph is not None:
        s.marketplace = MarketplaceService(s.engine, s.graph, s.personas, hub, s.audit, s.attributes, s.trust,
                                           s.is_admin)
    s.embed = EmbedService(s.engine, cfg.embed_signing_secret, cfg.embed_allowed_origins, cfg.public_url,
                           s.catalog, s.attributes, s.quality.store, s.can_view_as)

    def products_for(principal: Principal, query: str) -> list[dict]:
        if s.marketplace is None:
            return []
        return s.marketplace.list_products(principal.urn, query, limit=20, can_view=s.can_view(principal))

    s.assistant = Assistant(s.engine, s.tools_for, s.llm, s.explainer_for, s.nlq, products_for,
                            s.settings.ai.model, cfg.assistant_effort)

    # ---- inline actions (Slack buttons, Teams/email signed links, in-app) -----------
    if s.marketplace is not None:
        hub.register_action("access_request", s.marketplace.action_handler)

    def workflow_action(action: str, object_id: str, actor: str, comment: Optional[str]) -> str:
        from datahub_enterprise.governance.certification import WorkflowError

        if action not in ("approve", "reject", "request-changes"):
            raise ValueError(f"unsupported workflow action {action}")
        try:
            wf = s.workflows.act(object_id, action, actor, comment)
        except WorkflowError as e:
            raise (PermissionError(str(e)) if e.status == 403 else ValueError(str(e)))
        return f"{wf.kind.title()} request {wf.id} is now {wf.state.lower().replace('_', ' ')}"

    def finding_action(action: str, object_id: str, actor: str, comment: Optional[str]) -> str:
        f = s.context.get(object_id)
        p = Principal(urn=actor, via="delegated")
        if action == "dismiss":
            s.context.set_status(object_id, "DISMISSED", actor)
            return f"Dismissed: {f['title']}"
        if action != "accept":
            raise ValueError(f"unsupported finding action {action}")
        for e in f.get("entities", []):
            if e.startswith("urn:li:dataset:") and not s.authorize(p, "EDIT_ENTITY_GLOSSARY_TERMS", e, False):
                raise PermissionError(f"you cannot edit glossary terms on {e}")
        s.context.accept(object_id, actor)
        return f"Accepted: {f['title']}"

    def deprecation_action(action: str, object_id: str, actor: str, comment: Optional[str]) -> str:
        if action != "acknowledge":
            raise ValueError(f"unsupported deprecation action {action}")
        s.audit.record("notify", "deprecation.acknowledged", actor, object_id, {"comment": comment})
        return "Thanks — the owners can see you acknowledged the deprecation."

    hub.register_action("workflow", workflow_action)
    hub.register_action("finding", finding_action)
    hub.register_action("deprecation", deprecation_action)

    # ---- event sources ----------------------------------------------------------------
    _hook_workflow_events(s, Event)
    _hook_quality_events(s, Event)

    if cfg.slack_signing_secret:
        def resolve_slack_actor(slack_user_id: str) -> Optional[str]:
            if slack_sender is None:
                return None
            try:
                email = slack_sender.user_email(slack_user_id)
            except Exception:
                return None
            return f"urn:li:corpuser:{email}" if email else None

        def slack_search(actor: str, query: str) -> list[dict]:
            if s.catalog is None:
                return []
            check = s.can_view_as(actor)
            return [r for r in s.catalog.search(query, None, None, 20) if check(r["urn"])]

        def slack_ask(actor: str, question: str) -> str:
            out = s.assistant.chat(Principal(urn=actor, via="delegated"), question)
            return out["reply_md"]

        s.slack = SlackInteractions(hub, cfg.slack_signing_secret, resolve_slack_actor, slack_search, slack_ask)


def _hook_workflow_events(s: Services, Event) -> None:
    """Certification requests / decisions → notifications with inline Approve / Reject."""
    from datahub_enterprise.notify.hub import Action

    def on_transition(wf, entry) -> None:
        to, actor = entry.to_state, entry.actor
        if to == "SUBMITTED":
            template = s.workflows.templates.get(wf.template)
            reviewers = [g for g in (template.reviewer_groups if template else []) if g.startswith("urn:li:")]
            s.notify.publish(Event(
                type="certification.requested", title=f"{wf.kind.title()} review requested",
                body=f"{actor} asked for a {wf.kind.lower()} review of {wf.entity_urn}.", actor=actor,
                entity_urn=wf.entity_urn, recipients=reviewers, include_owners=True,
                actions=[Action("approve", "workflow", wf.id, "Approve", "primary"),
                         Action("reject", "workflow", wf.id, "Reject", "danger")],
                payload={"workflow_id": wf.id}))
        elif to in ("APPROVED", "REJECTED", "CHANGES_REQUESTED"):
            s.notify.publish(Event(
                type="certification.decided", title=f"{wf.kind.title()} {to.lower().replace('_', ' ')}",
                body=entry.comment or f"{actor} decided on {wf.entity_urn}.", actor=actor, entity_urn=wf.entity_urn,
                recipients=[wf.requester], include_followers=to == "APPROVED", payload={"workflow_id": wf.id}))

    s.workflows.listeners.append(on_transition)


def _hook_quality_events(s: Services, Event) -> None:
    """Quality incidents → owners and followers of the dataset."""
    original = s.quality.notifier

    def notifier(monitor, ev, kind):
        original(monitor, ev, kind)
        if kind not in ("failing", "recovered"):
            return
        try:
            raised = kind == "failing"
            s.notify.publish(Event(
                type="quality.incident.raised" if raised else "quality.incident.resolved",
                title=f"{'Quality incident' if raised else 'Recovered'}: {monitor.name}",
                body=ev.message or "", actor="urn:li:corpuser:dhe-quality", entity_urn=monitor.dataset_urn,
                include_followers=True, include_owners=True,
                payload={"monitor_id": monitor.id, "severity": monitor.severity}))
        except Exception:
            logger.exception("quality notification failed")

    s.quality.notifier = notifier
