"""Service container: builds every Cairn component from :class:`Settings`.

All entry points (HTTP apps, scheduler, MCP server, actions) obtain their
dependencies here, so wiring lives in exactly one place and tests can inject fakes.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

from cairn.agent.gateway import GatewayKeys, GatewayService
from cairn.ai.catalog import Catalog
from cairn.ai.docgen import DocGenerator, apply_documentation
from cairn.ai.embeddings import build_embedder, build_vector_store
from cairn.ai.llm import LLMProvider, build_provider
from cairn.ai.nlq import NaturalLanguageQuery
from cairn.ai.semantic import SemanticIndex
from cairn.ai.usage import MeteredProvider, UsageMeter, set_actor
from cairn.common.audit import AuditLog
from cairn.common.config import Settings
from cairn.common.db import get_engine, init_db
from cairn.common.core import CoreClient, MetadataGraph
from cairn.common.security import IdentityResolver, Principal
from cairn.governance.attributes import AttributeResolver
from cairn.governance.certification import EntryChecks, Template, Workflow, WorkflowService, load_templates
from cairn.governance.pdp import (
    AuthorizeRequest,
    PolicyDecisionPoint,
    PolicyRepository,
    ResourceRef,
    load_bundle_from_dir,
    sync_native_policies,
)
from cairn.quality.collectors import ConnectionRegistry, CoreSignalCollector, SqlCollector
from cairn.quality.engine import QualityEngine, webhook_notifier
from cairn.quality.store import QualityStore

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
    ai_usage: Optional[Any] = None
    forms: Optional[Any] = None
    bulk: Optional[Any] = None
    tag_sync: Optional[Any] = None
    connections: Optional[Any] = None       # named warehouse connections (quality, tag sync)
    contracts: Optional[Any] = None
    automations: Optional[Any] = None
    reports: Optional[Any] = None
    costs: Optional[Any] = None
    data_access: Optional[Any] = None
    ask: Optional[Any] = None
    semantic_layer: Optional[Any] = None
    ticketing: Optional[Any] = None
    steward: Optional[Any] = None
    context_layer: Optional[Any] = None
    licence: Optional[Any] = None
    extensions: Optional[Any] = None
    ai_systems: Optional[Any] = None
    agents: Optional[Any] = None
    playbooks: Optional[Any] = None
    traces: Optional[Any] = None
    slack_events: Optional[Any] = None
    teams_bot: Optional[Any] = None
    publisher: Optional[Any] = None
    invalidator: Any = None
    _threads: list = field(default_factory=list)
    _product_assets_cache: dict = field(default_factory=dict)

    # ---- authorization helper shared by every Cairn API ------------------------------
    def authorize(self, principal: Principal, privilege: str, resource_urn: Optional[str],
                  raise_on_deny: bool = True) -> bool:
        if principal.is_service:
            return True
        if principal.via == "extension":
            # Exactly the manifest's permissions on its scope; the policy engine is not consulted.
            allowed = self.extensions is not None and self.extensions.allows(principal.urn, privilege, resource_urn)
            if not allowed and raise_on_deny:
                raise Forbidden(f"{privilege} is not among this extension's permissions"
                                + (f" for {resource_urn}" if resource_urn else ""))
            return allowed
        resource = None
        if resource_urn:
            resource = ResourceRef(type=resource_urn.split(":", 3)[2] if resource_urn.startswith("urn:li:")
                                   else "unknown", urn=resource_urn)
        decision = self.pdp.decide(AuthorizeRequest(actor=principal.urn, privilege=privilege, resource=resource,
                                                    environment={"channel": "cairn-api"}))
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
        if principal.via == "extension":
            return not self.authorize(principal, privilege, resource_urn, raise_on_deny=False)
        resource = ResourceRef(type=resource_urn.split(":", 3)[2], urn=resource_urn) \
            if resource_urn and resource_urn.startswith("urn:li:") else None
        return self.pdp.decide(AuthorizeRequest(actor=principal.urn, privilege=privilege, resource=resource,
                                                environment={"channel": "cairn-api"})).decision == "DENY"

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
        """Trust badges per asset: certification status + worst current quality status.

        A fixed number of queries for any number of assets (search results and marketplace pages
        badge dozens at a time)."""
        out: dict[str, dict] = {}
        rank = {"FAILING": 3, "ERROR": 2, "WARNING": 1, "HEALTHY": 0}
        names = {"FAIL": "FAILING", "WARN": "WARNING", "PASS": "HEALTHY", "ERROR": "ERROR"}
        latest = self.quality.store.latest_statuses(urns)
        compliance: dict[str, dict] = {}
        if self.forms is not None:
            try:
                compliance = self.forms.asset_statuses(urns)
            except Exception:
                logger.exception("compliance status lookup failed")
        for urn in urns:
            cert = None
            try:
                cert = self.attributes.resource(urn).get("certification")
            except Exception:
                pass
            found = [names[s] for s in latest.get(urn, []) if s in names]
            out[urn] = {"certification": cert, "quality_status": max(found, key=rank.get) if found else None}
            if self.forms is not None and urn in compliance:
                out[urn]["compliance"] = compliance[urn]["status"]
        if self.contracts is not None and urns and self.contracts.any():
            try:
                self._contract_badges(urns, out)
            except Exception:
                logger.exception("contract status lookup failed")
        return out

    PRODUCT_ASSETS_TTL_SECONDS = 60

    def _product_assets(self, urn: str) -> list[str]:
        """A data product's datasets, cached briefly: badges are drawn many times per page."""
        import time

        cache = self._product_assets_cache
        hit = cache.get(urn)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        assets = [a.get("destinationUrn") for a in (self.graph.get_aspect(urn, "dataProductProperties") or {})
                  .get("assets") or [] if a.get("destinationUrn")]
        cache[urn] = (time.monotonic() + self.PRODUCT_ASSETS_TTL_SECONDS, assets)
        return assets

    def _contract_badges(self, urns: list[str], out: dict[str, dict]) -> None:
        """A dataset shows its contract's status; a data product the worst of its datasets'."""
        rank = {"BROKEN": 2, "PENDING": 1, "MET": 0}
        assets = {u: self._product_assets(u) for u in urns
                  if u.startswith("urn:li:dataProduct:") and self.graph is not None}
        found = self.contracts.statuses(sorted({*urns, *(a for v in assets.values() for a in v)}))
        for urn in urns:
            statuses = [found[a] for a in assets.get(urn, [urn]) if a in found]
            if statuses:
                out[urn]["contract"] = max(statuses, key=rank.get)

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
        from cairn.ai.lineage_explainer import LineageExplainer

        self.ai_actor(principal)
        return LineageExplainer(self.catalog, self.quality.store, self.context, self.llm,
                                self.settings.ai.model, self.can_view(principal))

    def tools_for(self, principal: Principal):
        """MCP tool implementations bound to an explicit principal (used by the assistant)."""
        from cairn.ai.mcp_server import CairnTools

        class _Scoped(CairnTools):
            @staticmethod
            def principal() -> Principal:
                return principal

        return _Scoped(self)

    def ai_actor(self, principal: Optional[Principal]) -> None:
        """Attribute the model calls this request makes (usage, cost, budgets) to ``principal``."""
        set_actor(principal.urn if principal else None)

    # ---- background maintenance ----------------------------------------------------
    def bootstrap_catalog(self, stop: threading.Event, retry_seconds: float = 15) -> None:
        """Create the definitions Cairn relies on (certification properties, Certified tag, PII
        terms) once Cairn Core answers. Skipped when they already exist; idempotent anyway."""
        from cairn.governance.certification import CERT_STATUS_PROP, bootstrap_mcps

        while not stop.is_set() and self.graph is not None:
            try:
                if self.graph.get_aspect(CERT_STATUS_PROP, "propertyDefinition") is None:
                    mcps = bootstrap_mcps()
                    self.graph.emit(mcps)
                    logger.info("created %d catalog definitions (certification, PII terms)", len(mcps))
                return
            except Exception as e:  # noqa: BLE001 - Cairn Core or the token may not be ready yet
                logger.info("catalog bootstrap waiting for Cairn Core: %s", e)
                stop.wait(retry_seconds)

    def maintain_semantic_index(self, stop: threading.Event, interval: float, retry_seconds: float = 30) -> None:
        """Fill the semantic index when it is empty, then reconcile it with the catalog every
        ``interval`` seconds (live changes arrive through the indexer action in between)."""
        if self.semantic is None:
            return
        built = False
        while not stop.is_set():
            try:
                if not built:
                    logger.info("semantic index ready: %s", self.semantic.ensure_built())
                    built = True
                elif interval > 0:
                    logger.info("semantic index reconciled: %s", self.semantic.rebuild())
            except Exception as e:  # noqa: BLE001 - Cairn Core may not be ready yet
                logger.info("semantic index maintenance waiting: %s", e)
                stop.wait(retry_seconds)
                continue
            if interval <= 0 and built:
                return
            stop.wait(interval if interval > 0 else retry_seconds)

    def start_background(self, native_policy_interval: int = 120, workflow_interval: int = 600,
                         layer_reload_interval: float = 10.0, bootstrap_catalog: bool = False) -> None:
        """Native-policy import retries until Cairn Core (and its token) is available. With Kafka
        configured, metadata changes invalidate cached attributes and decisions as they happen."""
        stop = threading.Event()
        policy_changed = threading.Event()

        def policy_loop() -> None:
            while not stop.is_set():
                wait = native_policy_interval
                if self.graph is not None and self.settings.authz.import_native_policies:
                    n = sync_native_policies(self.pdp, self.graph)
                    if n >= 0:
                        logger.info("imported %d native Cairn Core policies", n)
                    wait = native_policy_interval if n >= 0 else 15
                policy_changed.wait(wait)
                policy_changed.clear()

        def workflow_loop() -> None:
            while not stop.wait(workflow_interval):
                try:
                    expired = self.workflows.expire_due()
                    if expired:
                        logger.info("expired %d certifications", expired)
                except Exception:
                    logger.exception("certification expiry sweep failed")

        def layers_loop() -> None:
            # Personas, purposes, grants and the active bundle are edited through `cairn api`; other
            # processes (cairn-pdp behind the Cairn Core plugin, cairn-core-proxy, the scheduler) share the database.
            while not stop.wait(layer_reload_interval):
                try:
                    if self.personas is not None:
                        self.personas.reload()
                    latest = self.policy_repo.latest()
                    if latest is not None and latest != self.pdp.bundle:
                        self.pdp.set_bundle(latest)
                except Exception:
                    logger.exception("policy layer reload failed")

        targets = [policy_loop, workflow_loop, layers_loop]
        if bootstrap_catalog:
            targets.append(lambda: self.bootstrap_catalog(stop))
            interval = float(os.environ.get("CAIRN_SEMANTIC_RECONCILE_SECONDS", "21600"))
            targets.append(lambda: self.maintain_semantic_index(stop, interval))
        for target in targets:
            t = threading.Thread(target=target, daemon=True, name=getattr(target, "__name__", "bootstrap"))
            t.start()
            self._threads.append(t)
        if os.environ.get("CAIRN_MCL_INVALIDATION", "true").lower() == "true" and self.settings.kafka_bootstrap:
            from cairn.governance.invalidation import MclInvalidator, start_consumer

            self.invalidator = MclInvalidator(self.attributes, policy_changed.set)
            t = start_consumer(self.invalidator, self.settings.kafka_bootstrap, self.settings.schema_registry_url,
                               stop)
            if t is not None:
                self._threads.append(t)
        self._threads += [stop, policy_changed]   # set on stop: wakes the policy loop too

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
    if graph is None and settings.core.url:
        graph = CoreClient(settings.core.url, settings.core.token, settings.core.timeout_seconds,
                          token_file=settings.core.token_file)

    identity = IdentityResolver(settings.core.url, settings.core.frontend_url, settings.service_token,
                                settings.dev_trust_actor_header)
    attributes = AttributeResolver(graph)
    pdp = PolicyDecisionPoint(attributes, settings.authz.mode, settings.authz.root_users,
                              settings.authz.decision_cache_ttl_seconds, audit)
    policy_repo = PolicyRepository(engine, audit)
    file_bundle, rbac = load_bundle_from_dir(settings.authz.policy_dir)
    pdp.set_rbac(rbac)
    pdp.set_bundle(policy_repo.latest() or file_bundle)

    quality_store = QualityStore(engine)
    connections = ConnectionRegistry.from_file(os.environ.get("CAIRN_QUALITY_CONNECTIONS")
                                               or "/etc/cairn/config/connections")
    collectors = {"catalog": CoreSignalCollector(graph), "sql": SqlCollector(connections)} if graph else \
        {"sql": SqlCollector(connections)}

    templates = load_templates(os.environ.get("CAIRN_WORKFLOW_DIR"))
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
        privilege = f"CAIRN_APPROVE_{wf.kind}"
        return pdp.decide(AuthorizeRequest(actor=actor, privilege=privilege,
                                           resource=ResourceRef(type=wf.entity_urn.split(":", 3)[2],
                                                                urn=wf.entity_urn))).decision == "ALLOW"

    def on_doc_approved(wf: Workflow, approver: str) -> None:
        if graph is None:
            raise RuntimeError("Cairn Core not configured")
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
    ai_usage = UsageMeter.from_env(engine, audit)
    if with_ai and graph is not None:
        catalog = Catalog(graph)
        llm = llm if llm is not None else build_provider(settings)
        if llm is not None:
            llm = MeteredProvider(llm, ai_usage)
        nlq = NaturalLanguageQuery(catalog, llm, settings.ai.model_nlq)
        embedder = build_embedder(settings)
        semantic = SemanticIndex(catalog, embedder, build_vector_store(engine, embedder.dim))
        docgen = DocGenerator(catalog, llm, settings.ai.model_docgen)

    services = Services(settings, engine, graph, audit, identity, attributes, pdp, policy_repo, quality, workflows,
                        gateway, catalog, llm, nlq, semantic, docgen, ai_usage=ai_usage,
                        connections=connections)
    from cairn.common.licence import from_env as licence_from_env

    services.licence = licence_from_env(engine, audit)
    from cairn.extensions.service import ExtensionService

    services.extensions = ExtensionService(engine, attributes, audit)
    identity.extension_lookup = services.extensions.actor_for_token
    from cairn.identity.agents import AgentService

    def ai_system_state(sid: str) -> Optional[str]:
        try:
            return services.ai_systems.get(sid)["state"] if services.ai_systems is not None else None
        except Exception:  # noqa: BLE001 - a missing register entry means not approved
            return None

    registration = os.environ.get("CAIRN_OAUTH_REGISTRATION", "open")
    services.agents = AgentService(engine, audit, ai_system_state,
                                   registration if registration in ("open", "admin", "off") else "open")
    identity.agent_lookup = services.agents.resolve
    from cairn.ai.traces import TraceService

    services.traces = TraceService(engine, audit)
    from cairn.common.metrics import watch_licence

    watch_licence(services.licence)
    _build_active_metadata(services)
    return services


def _build_active_metadata(s: Services) -> None:
    """Personas & Purposes, notifications, collaboration, marketplace, context mining, embeds, assistant."""
    from cairn.ai.assistant import Assistant
    from cairn.collab.service import CollabService
    from cairn.context.miner import ContextStore
    from cairn.embed.service import EmbedService
    from cairn.governance.personas import PersonaEngine
    from cairn.marketplace.service import MarketplaceService
    from cairn.notify.channels import EmailSender, SlackSender, TeamsSender, WebhookSender
    from cairn.notify.hub import Event, NotificationHub, Route
    from cairn.notify.slack import SlackInteractions

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
    hub.channels["webhook"] = WebhookSender(signing_secret=s.extensions.signing_secret_for_route if s.extensions else None)
    if s.extensions is not None:
        hub.route_providers.append(s.extensions.routes)
    hub.channels["teams"] = TeamsSender()
    from cairn.notify.ticketing import TicketingService
    s.ticketing = TicketingService(s.engine, hub, lambda email: actor_for_email(s.graph, email), audit=s.audit)
    hub.channels["ticket"] = s.ticketing.sender
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
    from cairn.ai.embeddings import HashingEmbedder
    from cairn.context.layer import ContextLayer
    embedder = s.semantic.embedder if s.semantic is not None else HashingEmbedder()
    s.context_layer = ContextLayer(s.engine, lambda text: embedder.embed([text])[0], s.audit)
    s.context = ContextStore(s.engine, s.graph, s.collab, s.audit, hub, layer=s.context_layer)
    if s.graph is not None:
        s.marketplace = MarketplaceService(s.engine, s.graph, s.personas, hub, s.audit, s.attributes, s.trust,
                                           s.is_admin)
    _build_forms(s, hub, Event)
    from cairn.governance.ai_systems import AISystemService

    s.ai_systems = AISystemService(
        s.engine, s.attributes, s.personas, s.audit, hub,
        can_approve=lambda actor: s.authorize(Principal(urn=actor, via="delegated"), "MANAGE_POLICIES", None,
                                              raise_on_deny=False))
    if s.graph is not None:
        from cairn.governance.bulk import BulkService
        s.bulk = BulkService(s.engine, s.graph, s.audit)
        _build_contracts(s, hub, Event)
        _build_automations(s, hub, Event)
        from cairn.reports.service import ReportService
        s.reports = ReportService(s.engine, s.graph, s.quality.store, s.personas)
        _build_costs(s, hub, Event)
        _build_data_access(s)
        _build_steward(s)
        from cairn.governance.tag_sync import TagSyncService, sqlalchemy_executor
        from cairn.quality.auto import Planner
        s.tag_sync = TagSyncService(s.engine, lambda: Planner(s.graph).datasets(),
                                    sqlalchemy_executor(s.connections), audit=s.audit)
        from cairn.marketplace.publish import ProductPublisher
        s.publisher = ProductPublisher(s.engine, s.graph, s.contracts, s.audit, hub,
                                       sqlalchemy_executor(s.connections) if s.connections else None)
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
        from cairn.governance.certification import WorkflowError

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
            return actor_for_email(s.graph, email) if email else None

        def slack_search(actor: str, query: str) -> list[dict]:
            if s.catalog is None:
                return []
            check = s.can_view_as(actor)
            return [r for r in s.catalog.search(query, None, None, 20) if check(r["urn"])]

        def slack_ask(actor: str, question: str) -> str:
            out = s.assistant.chat(Principal(urn=actor, via="delegated"), question)
            return out["reply_md"]

        s.slack = SlackInteractions(hub, cfg.slack_signing_secret, resolve_slack_actor, slack_search, slack_ask)
        if slack_sender is not None and s.assistant is not None:
            from cairn.notify.chatops import ChatAnswerer, SlackEvents

            s.slack_events = SlackEvents(cfg.slack_signing_secret, slack_sender,
                                         lambda u: hub.actor_for_slack_user(u) or resolve_slack_actor(u),
                                         _chat_answerer(s, ChatAnswerer), os.environ.get("CAIRN_SLACK_CHANNEL_ACTOR"))
    if os.environ.get("CAIRN_TEAMS_OUTGOING_SECRET") and s.assistant is not None:
        from cairn.notify.chatops import ChatAnswerer, TeamsOutgoing

        def teams_person(sender: dict) -> Optional[str]:
            from sqlalchemy import select

            from cairn.identity.scim import scim_resources

            oid = sender.get("aadObjectId")
            if not oid:
                return None
            with s.engine.connect() as conn:          # SCIM from Entra ID keeps the object id as externalId
                rid = conn.execute(select(scim_resources.c.id).where(
                    scim_resources.c.resource_type == "User", scim_resources.c.external_id == oid)).scalar()
            return f"urn:li:corpuser:{rid}" if rid else None

        s.teams_bot = TeamsOutgoing(os.environ["CAIRN_TEAMS_OUTGOING_SECRET"],
                                    os.environ.get("CAIRN_TEAMS_CHANNEL_ACTOR"), _chat_answerer(s, ChatAnswerer),
                                    os.environ.get("CAIRN_TEAMS_REPLY_WEBHOOK"), teams_person)


def _chat_answerer(s: Services, cls):
    def chat(actor: str, question: str, conversation: Optional[str]) -> dict:
        return s.assistant.chat(Principal(urn=actor, via="delegated"), question, conversation)

    return cls(chat, s.settings.active.public_url, s.audit)


_USERS_BY_EMAIL = """query($i: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $i) {
  searchResults { entity { urn ... on CorpUser { properties { email } info { email } } } } } }"""


def actor_for_email(graph: Optional[MetadataGraph], email: str) -> Optional[str]:
    """The Cairn Core user whose profile has this email (usernames need not be emails). Falls back
    to a user whose username is the email; None when nobody matches or the match is ambiguous."""
    email = email.strip().lower()
    if graph is None or not email:
        return None
    try:
        data = graph.graphql(_USERS_BY_EMAIL, {"i": {"types": ["CORP_USER"], "query": f'"{email}"', "start": 0,
                                                     "count": 10}})
        hits = {r["entity"]["urn"] for r in (data.get("searchAcrossEntities") or {}).get("searchResults") or []
                if email in {str(((r["entity"].get(k) or {}).get("email")) or "").lower()
                             for k in ("properties", "info")}}
        if len(hits) == 1:
            return hits.pop()
        if len(hits) > 1:
            logger.warning("%d Cairn Core users share the email %s; not guessing", len(hits), email)
            return None
    except Exception:
        logger.exception("user lookup by email failed")
    urn = f"urn:li:corpuser:{email}"
    return urn if graph.get_aspect(urn, "corpUserKey") or graph.get_aspect(urn, "corpUserInfo") else None


def _build_forms(s: Services, hub, Event) -> None:
    from cairn.governance.forms import FormService
    from cairn.quality.auto import Planner

    def notify(form_row, recipients: list[str], urns: list[str]) -> None:
        names = s.summaries(urns[:5])
        listing = ", ".join((names.get(u) or {}).get("name") or u for u in urns[:5])
        more = f" and {len(urns) - 5} more" if len(urns) > 5 else ""
        hub.publish(Event(type="form.assigned", title=f"Please complete “{form_row.name}”",
                          body=f"{len(urns)} asset(s) need your answers: {listing}{more}.",
                          recipients=recipients, url=f"{s.settings.active.public_url}/mfe/cairn-requests#/requests"))

    datasets = (lambda: Planner(s.graph).datasets()) if s.graph is not None else None
    s.forms = FormService(s.engine, s.graph, s.audit, notify, datasets)


def _build_steward(s: Services) -> None:
    from cairn.ai.steward import StewardService
    from cairn.collab.service import views

    def popularity(urn: str) -> float:
        from sqlalchemy import func, select
        with s.engine.connect() as conn:
            return float(conn.execute(select(func.count()).select_from(views).where(views.c.urn == urn)).scalar() or 0)

    def upstreams(urn: str) -> list[str]:
        try:
            return [u["urn"] for u in s.catalog.lineage(urn, "UPSTREAM", 1, 10)] if s.catalog else []
        except Exception:
            return []

    s.steward = StewardService(s.engine, s.graph, s.bulk, popularity=popularity, upstreams=upstreams,
                               docgen=s.docgen if s.llm is not None else None, audit=s.audit)


def _build_data_access(s: Services) -> None:
    from cairn.coreproxy.proxy import MaskingService
    from cairn.dataaccess.service import DataAccessService

    masking = MaskingService(s.pdp, s.attributes)

    def can_read(actor: str, urn: str) -> bool:
        p = Principal(urn=actor, via="delegated")
        return s.can_view(p)(urn) and s.authorize(p, "VIEW_DATASET_PROFILE", urn, raise_on_deny=False)

    def known(urn: str) -> bool:
        return s.graph is not None and any(s.graph.get_aspect(urn, a) for a in ("schemaMetadata", "datasetProperties"))

    s.data_access = DataAccessService(s.connections, masking.plan, can_read, s.audit, known=known)

    from cairn.dataaccess.ask import AskService

    def search(question: str, can_view, k: int) -> list[str]:
        if s.semantic is not None:
            hits = s.semantic.search(question, k, ["dataset"], can_view)
        elif s.catalog is not None:
            hits = [h for h in s.catalog.search(question, ["dataset"], count=k) if can_view(h["urn"])]
        else:
            return []
        return [h["urn"] for h in hits]

    s.ask = AskService(s.data_access, s.graph, s.llm, s.context_layer, search,
                       s.settings.ai.model_nlq or s.settings.ai.model, s.audit)
    if s.context is not None:
        from cairn.context.semantic import SemanticService

        def catalog_datasets() -> list[str]:
            from cairn.quality.auto import Planner

            return [u for u, _, _ in Planner(s.graph).datasets()] if s.graph is not None else []

        s.semantic_layer = SemanticService(s.context, s.data_access, catalog_datasets, s.audit)


def _build_costs(s: Services, hub, Event) -> None:
    from cairn.governance.pii import GraphLineage
    from cairn.reports.costs import CostService

    def has_downstream(urn: str) -> bool:
        try:
            return bool(GraphLineage(s.graph).downstream_datasets(urn))
        except Exception:  # unknown lineage: never call a table idle on a guess
            logger.warning("lineage lookup failed for %s", urn)
            return True

    def notify(urn: str, actor: str, reason: str, figures: dict) -> None:
        name = (s.summaries([urn]).get(urn) or {}).get("name") or urn
        hub.publish(Event(
            type="asset.deprecation_proposed", title=f"Deprecation proposed: {name}",
            body=(reason or "It looks unused.") + f" Last 30 days: {figures['reads']} reads, "
                 f"${figures['storage_usd'] + figures['compute_usd']:.2f} to store and query.",
            actor=actor, entity_urn=urn, include_owners=True, include_followers=True, payload=figures))

    s.costs = CostService(s.engine, s.graph, has_downstream, notify, registry=s.connections, audit=s.audit)


def _build_automations(s: Services, hub, Event) -> None:
    from cairn.governance.automations import AutomationService
    from cairn.quality.auto import Planner

    def notify(row: dict, recipients: list[str], urns: list[str]) -> None:
        names = s.summaries(urns[:5])
        listing = ", ".join((names.get(u) or {}).get("name") or u for u in urns[:5])
        more = f" and {len(urns) - 5} more" if len(urns) > 5 else ""
        hub.publish(Event(type="automation.applied", title=f"Automation “{row['name']}” updated {len(urns)} asset(s)",
                          body=f"{listing}{more}.", actor=row["identity"], recipients=recipients,
                          entity_urn=urns[0] if len(urns) == 1 else None, payload={"automation_id": row["id"]}))

    def dataset(urn: str):
        from cairn.governance.attributes import resource_from_aspects
        from cairn.quality.auto import _SCROLL_ASPECTS

        if s.graph is None:
            return None
        aspects = {a: s.graph.get_aspect(urn, a) for a in _SCROLL_ASPECTS + ["datasetProperties",
                                                                             "editableDatasetProperties"]}
        if not aspects.get("schemaMetadata") and not aspects.get("datasetProperties"):
            return None
        aspects = {k: v for k, v in aspects.items() if v}
        return resource_from_aspects(urn, aspects), aspects

    def steward(urns: list[str], actor: str) -> dict:
        if s.steward is None:
            return {"proposed": 0}
        return s.steward.run(actor=actor, urns=urns, limit=len(urns), max_drafts=min(len(urns), 5))

    s.automations = AutomationService(s.engine, s.bulk, lambda: Planner(s.graph).datasets(), notify, s.audit,
                                      dataset=dataset, steward=steward)

    from cairn.governance.automations import CAIRN_EVENTS

    def on_event(e) -> None:
        dataset = e.entity_urn and e.entity_urn.startswith("urn:li:dataset:")
        product = e.type == "access.requested" and e.entity_urn and e.entity_urn.startswith("urn:li:dataProduct:")
        if e.type in CAIRN_EVENTS and (dataset or product):
            s.automations.events.enqueue(e.type, e.entity_urn, {"actor": e.actor or "", **(e.payload or {})})

    hub.listeners.append(on_event)
    from cairn.ai.playbooks import PlaybookService

    s.playbooks = PlaybookService(s.engine, s, s.audit)
    s.automations.listeners.append(s.playbooks.handle)


def _build_contracts(s: Services, hub, Event) -> None:
    from cairn.quality.contracts import ContractService

    def notify(kind: str, row: dict, old: str, new: str) -> None:
        dataset = row["dataset_urn"]
        subscribers: list[str] = []
        if s.marketplace is not None:
            for product in s.graph.scroll_entities("dataProduct", ["dataProductProperties"]):
                if any(a.get("destinationUrn") == dataset
                       for a in (product.get("dataProductProperties") or {}).get("assets") or []):
                    subscribers += s.marketplace.subscribers(product["urn"])
        name = (s.summaries([dataset]).get(dataset) or {}).get("name") or dataset
        parts = [p for p in (s.contracts.get(dataset) or {}).get("status_detail", {}).get("parts", [])
                 if p["status"] == "BROKEN"]
        hub.publish(Event(
            type=f"contract.{kind}",
            title=f"Contract {'broken' if kind == 'broken' else 'met again'}: {name} v{row['version']}",
            body="; ".join(f"{p['part']}: {p['detail']}" for p in parts) or "All parts of the contract hold.",
            actor="urn:li:corpuser:cairn-quality", entity_urn=dataset, recipients=sorted(set(subscribers)),
            include_owners=True, include_followers=True, payload={"version": row["version"], "from": old, "to": new}))

    s.contracts = ContractService(s.engine, s.graph, s.quality, notify, s.audit)


def _hook_workflow_events(s: Services, Event) -> None:
    """Certification requests / decisions → notifications with inline Approve / Reject."""
    from cairn.notify.hub import Action

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
                body=ev.message or "", actor="urn:li:corpuser:cairn-quality", entity_urn=monitor.dataset_urn,
                include_followers=True, include_owners=True,
                payload={"monitor_id": monitor.id, "severity": monitor.severity}))
        except Exception:
            logger.exception("quality notification failed")
        if s.contracts is not None and monitor.config.get("contract"):
            try:
                s.contracts.refresh(monitor.config["contract"])
            except Exception:
                logger.exception("contract refresh failed")

    s.quality.notifier = notifier
