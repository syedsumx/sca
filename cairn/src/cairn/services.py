"""Service container: builds every Cairn component from :class:`Settings`.

All entry points (HTTP apps, scheduler, MCP server, actions) obtain their
dependencies here, so wiring lives in exactly one place and tests can inject fakes.
"""

from __future__ import annotations

import logging
import os
import threading
import time
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
    ai_workspace: Optional[Any] = None
    checked_queries: Optional[Any] = None
    databases: Optional[Any] = None
    pg_instances: Optional[Any] = None     # managed PostgreSQL instances (cairn.databases.instances)
    pg_backups: Optional[Any] = None       # the same, when their backups have a store (CAIRN_PG_BACKUP_STORE)
    mirrors: Optional[Any] = None
    app_packages: Optional[Any] = None
    finetunes: Optional[Any] = None
    ai_usage: Optional[Any] = None
    forms: Optional[Any] = None
    bulk: Optional[Any] = None
    tag_sync: Optional[Any] = None
    connections: Optional[Any] = None       # named warehouse connections (quality, tag sync)
    contracts: Optional[Any] = None
    automations: Optional[Any] = None
    report_schedules: Optional[Any] = None
    saml: Optional[Any] = None
    secret_scopes: Optional[Any] = None
    volumes: Optional[Any] = None
    storage_locations: Optional[Any] = None   # storage credentials and external locations (the customer's storage)
    saas: Optional[Any] = None
    reverse_syncs: Optional[Any] = None
    ml_serving: Optional[Any] = None
    custom_agents: Optional[Any] = None
    sandboxes: Optional[Any] = None
    sandbox_proxy: Optional[Any] = None
    trace_insights: Optional[Any] = None
    intents: Optional[Any] = None
    consent: Optional[Any] = None
    bi_sharing: Optional[Any] = None
    records: Optional[Any] = None
    limits: Optional[Any] = None
    bi_catalog: Optional[Any] = None
    bi_versions: Optional[Any] = None
    embedded_dashboards: Optional[Any] = None
    bi_collab: Optional[Any] = None
    metric_alerts: Optional[Any] = None
    jobs: Optional[Any] = None
    costs: Optional[Any] = None
    data_access: Optional[Any] = None
    sql_workspace: Optional[Any] = None
    sql_boards: Optional[Any] = None
    maintenance: Optional[Any] = None
    pipes: Optional[Any] = None
    streams: Optional[Any] = None
    derived: Optional[Any] = None
    bundle_state: Optional[Any] = None
    sql_ai: Optional[Any] = None
    ai_gateway: Optional[Any] = None
    agent_apps: Optional[Any] = None
    agent_memory: Optional[Any] = None
    abuse: Optional[Any] = None
    external_catalogs: Optional[Any] = None
    listings: Optional[Any] = None
    ask: Optional[Any] = None
    ontology: Optional[Any] = None          # Cairn Ontology, the ontology compiler (cairn.context.ontology_compiler)
    semantic_layer: Optional[Any] = None
    addins: Optional[Any] = None
    profiles: Optional[Any] = None
    ui_settings: Optional[Any] = None       # workspace UI settings (onboarding on or off)
    sheets: Optional[Any] = None
    ticketing: Optional[Any] = None
    steward: Optional[Any] = None
    context_layer: Optional[Any] = None
    licence: Optional[Any] = None
    extensions: Optional[Any] = None
    ai_systems: Optional[Any] = None
    agents: Optional[Any] = None
    device_logins: Optional[Any] = None          # cairn.identity.device: command-line sign-in
    desktop_policy: Optional[Any] = None         # cairn.identity.desktop: what Cairn Desktop may do here
    playbooks: Optional[Any] = None
    sources: Optional[Any] = None
    openlineage: Optional[Any] = None
    git: Optional[Any] = None
    external_quality: Optional[Any] = None
    dbt_runs: Optional[Any] = None
    lakehouse: Optional[Any] = None
    lakehouse_sessions: Optional[Any] = None
    shares: Optional[Any] = None
    share_packages: Optional[Any] = None
    ml: Optional[Any] = None
    notebooks: Optional[Any] = None
    query_history: Optional[Any] = None
    system_tables: Optional[Any] = None
    trino_functions: Optional[Any] = None
    change_feed: Optional[Any] = None
    vectors: Optional[Any] = None
    sql_search: Optional[Any] = None
    documents: Optional[Any] = None
    parquet_tables: Optional[Any] = None
    delta_convert: Optional[Any] = None
    git_folders: Optional[Any] = None
    procedures: Optional[Any] = None
    data_apps: Optional[Any] = None
    app_chat: Optional[Any] = None
    clean_rooms: Optional[Any] = None
    feature_tables: Optional[Any] = None
    online_features: Optional[Any] = None
    serving_tables: Optional[Any] = None
    ml_monitoring: Optional[Any] = None
    budgets: Optional[Any] = None
    siem: Optional[Any] = None
    signup: Optional[Any] = None
    metering: Optional[Any] = None
    usage_reporter: Optional[Any] = None
    status_page: Optional[Any] = None
    workbooks: Optional[Any] = None
    input_tables: Optional[Any] = None
    metrics: Optional[Any] = None
    autopilot: Optional[Any] = None
    pipeline_agent: Optional[Any] = None
    cowork: Optional[Any] = None
    doc_sources: Optional[Any] = None
    builder: Optional[Any] = None
    bi_actions: Optional[Any] = None
    reports: Optional[Any] = None
    sql_workflows: Optional[Any] = None
    flows: Optional[Any] = None
    notebook_catalog: Optional[Any] = None
    workload: Optional[Any] = None
    dataset_changes: Optional[Any] = None
    pipeline_health: Optional[Any] = None
    knowledge: Optional[Any] = None
    privacy: Optional[Any] = None
    slos: Optional[Any] = None
    workspaces: Optional[Any] = None
    people: Optional[Any] = None            # People & roles (cairn.identity.people)
    observability: Optional[Any] = None     # Observability: health map, journeys, logs, SLOs, alerts, incidents
    sre: Optional[Any] = None               # the SRE agent (cairn.sre): investigates, proposes; people approve
    pipeline_obs: Optional[Any] = None      # pipeline observability, data products, SLAs (L145)
    ops_tiles: Optional[Any] = None         # ops dashboards: PromQL, LogQL, TraceQL, SLO, alert, PostgreSQL tiles
    airflow_deployments: Optional[Any] = None   # managed Airflow (cairn.airflow_deploy): deployments Cairn runs
    store_admin: Optional[Any] = None
    # Trino access control (the OPA plugin's requests), with the key Trino servers must present.
    trino_access: Optional[Any] = None
    trino_key: str = ""
    trino_tokens: Optional[Any] = None
    dataplanes: Optional[Any] = None        # split control and data plane (cairn.dataplane)
    egress: Optional[Any] = None            # egress rules for user code (cairn.network.egress)
    trino_outbox: Optional[Any] = None
    trino_usage: Optional[Any] = None
    trino_catalog: Optional[Any] = None
    trino_oauth: Optional[Any] = None
    history: Optional[Any] = None
    history_consumer: Any = None
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
                    if self.workspaces is not None:
                        self.workspaces.reload()
                    if self.storage_locations is not None:
                        self.storage_locations.reload()
                    latest = self.policy_repo.latest()
                    if latest is not None and latest != self.pdp.bundle:
                        self.pdp.set_bundle(latest)
                except Exception:
                    logger.exception("policy layer reload failed")

        def trino_lineage_loop() -> None:
            every = float(os.environ.get("CAIRN_TRINO_LINEAGE_RETRY_SECONDS", "15"))
            while not stop.wait(every):
                try:
                    if self.openlineage is not None:
                        self.trino_outbox.drain()
                    if self.trino_usage is not None:
                        self.trino_usage.flush()
                        self.trino_usage.prune()
                    sync_every = float(os.environ.get("CAIRN_TRINO_CATALOG_SYNC_SECONDS", "3600"))
                    if self.trino_catalog is not None and sync_every > 0:
                        self.trino_catalog.run_due(sync_every)
                    if self.shares is not None:              # share usage written; expiry and unused notices
                        self.shares.flush_usage()
                        self.shares.sweep(every_s=float(os.environ.get("CAIRN_SHARES_NOTICE_SECONDS", "3600")))
                    if self.external_catalogs is not None:   # back in Trino after a restart; catalogued again
                        self.external_catalogs.run_due(
                            float(os.environ.get("CAIRN_EXTERNAL_CATALOG_SYNC_SECONDS", "3600")),
                            float(os.environ.get("CAIRN_EXTERNAL_CATALOGS_RECONCILE_SECONDS", "60")))
                except Exception:
                    logger.exception("held Trino lineage or usage could not be retried")

        targets = [policy_loop, workflow_loop, layers_loop]
        if self.trino_outbox is not None:
            targets.append(trino_lineage_loop)
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
        if (self.history is not None and self.settings.kafka_bootstrap
                and os.environ.get("CAIRN_CHANGE_HISTORY", "true").lower() == "true"):
            from cairn.governance.invalidation import start_consumer

            # One shared group: each change is recorded once, whichever replica reads it.
            t = start_consumer(self.history, self.settings.kafka_bootstrap, self.settings.schema_registry_url, stop,
                               group="cairn-change-history", offset="earliest", name="change_history")
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
    _build_device_logins(services)
    from cairn.identity.desktop import DesktopPolicy

    services.desktop_policy = DesktopPolicy(engine, audit)
    # The spreadsheet add-ins (Excel, Google Sheets): OAuth clients from the operator's configuration.
    from cairn.identity.addins import SpreadsheetAddins

    services.addins = SpreadsheetAddins.from_env(services.agents)
    services.addins.sync()
    from cairn.identity.intents import IntentGuard

    def dataset_domains(urn: str) -> list[str]:
        return [str(d) for d in ((services.graph.get_aspect(urn, "domains") or {}).get("domains") or [])] \
            if services.graph is not None else []
    services.intents = IntentGuard(services.agents, audit, dataset_domains, lambda actor, privilege: services.authorize(
        Principal(urn=actor, via="delegated"), privilege, None, raise_on_deny=False))
    from cairn.ai.traces import TraceService

    services.traces = TraceService(engine, audit)
    from cairn.common.metrics import watch_licence

    watch_licence(services.licence)
    _build_active_metadata(services)
    _build_pipeline_health(services)
    _build_medallion(services)
    _build_flows(services)
    _build_split_compute(services)
    _build_people(services)
    _build_observability(services)
    _build_pipeline_observability(services)
    _build_ops_dashboards(services)
    _build_iac_changes(services)
    _build_sre(services)
    _build_airflow_deployments(services)
    return services


def _build_airflow_deployments(s: Services) -> None:
    """Managed Airflow (L144, cairn.airflow_deploy): deployments on Docker or Kubernetes (CAIRN_AIRFLOW_RUNTIME), their
    DAG bundles in Cairn Store (CAIRN_AIRFLOW_BUNDLE_STORE), sign-in and workload identity through Cairn. Built after
    Trino's sign-in (its OAuth server and tokens), secret scopes and git folders. A misconfiguration turns it off,
    loudly; a deployment never starts without sign-in, its identity and its secrets."""
    kind = os.environ.get("CAIRN_AIRFLOW_RUNTIME", "").strip()
    if not kind:
        return
    import json

    from cairn.airflow_deploy.deployments import AirflowDeployments, DeployStore
    from cairn.airflow_deploy.identity import ISSUER_NAME, Keys

    def allowed(actor: str, privilege: str, urn: Optional[str]) -> bool:
        return s.authorize(Principal(urn=actor, via="delegated"), privilege, urn, raise_on_deny=False)

    try:
        keys = Keys(_secret_env("CAIRN_AIRFLOW_DEPLOY_KEY").encode())
        cairn_url = os.environ.get("CAIRN_AIRFLOW_CAIRN_URL", "").strip()
        if not cairn_url:
            raise ValueError("CAIRN_AIRFLOW_CAIRN_URL: Cairn's API as the deployments reach it")
        store = None
        raw = os.environ.get("CAIRN_AIRFLOW_BUNDLE_STORE", "")
        if raw:
            cfg = json.loads(raw)
            ak, sk = _secret_env("CAIRN_AIRFLOW_BUNDLE_ACCESS_KEY"), _secret_env("CAIRN_AIRFLOW_BUNDLE_SECRET_KEY")
            if not (cfg.get("endpoint") and cfg.get("bucket") and ak and sk):
                raise ValueError("CAIRN_AIRFLOW_BUNDLE_STORE needs endpoint and bucket, and "
                                 "CAIRN_AIRFLOW_BUNDLE_ACCESS_KEY(_FILE) and CAIRN_AIRFLOW_BUNDLE_SECRET_KEY(_FILE)")
            store = DeployStore(cfg["endpoint"], cfg["bucket"], ak, sk, cfg.get("region") or "us-east-1")
        if kind == "docker":
            from cairn.airflow_deploy.docker_runtime import DockerRuntime

            images = os.environ.get("CAIRN_AIRFLOW_DOCKER_IMAGES", "")
            hosts = os.environ.get("CAIRN_AIRFLOW_DOCKER_EXTRA_HOSTS", "")
            lo, _, hi = os.environ.get("CAIRN_AIRFLOW_DOCKER_PORTS", "18080-18999").partition("-")
            runtime = DockerRuntime(os.environ.get("CAIRN_AIRFLOW_DOCKER_SOCKET", "/var/run/docker.sock"),
                                    os.environ.get("CAIRN_AIRFLOW_DOCKER_BIND", "127.0.0.1"),
                                    os.environ.get("CAIRN_AIRFLOW_DOCKER_HOST", "127.0.0.1"),
                                    json.loads(images) if images else None,
                                    os.environ.get("CAIRN_AIRFLOW_DOCKER_DB_IMAGE", "postgres:16-alpine"),
                                    (int(lo), int(hi or lo)),
                                    extra_hosts=json.loads(hosts) if hosts else None)
        elif kind == "kubernetes":
            from cairn.airflow_deploy.k8s_runtime import KubernetesRuntime

            runtime = KubernetesRuntime.from_env()
        else:
            raise ValueError(f"CAIRN_AIRFLOW_RUNTIME is docker or kubernetes, not {kind!r}")
        trino = json.loads(os.environ.get("CAIRN_AIRFLOW_TRINO", "") or "{}")
        ca_file = os.environ.get("CAIRN_AIRFLOW_CA_FILE", "")
        ca_pem = open(ca_file, "rb").read() if ca_file else b""
    except Exception as e:  # noqa: BLE001 - a misconfiguration: managed Airflow is off, said loudly
        logger.error("managed Airflow is off: %s", e)
        return
    oauth = getattr(s, "trino_oauth", None)
    tokens = getattr(s, "trino_tokens", None)
    if oauth is None and tokens is not None:
        from cairn.dataaccess.trino_oauth import TrinoOAuth

        oauth = s.trino_oauth = TrinoOAuth(s.engine, tokens, "", [],
                                           int(os.environ.get("CAIRN_TRINO_TOKEN_TTL", "3600")))
    workload = getattr(s, "workload", None)
    if tokens is not None:
        if workload is None:
            from cairn.identity.workload import WorkloadIdentity

            g = s.graph
            workload = s.workload = WorkloadIdentity(
                {}, s.engine, tokens, s.audit,
                (lambda urn: (g.get_aspect(urn, "corpUserStatus") or {}).get("status", "ACTIVE")) if g is not None else None)
            if s.identity is not None:
                s.identity.workload_lookup = workload.principal_of
        if kind == "docker":
            workload.issuers[ISSUER_NAME] = keys.issuer()
    else:
        logger.warning("managed Airflow: Trino sign-in is off (CAIRN_TRINO_ACCESS_KEY), so no deployment can be made "
                       "(people sign in, and tasks get their identity, through it)")
    s.airflow_deployments = AirflowDeployments(
        s.engine, allowed, s.audit, keys, runtime, store, cairn_url,
        os.environ.get("CAIRN_AIRFLOW_PUBLIC_URL", "") or s.settings.active.public_url, trino, ca_pem,
        getattr(s, "secret_scopes", None), oauth, workload, os.environ.get("CAIRN_AIRFLOW_K8S_WORKLOAD_ISSUER", ""),
        getattr(s, "git_folders", None), os.environ.get("CAIRN_AIRFLOW_SELF_APPROVE", "").lower() == "true")
    if getattr(s, "flows", None) is not None:      # a flow's airflow step may name a managed deployment
        s.flows.b.airflow_deployments = s.airflow_deployments
    pobs = getattr(s, "pipeline_obs", None)         # pipeline observability (L145) reads each deployment's Airflow
    if pobs is not None and hasattr(pobs, "airflow_sources"):
        pobs.airflow_sources.append(s.airflow_deployments.observability_sources)


def _build_split_compute(s: Services) -> None:
    """Egress rules for user code (cairn.network.egress) and data planes in customers' clusters (cairn.dataplane):
    jobs, notebooks and flows' steps run where their owner's workspace computes, contained by the egress rules."""
    from cairn.agent.gateway import GatewayKeys
    from cairn.dataplane.service import ComputeRouter, DataPlanes
    from cairn.network.egress import Egress

    def workspaces_of(actor: str) -> list[str]:
        ws = getattr(s, "workspaces", None)
        return [w["id"] for w in ws.mine(actor)] if ws is not None else []

    s.egress = Egress(s.engine, s.audit, workspaces_of)
    keys = s.gateway.keys if s.gateway is not None else GatewayKeys(s.settings.gateway.key_dir)
    s.dataplanes = DataPlanes(s.engine, keys, s.audit, getattr(s, "trino_access", None), workspaces_of)
    jobs = getattr(s, "jobs", None)
    if jobs is not None:
        jobs.egress = s.egress
        # Routed per workspace when there is a managed pool to route from, or when jobs run only on data planes.
        if jobs.runner is not None or not s.dataplanes.managed:
            jobs.runner = ComputeRouter(jobs.runner, s.dataplanes, s.egress)
    if getattr(s, "sandboxes", None) is not None:
        s.sandboxes.egress = s.egress
    if getattr(s, "flows", None) is not None:              # flows' platform steps: through the egress proxy when enforced
        s.flows.b.egress = s.egress
    apps = getattr(s, "agent_apps", None)
    if apps is not None:
        apps.egress = s.egress
        if hasattr(apps.runtime, "ingress_from"):          # the Kubernetes runtime: its pods get the egress policy
            apps.runtime.egress = s.egress


def _build_device_logins(s: Services) -> None:
    """Command-line sign-in (cairn.identity.device): the device flow and the access tokens it hands out; a suspended
    person's devices cannot approve or refresh."""
    from cairn.identity.device import DeviceLogins

    graph = s.graph

    def why_not(actor: str) -> Optional[str]:
        if not actor.startswith("urn:li:corpuser:"):
            return f"{actor} is not a person"
        if graph is not None:
            try:
                status = (graph.get_aspect(actor, "corpUserStatus") or {}).get("status", "ACTIVE")
            except Exception:  # noqa: BLE001 - fail closed: an unknown state is not a yes
                return "the account's state cannot be read now; try again"
            if str(status).upper() not in ("ACTIVE",):
                return f"the account is {str(status).lower()}"
        return None
    verify = os.environ.get("CAIRN_DEVICE_VERIFICATION_URL", "").strip() or \
        f"{s.settings.active.public_url.rstrip('/')}/cairn/device"
    s.device_logins = DeviceLogins(s.engine, s.audit, verify, why_not)
    s.identity.cli_lookup = s.device_logins.resolve


def provisioning_graph(s: Services):
    """Cairn Core as cairn-provisioner: what writes people and groups (SCIM, People & roles), never the control-plane
    account (which must not be able to change group membership, and so its own access). Its token sits next to the
    control plane's."""
    core = s.settings.core
    path = os.environ.get("CAIRN_SCIM_CORE_TOKEN_FILE") or (
        os.path.join(os.path.dirname(core.token_file), "provisioner_token") if core.token_file else None)
    if not path or not core.url:
        return s.graph                   # tests and single-process demos
    return CoreClient(core.url, None, core.timeout_seconds, token_file=path)


def _build_people(s: Services) -> None:
    """People & roles (cairn.identity.people): built after everything it reports on (personas, workspaces, agents,
    access reviews, command-line sessions)."""
    from cairn.identity.people import PeopleDirectory

    devices = getattr(s, "device_logins", None)
    s.people = PeopleDirectory(
        s.engine, s.graph, provisioning_graph(s) if s.graph is not None else None, s.graph, s.audit, s.attributes,
        s.pdp, s.personas, s.workspaces, s.agents, getattr(s, "access_reviews", None),
        on_deactivate=(lambda urn: devices.end_all(urn, "people")) if devices is not None else None)


def _build_observability(s: Services) -> None:
    """Observability (cairn.observability): reads Prometheus, Loki and Tempo (CAIRN_OBS_*), keeps SLOs, alert rules,
    silences, alerts and incidents, and tells the notification routes. Built after notifications."""
    from cairn import __version__
    from cairn.observability.backends import Backends
    from cairn.observability.service import ObservabilityService

    hub = getattr(s, "notify", None)
    s.observability = ObservabilityService(s.engine, Backends(), hub, s.audit,
                                           public_url=getattr(hub, "public_url", "") or "", version=__version__)


def _build_pipeline_observability(s: Services) -> None:
    """Pipeline observability (L145, cairn.observability.pipelines): runs of Airflow DAGs (OpenLineage and Airflow's
    API), flows, dbt and jobs; data products, SLAs, impact, quality; each run as OpenTelemetry. Built after
    observability, flows, dbt, jobs, OpenLineage and the BI catalog."""
    if getattr(s, "observability", None) is None:
        return
    from cairn.flows.steps import airflow_from_env
    from cairn.observability.pipelines import PipelineObservability

    lk = getattr(s, "lakehouse", None)

    def resolve(name: str) -> str:
        ns, _, t = name.rpartition(".")
        if lk is not None and ns:
            return lk.urn(ns.split("."), t)
        return f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{name},PROD)"

    flows = getattr(s, "flows", None)
    airflow = getattr(getattr(flows, "b", None), "airflow", None) or airflow_from_env()
    s.pipeline_obs = PipelineObservability(s.engine, s.observability, s.graph, s.audit, getattr(s, "bi_catalog", None),
                                           flows, airflow, resolve)
    if flows is not None:
        flows.observers.append(s.pipeline_obs.on_flow_run)
    if getattr(s, "dbt_runs", None) is not None:
        s.dbt_runs.observers.append(s.pipeline_obs.on_dbt_run)
    if getattr(s, "jobs", None) is not None:
        s.jobs.observers.append(s.pipeline_obs.on_job_run)
    if getattr(s, "openlineage", None) is not None:
        s.openlineage.observers.append(s.pipeline_obs.on_openlineage)


def _build_ops_dashboards(s: Services) -> None:
    """Ops dashboards (L143, cairn.observability.dashboards): PromQL, LogQL, TraceQL, SLO, alert and PostgreSQL tiles
    in the dashboards (sql_boards), run as the viewer (VIEW_OPERATIONS or MANAGE_SYSTEM_OPERATIONS), and the built-in
    ops dashboards seeded as code. Built after observability and the dashboards."""
    if getattr(s, "observability", None) is None or getattr(s, "sql_boards", None) is None:
        return
    from cairn.observability import builtin
    from cairn.observability.dashboards import OpsTiles

    def decide(actor: str, privilege: str) -> bool:
        return bool(s.authorize(Principal(urn=actor, via="delegated"), privilege, None, raise_on_deny=False))

    s.ops_tiles = OpsTiles(s.observability, decide, pg=lambda: getattr(s, "pg_instances", None))
    s.sql_boards.ops = s.ops_tiles
    if os.environ.get("CAIRN_OPS_BUILTIN_DASHBOARDS", "true").lower() not in ("0", "false", "no"):
        try:
            builtin.seed(s.sql_boards)
        except Exception:  # noqa: BLE001 - a dashboard that cannot be seeded never stops Cairn starting
            logger.exception("the built-in ops dashboards could not be seeded")


def _build_iac_changes(s: Services) -> None:
    """Infrastructure-as-code change requests (cairn.iac.changes) and Ask an agent (cairn.iac.assistant): planned and
    applied through Cairn's own API as a person, so built after everything that API serves."""
    from cairn.iac.assistant import IacAssistant
    from cairn.iac.changes import ChangeRequests

    hub = s.notify

    def tell(type_, title, body, recipients, payload):
        if hub is None:
            return
        from cairn.notify.hub import Event

        hub.publish(Event(type=type_, title=title, body=body, recipients=list(recipients), payload=payload,
                          url=f"{hub.public_url}/cairn/settings/iac?change={payload.get('change', '')}"))

    def authz(principal, privilege, urn):
        return s.authorize(principal, privilege, urn, raise_on_deny=False)

    def app():
        if getattr(s, "api_app", None) is None:      # the MCP server's process: the API in-process, as in Cairn's
            from cairn.app import create_api_app

            create_api_app(s)
        return s.api_app

    s.iac_changes = ChangeRequests(s.engine, s.audit, authz, s.is_admin, tell, getattr(s, "git_folders", None), app)
    s.iac_assistant = IacAssistant(s.iac_changes, lambda: getattr(s, "ai_gateway", None) is not None)


def _build_sre(s: Services) -> None:
    """The SRE agent (cairn.sre): acts through Cairn's own API with its agent credential, proposes change requests
    and runbook actions; built after observability, the agent hub, databases and change requests."""
    if getattr(s, "observability", None) is None or getattr(s, "agents", None) is None:
        return
    from cairn.sre.agent import SreAgent

    def authz(principal, privilege, urn):
        return s.authorize(principal, privilege, urn, raise_on_deny=False)

    def app():
        if getattr(s, "api_app", None) is None:
            from cairn.app import create_api_app

            create_api_app(s)
        return s.api_app

    s.sre = SreAgent(s.engine, s.observability, s.agents, s.audit, authz, app, getattr(s, "databases", None),
                     lambda: getattr(s, "ai_gateway", None) is not None)


def _build_medallion(s: Services) -> None:
    """Medallion layers on the tables pipes, streams and derived tables write; gold is checked against the catalog's
    owner and description and the table's contract or quality monitors (built after all of those)."""
    from cairn.pipelines.medallion import Medallion

    contracts = getattr(s, "contracts", None)
    s.medallion = Medallion(s.engine, s.graph, s.audit,
                            (lambda urn: (contracts.get(urn) or {}).get("status")) if contracts is not None else None,
                            s.quality)
    lk = getattr(s, "lakehouse", None)
    s.medallion_urn = (lambda ns, t: lk.urn([ns], t)) if lk is not None else \
        (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)")
    for name in ("pipes", "streams", "derived"):
        if getattr(s, name, None) is not None:
            getattr(s, name).medallion = s.medallion


def _build_flows(s: Services) -> None:
    """Cairn Flows (cairn.flows): DAGs of any steps, run as their owner; built last, on every service steps use."""
    from cairn.flows.engine import Flows
    from cairn.flows.steps import Backends

    def operator(actor: str) -> bool:
        return s.authorize(Principal(urn=actor, via="delegated"), "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False)

    hub = s.notify
    public = getattr(s.settings, "public_url", "") or ""

    def tell(type_, title, body, recipients, payload):
        if hub is None:
            return
        from cairn.notify.hub import Event

        hub.publish(Event(type=type_, title=title, body=body, recipients=list(recipients), payload=payload,
                          url=f"{hub.public_url}/cairn/flows?flow={payload.get('flow', '')}&run={payload.get('run', '')}"))

    def groups_of(actor: str) -> list[str]:
        return [str(g) for g in (s.attributes.subject(actor).get("groups") or [])]

    graph = s.graph

    def may_run(owner: str) -> Optional[str]:
        """Fail closed: a suspended (or unknown-state) owner's flows do not run."""
        if not owner.startswith("urn:li:corpuser:"):
            return f"{owner} is not a Cairn user"
        if graph is not None:
            status = (graph.get_aspect(owner, "corpUserStatus") or {}).get("status", "ACTIVE")
            if status not in ("ACTIVE", "active"):
                return f"{owner} is {str(status).lower()}"
        return None

    lake = getattr(s, "lakehouse", None)

    def table_urn(name: str) -> str:
        if name.startswith("urn:li:dataset:"):
            return name
        ns, _, t = name.rpartition(".")
        if lake is not None and ns:
            return lake.urn(ns.split("."), t)
        return f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{name},PROD)"

    ol = getattr(s, "openlineage", None)
    backends = Backends(workspace=getattr(s, "sql_workspace", None), derived=getattr(s, "derived", None),
                        jobs=getattr(s, "jobs", None), agent_apps=getattr(s, "agent_apps", None),
                        pipes=getattr(s, "pipes", None), streams=getattr(s, "streams", None), notify=tell,
                        is_operator=operator, lakehouse_sessions=getattr(s, "lakehouse_sessions", None), public_url=public,
                        secrets=getattr(s, "secret_scopes", None), egress=getattr(s, "egress", None))
    s.flows = Flows(s.engine, backends, s.audit, tell, s.is_admin, groups_of, getattr(s, "dataset_changes", None),
                    table_urn, (lambda event: ol.handle(event)) if ol is not None else None, may_run)
    # Airflow both ways (cairn.flows.airflow_interop): the DAG folder runner-airflow flows deploy to (a bad setting
    # stops Cairn at start), and Airflow's DAGs shown to the people they belong to.
    from cairn.flows.airflow_interop import AirflowDags, DagFolder

    s.flows.dag_folder = DagFolder.from_env(getattr(s, "git_folders", None))
    s.airflow_dags = AirflowDags(backends.airflow, groups_of, s.is_admin, s.flows)


def _build_pipeline_health(s: Services) -> None:
    """Dataset changes (for orchestrators) and incidents for failed pipelines, from OpenLineage and dbt."""
    from cairn.pipelines.changes import DatasetChanges, PipelineHealth

    s.dataset_changes = DatasetChanges(s.engine)
    if getattr(s, "pipes", None) is not None:
        s.pipes.changes = s.dataset_changes   # loaded tables are changes (data-aware schedules)
        if getattr(s, "lakehouse", None) is not None:
            s.pipes.urn = lambda ns, t: s.lakehouse.urn([ns], t)
    if getattr(s, "derived", None) is not None:
        s.derived.changes = s.dataset_changes  # refreshes follow upstream changes, and are changes
        if getattr(s, "lakehouse", None) is not None:
            s.derived.urn = lambda ns, t: s.lakehouse.urn([ns], t)
    if getattr(s, "streams", None) is not None:
        s.streams.changes = s.dataset_changes  # streamed tables are changes too
        if getattr(s, "lakehouse", None) is not None:
            s.streams.urn = lambda ns, t: s.lakehouse.urn([ns], t)
    if s.dbt_runs is not None:
        s.dbt_runs.changes = s.dataset_changes
        s.dbt_runs.engine = s.engine  # runs, state (Slim CI) and docs
        s.dbt_runs.semantic = getattr(s, "semantic_layer", None)
    if s.openlineage is not None and s.graph is not None:
        s.pipeline_health = PipelineHealth(s.graph, getattr(s, "notify", None), s.dataset_changes)
        s.openlineage.on_run = s.pipeline_health.on_run
    if s.openlineage is not None and getattr(s, "costs", None) is not None:
        s.openlineage.on_finished = s.costs.on_lineage_run  # Spark compute, priced per run


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
    from cairn.governance.workspaces import WorkspaceService

    s.workspaces = WorkspaceService(s.engine, s.pdp, s.graph, s.audit, s.attributes)
    # Cairn Store administration: on when the control plane is given the store's admin key.
    store_key = os.environ.get("CAIRN_STORE_ADMIN_ACCESS_KEY", "")
    store_secret = os.environ.get("CAIRN_STORE_ADMIN_SECRET_KEY", "")  # or CAIRN_STORE_ADMIN_SECRET_KEY_FILE
    if store_key and store_secret:
        from cairn.storage.store_admin import StoreAdmin

        s.store_admin = StoreAdmin(os.environ.get("CAIRN_STORE_URL", "http://cairn-store:8333"), store_key, store_secret,
                                   os.environ.get("CAIRN_STORE_REGION", "us-east-1"))
    from cairn.governance.history import ChangeHistory

    s.history = ChangeHistory(s.engine, s.graph, s.audit)

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
    from cairn.notify.streams import EventBridgeSender, KafkaSender, PubSubSender

    hub.channels["kafka"] = KafkaSender()
    hub.channels["eventbridge"] = EventBridgeSender()
    hub.channels["pubsub"] = PubSubSender()
    from cairn.notify.ticketing import TicketingService
    s.ticketing = TicketingService(s.engine, hub, lambda email: actor_for_email(s.graph, email), audit=s.audit)
    hub.channels["ticket"] = s.ticketing.sender
    def report_attachments(row: dict) -> list:
        """A report's copy, for its own recipient only (email attaches it, Slack uploads it to their DM)."""
        fid = (row.get("payload") or {}).get("file")
        rep = getattr(s, "report_schedules", None)
        got = rep.attachment(fid, row.get("recipient")) if (rep is not None and fid and row.get("type") == "report.ready") \
            else None
        return [got] if got else []
    slack_sender = None
    if cfg.slack_bot_token:
        slack_sender = SlackSender(cfg.slack_bot_token, email_of, attachments=report_attachments)
        hub.channels["slack"] = slack_sender
    if cfg.smtp_url:
        hub.channels["email"] = EmailSender(cfg.smtp_url, cfg.email_from, email_of, report_attachments)
    if cfg.teams_webhook_url and not hub.list_routes():
        hub.put_routes([Route(id="teams-default", channel="teams", target=cfg.teams_webhook_url,
                              event_types=["quality.incident.raised", "certification.requested", "asset.deprecated",
                                           "context.drift"])])
    s.notify = hub
    # Profiles (cairn.identity.profiles): what a person tells Cairn about themselves; web push to their devices.
    from cairn.identity.profiles import ProfileService, WebPush

    push = WebPush.from_env(s.engine, s.audit)
    if push.enabled:
        hub.channels["push"] = push

    def personas_of(actor: str) -> list[dict]:
        if s.personas is None:
            return []
        out = []
        for pid in s.subject_of(actor).get("personas") or []:
            p = s.personas.get_persona(pid)
            if p is not None and p.enabled:
                out.append({"id": p.id, "name": p.name})
        return out

    def profile_can_view(actor: str, urn: str) -> bool:
        return bool(s.can_view(Principal(urn=actor, via="delegated"))(urn))
    s.profiles = ProfileService(s.engine, hub, personas_of, profile_can_view, s.audit, push)
    # Workspace UI settings (onboarding on or off): changing them is decided by the policy engine.
    from cairn.identity.profiles import UiSettings

    s.ui_settings = UiSettings(s.engine, lambda actor, privilege: s.authorize(
        Principal(urn=actor, via="delegated"), privilege, None, raise_on_deny=False), s.audit)

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
        # Agents and skills listed beside data products (cairn.marketplace.listings); the agent and extension
        # services are built later, so they are looked up when a listing is published.
        from cairn.marketplace.listings import Listings

        def listing_owner(kind: str, name: str) -> Optional[tuple[str, str]]:
            if kind == "agent":
                apps, built = getattr(s, "agent_apps", None), getattr(s, "custom_agents", None)
                for source, svc in (("agent_app", apps), ("custom_agent", built)):
                    if svc is None:
                        continue
                    try:
                        row = svc._row(name) if source == "agent_app" else svc._get(name)
                    except Exception:  # noqa: BLE001 - not one of these
                        continue
                    return source, row["owner"]
                return None
            ext = getattr(s, "extensions", None)
            if ext is None:
                return None
            try:
                return "extension", ext.describe(name)["installed_by"]
            except Exception:  # noqa: BLE001
                return None
        s.listings = Listings(s.engine, s.marketplace, listing_owner,
                              lambda actor: s.authorize(Principal(urn=actor, via="delegated"), "MANAGE_SYSTEM_OPERATIONS",
                                                        None, raise_on_deny=False), s.audit)
        from cairn.marketplace.provisioning import Provisioner, load_targets

        try:
            targets = load_targets()
        except Exception:  # noqa: BLE001 - a bad file must not stop the API
            logger.exception("could not load config/provisioning")
            targets = {}
        if targets:
            def person(urn: str) -> dict:
                info = (s.graph.get_aspect(urn, "corpUserInfo") or {}) if s.graph is not None else {}
                return {"username": urn.split(":")[-1], "email": info.get("email")}

            s.marketplace.provisioner = Provisioner(targets, s.connections, person, s.audit)
    _build_forms(s, hub, Event)
    # Access reviews: owners recertify the access people hold (persona grants), fail closed at the due date.
    from cairn.governance.access_reviews import AccessReviews

    def tell_reviewers(type_, title, body, recipients, payload):
        from cairn.notify.hub import Event as _Event

        if s.notify is not None:
            s.notify.publish(_Event(type=type_, title=title, body=body, recipients=recipients, payload=payload,
                                    url=f"/governance/access-reviews/{payload.get('review', '')}"))
    market = getattr(s, "marketplace", None)
    s.access_reviews = AccessReviews(s.engine, s.personas,
                                     market.request_owners if market is not None else (lambda rid: []),
                                     s.audit, tell_reviewers)
    from cairn.governance.ai_systems import AISystemService

    s.ai_systems = AISystemService(
        s.engine, s.attributes, s.personas, s.audit, hub,
        can_approve=lambda actor: s.authorize(Principal(urn=actor, via="delegated"), "MANAGE_POLICIES", None,
                                              raise_on_deny=False),
        observed=lambda sid: s.traces.datasets_read(agents=[a["id"] for a in s.agents.list() if a["ai_system"] == sid])
        if s.traces is not None and s.agents is not None else [])
    if s.graph is not None:
        from cairn.sources.service import SourceService

        s.sources = SourceService(s.engine, s.graph, s.gateway, s.audit, hub)
        from cairn.context.knowledge import KnowledgeService
        from cairn.context.knowledge import load_sources as load_knowledge

        try:
            knowledge_sources = load_knowledge()
        except Exception:  # noqa: BLE001 - a bad file must not stop the API
            logger.exception("could not load config/knowledge")
            knowledge_sources = {}

        def known_datasets() -> list[str]:
            from cairn.quality.auto import Planner

            return [u for u, _, _ in Planner(s.graph).datasets()]

        s.knowledge = KnowledgeService(s.engine, knowledge_sources, lambda text: embedder.embed([text])[0],
                                       known_datasets, s.graph, s.audit, cfg.public_url)
        from cairn.marketplace.slos import SLOService

        def product_owners(urn: str) -> list[str]:
            own = s.graph.get_aspect(urn, "ownership") or {}
            return [o["owner"] for o in own.get("owners") or [] if o.get("owner")]

        s.slos = SLOService(s.engine, s.quality.store, s._product_assets, product_owners, hub, s.audit)
        from cairn.governance.privacy import PrivacyService

        def scanned_datasets() -> list:
            from cairn.quality.auto import Planner

            return Planner(s.graph).datasets()

        s.privacy = PrivacyService(s.engine, scanned_datasets, None, hub, s.audit, s.personas, s.ai_systems)
        from cairn.quality.external import ExternalResults

        s.external_quality = ExternalResults(s.engine, s.graph, hub, s.audit)
        from cairn.sources.openlineage import OpenLineageReceiver

        s.openlineage = OpenLineageReceiver(s.graph, s.audit, os.environ.get("CAIRN_OPENLINEAGE_ENV", "PROD"))
        from cairn.sources.git import GitService, load_repos

        try:
            repos = load_repos()
        except Exception:  # noqa: BLE001 - a bad file must not stop the API
            logger.exception("could not load config/git")
            repos = {}
        s.git = GitService(repos, s.graph, s.catalog, None, s.attributes, s.audit, cfg.public_url)
        from cairn.governance.bulk import BulkService
        s.bulk = BulkService(s.engine, s.graph, s.audit)
        _build_contracts(s, hub, Event)
        s.git.contracts = s.contracts
        _build_automations(s, hub, Event)
        from cairn.reports.service import ReportService
        s.reports = ReportService(s.engine, s.graph, s.quality.store, s.personas)
        _build_costs(s, hub, Event)
        if os.environ.get("CAIRN_PIPES_STORE") and os.environ.get("CAIRN_PIPES_CATALOG"):
            from cairn.pipelines.pipes import Pipes, _settings

            s.pipes = Pipes(s.engine, _settings("CAIRN_PIPES_STORE"), _settings("CAIRN_PIPES_CATALOG"), s.audit,
                            webhook_secret=os.environ.get("CAIRN_PIPES_WEBHOOK_SECRET", ""))
            s.pipes.llm = getattr(s, "llm", None)    # files pipes' AI enrichment (metered, as the pipe's owner)
        from cairn.iac.state import BundleState

        s.bundle_state = BundleState(s.engine, s.audit)
        if os.environ.get("CAIRN_STREAMS_KAFKA") or os.environ.get("CAIRN_STREAMS_ENABLED", "").lower() == "true":
            from cairn.pipelines.pipes import _settings
            from cairn.pipelines.streams import Streams

            s.streams = Streams(s.engine, _settings("CAIRN_STREAMS_KAFKA"),
                                _settings("CAIRN_STREAMS_CATALOG") or _settings("CAIRN_PIPES_CATALOG"), s.audit,
                                graph=getattr(s, "graph", None), registry=_settings("CAIRN_STREAMS_SCHEMA_REGISTRY"))
            # Mirrors of PostgreSQL tables (cairn.pipelines.mirror), written through the streams' writer.
            from cairn.pipelines.mirror import Mirrors
            from cairn.quality.collectors import _expand_env

            def source_url(name: str) -> str:
                d = (s.connections.definitions() or {}).get(name) if s.connections is not None else None
                return _expand_env(str(d.get("url") or "")) if d else ""
            s.mirrors = Mirrors(s.engine, s.streams, source_url, s.audit)
        if os.environ.get("CAIRN_MAINTENANCE_CONNECTION"):
            from cairn.pipelines.maintenance import TableMaintenance

            conn_name = os.environ["CAIRN_MAINTENANCE_CONNECTION"]
            maint_user = "urn:li:corpuser:" + os.environ.get("CAIRN_MAINTENANCE_USER", "svc-maintenance")

            def run_maintenance(sql: str) -> list:
                definition = (s.data_access.registry.definitions() or {}).get(conn_name) or {}
                return s.data_access.run_on_trino(definition, maint_user, sql, 100_000)[1]
            maint_catalog = os.environ.get("CAIRN_MAINTENANCE_CATALOG", "lakehouse")

            def table_queries(schema: str, table: str, since_ms: int):
                # Trino's query history (with Trino access on): what the table's queries filter on.
                qh, ta = getattr(s, "query_history", None), getattr(s, "trino_access", None)
                if qh is None or ta is None:
                    raise RuntimeError("automatic clustering needs Trino's query history (CAIRN_TRINO_ACCESS_KEY)")
                return qh.reading(ta.urn(maint_catalog, schema, table), since_ms)
            s.maintenance = TableMaintenance(s.engine, run_maintenance, maint_catalog, s.audit,
                                             table_queries if os.environ.get("CAIRN_TRINO_ACCESS_KEY") else None)
        if os.environ.get("CAIRN_DERIVED_CONNECTION") or os.environ.get("CAIRN_MAINTENANCE_CONNECTION"):
            from cairn.pipelines.derived import DerivedTables

            derived_conn = os.environ.get("CAIRN_DERIVED_CONNECTION") or os.environ["CAIRN_MAINTENANCE_CONNECTION"]

            def run_derived(owner: str, sql: str) -> list:      # as the derived table's owner
                definition = (s.data_access.registry.definitions() or {}).get(derived_conn) or {}
                return s.data_access.run_on_trino(definition, owner, sql, 100_000)[1]
            s.derived = DerivedTables(s.engine, run_derived, os.environ.get("CAIRN_DERIVED_CATALOG", "lakehouse"), s.audit)
        feed_conn = (os.environ.get("CAIRN_CHANGE_FEED_CONNECTION") or os.environ.get("CAIRN_DERIVED_CONNECTION")
                     or os.environ.get("CAIRN_MAINTENANCE_CONNECTION"))
        if feed_conn:                                    # a table's row changes, read as the caller
            from cairn.pipelines.change_feed import ChangeFeed

            def run_feed(actor: str, sql: str, limit: int):
                definition = (s.data_access.registry.definitions() or {}).get(feed_conn) or {}
                return s.data_access.run_on_trino(definition, actor, sql, limit)
            s.change_feed = ChangeFeed(run_feed, os.environ.get("CAIRN_DERIVED_CATALOG", "lakehouse"), s.audit)
        _build_data_access(s)
        _build_databases(s)
        _build_consent(s)
        _build_serving(s)
        _build_saml(s)
        _build_secret_scopes(s)
        _build_volumes(s)
        _build_storage_locations(s)
        _build_saas(s)
        _build_reverse_etl(s)
        _build_doc_sources(s)
        _build_external_catalogs(s)
        _build_branches(s)
        _build_records(s)
        _wire_residency(s)
        _build_tokens(s)
        _build_posture(s)
        _build_passkeys(s)
        from cairn.common.limits import ApiLimits

        s.limits = ApiLimits(s.engine, s.audit)
        s.privacy.data = s.data_access
        from cairn.sqlworkspace import SqlWorkspace

        s.sql_workspace = SqlWorkspace(s.engine, s.data_access, s.graph)
        # Sample datasets (queue row 26): written where pipes write; starter queries on the lakehouse connection.
        from cairn.pipelines.samples import Samples

        pipes = getattr(s, "pipes", None)
        s.samples = Samples(s.engine, pipes.catalog_props if pipes is not None else None, s.graph, s.sql_workspace, s.audit,
                            connection=os.environ.get("CAIRN_SAMPLES_CONNECTION", "lakehouse"),
                            trino_catalog=os.environ.get("CAIRN_SAMPLES_TRINO_CATALOG", "lakehouse"),
                            urn=pipes.urn if pipes is not None else None)
        # The shared result cache (dashboards, workbooks, metrics): shared only between people whose decisions on
        # every table read are the same, dropped when a table read changes.
        from cairn.governance.pdp import AuthorizeRequest as _AR, ResourceRef as _RR
        from cairn.sqlworkspace.cache import ResultCache

        def cache_decisions(actor: str, urn: str) -> list:
            out: list = []
            for privilege, channel in (("VIEW_ENTITY_PAGE", "trino"), ("VIEW_DATASET_PROFILE", "trino"),
                                       ("VIEW_DATASET_PROFILE", "core-proxy")):
                d = s.pdp.decide(_AR(actor=actor, privilege=privilege, actor_groups=[],
                                     resource=_RR(type="dataset", urn=urn), environment={"channel": channel}))
                out.append([d.decision, sorted(([o.type, o.params] for o in d.obligations), key=str)])
            out.append(bool(s.data_access.can_read(actor, urn)))
            return out
        s.sql_workspace.cache = ResultCache(s.engine, s.data_access.urns_of, cache_decisions,
                                            lambda: getattr(s, "dataset_changes", None), s.audit)
        # Workbooks: spreadsheet-style elements compiled to SQL and run through the workspace as the viewer.
        from cairn.sqlworkspace.workbooks import Workbooks, _table as _wb_table

        def workbook_columns(actor: str, connection: str, table: str) -> list[str]:
            out = s.data_access.query(actor, f"SELECT * FROM {_wb_table(table)} LIMIT 0", connection, None, 1)
            return list(out.get("columns") or [])
        s.workbooks = Workbooks(s.engine, s.sql_workspace, workbook_columns)
        # Input tables: lakehouse tables people type into, each statement run on Trino as the person writing.
        from cairn.sqlworkspace.input_tables import InputTables

        def input_run(connection: str, actor: str, sql: str):
            definition = (s.data_access.registry.definitions() or {}).get(connection) if s.data_access.registry else None
            if not definition or not definition.get("run_as_user") or not str(definition.get("url", "")).startswith("trino://"):
                from cairn.sqlworkspace.input_tables import InputTableError

                raise InputTableError(f"{connection!r} is not a Trino connection that runs as the user", 400)
            return s.data_access.run_on_trino(definition, actor, sql, 1000)[:2]

        def input_register(connection: str, table: str, columns: list, owner: str, description: str) -> None:
            import re as _re

            from cairn.sdk.source import Dataset, Field, to_mcps

            schema, name = table.split(".")
            urn = s.data_access._source(connection, None).urn(schema, name, None)
            m = _re.fullmatch(r"urn:li:dataset:\(urn:li:dataPlatform:([^,]+),(.+),([A-Z]+)\)", urn)
            if m is None or s.graph is None:
                return
            kinds = {"text": "string", "number": "number", "integer": "number", "boolean": "boolean", "date": "date",
                     "timestamp": "time"}
            fields = [Field(path=c["name"], type=kinds[c["type"]], native_type=c["type"]) for c in columns]
            fields += [Field(path="_row_id", native_type="varchar"), Field(path="_updated_by", native_type="varchar"),
                       Field(path="_updated_at", type="time", native_type="timestamp(6) with time zone")]
            s.graph.emit(to_mcps(m.group(1), [Dataset(
                name=m.group(2), fields=fields, owners=[owner], description=description or None, subtype="Table",
                properties={"cairn_input_table": "true", "cairn_connection": connection})], m.group(3)))
        def input_changed(connection: str, table: str) -> None:
            if getattr(s, "dataset_changes", None) is not None:
                schema, name = table.split(".")
                s.dataset_changes.record([s.data_access._source(connection, None).urn(schema, name, None)], "input-table")
        s.input_tables = InputTables(s.engine, input_run, s.data_access.query, s.audit, input_register, input_changed)
        from cairn.sqlworkspace.input_approvals import InputApprovals

        def tell_input(type_, title, body, recipients, payload):
            hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload,
                              url="/sql/input-tables"))
        s.input_tables.approvals = InputApprovals(
            s.engine, s.input_tables, lambda u: [str(g) for g in (s.attributes.subject(u).get("groups") or [])],
            lambda actor: s.authorize(Principal(actor, "token"), "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False),
            tell_input, s.audit)
        if getattr(s, "derived", None) is not None:      # materialized views, written in SQL (cairn.pipelines.matviews)
            from cairn.pipelines.matviews import MaterializedViews

            s.sql_workspace.matviews = MaterializedViews(
                s.derived, s.derived.catalog,
                lambda actor: s.authorize(Principal(actor, "token"), "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False))
        # Dashboards and alerts, over saved queries.
        from cairn.sqlworkspace.boards import Boards

        def tell(type_, title, body, recipients, payload):
            hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload,
                              url=f"{hub.public_url}/cairn/sql/workflows"))
        s.sql_boards = Boards(s.engine, s.sql_workspace, tell, s.workbooks)
        # Sharing queries, workbooks and dashboards with people and groups (viewers, editors); data stays the viewer's.
        from cairn.sqlworkspace.sharing import Sharing

        def share_groups(actor: str) -> list[str]:
            return [str(g) for g in (s.attributes.subject(actor).get("groups") or [])]

        def bi_link(kind: str, aid: str) -> str:
            # Where the asset opens in the UI: a dashboard itself, a workbook or saved query its page.
            from urllib.parse import quote

            path = {"dashboard": f"dashboards?open={quote(aid, safe='')}", "workbook": "sql/workbooks"}.get(kind, "sql")
            return f"{hub.public_url}/cairn/{path}"

        def tell_shared(title, name, body, recipients, payload):
            hub.publish(Event(type="bi.shared", title=title, body=body, recipients=recipients, payload=payload,
                              url=bi_link(payload["kind"], payload["id"])))
        s.bi_sharing = Sharing(s.engine, share_groups, s.audit, tell_shared)
        s.sql_workspace.sharing = s.workbooks.sharing = s.sql_boards.sharing = s.bi_sharing
        # The metrics layer: measures defined once, queried as the person (through workbook elements).
        from cairn.sqlworkspace.metrics import Metrics

        def metric_ddl(actor: str, connection: str, sql: str):
            # Statements for metric views run on the connection's Trino as the person (run_as_user connections).
            from cairn.dataaccess.service import DataAccessError
            from cairn.sqlworkspace.metrics import MetricError

            d = (s.data_access.registry.definitions() or {}).get(connection) or {}
            if not d.get("run_as_user"):
                raise MetricError(f"connection {connection!r} is not a Trino connection that runs as the user", 400)
            try:
                return s.data_access.run_on_trino(d, actor, sql, 10)
            except DataAccessError as e:
                raise MetricError(str(e), getattr(e, "status", 400))

        s.metrics = Metrics(s.engine, s.workbooks, s.audit, lambda actor: s.authorize(
            Principal(actor, "token"), "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False), ddl=metric_ddl)

        def metric_view_source(t: dict):
            # lake.metrics.<name> of a published metric → the metric's table in the same catalog.
            if s.trino_access is None or str(t.get("schemaName") or "").lower() != s.metrics.view_schema:
                return None
            from cairn.sqlworkspace.metrics import MetricError

            try:
                m = s.metrics.get(str(t.get("tableName") or "").lower())
            except MetricError:
                return None
            if not m["published"]:
                return None
            schema, _, table = m["table"].rpartition(".")
            return s.trino_access.urn(str(t.get("catalogName") or ""), schema, table)
        s.metric_view_source = metric_view_source
        s.sql_boards.metrics = s.metrics
        # Metric alerts: a metric watched per period as its alert's owner; told once on each change of state.
        from cairn.sqlworkspace.metric_alerts import MetricAlerts

        def tell_metric(type_, title, body, recipients, payload):
            hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload,
                              url=f"{hub.public_url}/cairn/sql/metrics"))
        s.metric_alerts = MetricAlerts(s.engine, s.metrics, tell_metric, s.audit)
        # Scheduled notebook and Python jobs: each run in a container of its own, as its owner (their Trino token).
        from cairn.pipelines.jobs import Jobs, runner_from_env

        def tell_job(type_, title, body, recipients, payload):
            hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload,
                              url=f"{hub.public_url}/v1/jobs/{payload['job']}/runs/{payload['run']}"))

        def job_token(owner: str, ttl: int):
            tokens = getattr(s, "trino_tokens", None)     # built later, with Trino; no Trino, no token
            return tokens.mint(owner, ttl)["token"] if tokens is not None else None
        s.jobs = Jobs(s.engine, runner_from_env(),
                      lambda actor: s.authorize(Principal(actor, "token"), "RUN_JOBS", None, raise_on_deny=False),
                      lambda actor: s.authorize(Principal(actor, "token"), "MANAGE_SYSTEM_OPERATIONS", None,
                                                raise_on_deny=False),
                      job_token, os.environ.get("CAIRN_JOBS_TRINO_URL", ""), tell_job, s.audit,
                      os.environ.get("CAIRN_JOBS_TRINO_CA_FILE", ""))
        # BI assets in the catalog: charts and dashboards with lineage from the tables and columns they read.
        from cairn.sqlworkspace.bi_catalog import BiCatalog
        from cairn.sqlworkspace.sharing import access as _bi_access

        def bi_can_open(actor: str, kind: str, aid: str) -> bool:
            from cairn.sqlworkspace.sharing import _table

            t = _table(kind)
            with s.engine.connect() as conn:
                row = conn.execute(t.select().where(t.c.id == aid)).first()
            return bool(_bi_access(s.bi_sharing, actor, kind, row))
        s.bi_catalog = BiCatalog(s.engine, s.graph, s.sql_workspace, s.workbooks, s.sql_boards, s.metrics, bi_can_open,
                                 lambda actor: s.authorize(Principal(actor, "token"), "MANAGE_SYSTEM_OPERATIONS", None,
                                                           raise_on_deny=False))
        # Version history: every save kept, compared and restored through the asset's own save.
        from cairn.sqlworkspace.versions import Versions

        s.bi_versions = Versions(s.engine, s.sql_workspace, s.workbooks, s.sql_boards, s.bi_sharing)

        def bi_changed(kind: str, aid: str, actor: str) -> None:
            try:
                s.bi_versions.record(kind, aid, actor)
            except Exception:  # noqa: BLE001 - the save stands; the next one records the asset as it is then
                logger.exception("recording a version of %s %s failed", kind, aid)
            s.bi_catalog.changed(kind, aid, actor)
            if s.bi_collab is not None:
                try:
                    s.bi_collab.forget_if_gone(kind, aid)
                except Exception:  # noqa: BLE001 - the delete stands; what is left points at nothing and is not shown
                    logger.exception("forgetting the comments and views of %s %s failed", kind, aid)
        s.sql_workspace.on_change = s.workbooks.on_change = s.sql_boards.on_change = bi_changed
        # Folders, comments and usage of BI assets.
        from cairn.sqlworkspace.collab import BiCollab

        def bi_level(actor: str, kind: str, aid: str) -> int:
            from cairn.sqlworkspace.sharing import _table

            t = _table(kind)
            with s.engine.connect() as conn:
                row = conn.execute(t.select().where(t.c.id == aid)).first()
            return _bi_access(s.bi_sharing, actor, kind, row)

        def tell_bi(type_, title, body, recipients, payload):
            hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload,
                              url=bi_link(payload["kind"], payload["id"])))
        s.bi_collab = BiCollab(s.engine, bi_level, tell_bi, s.audit, int(os.environ.get("CAIRN_BI_USAGE_DAYS", "180")))
        s.sql_workspace.on_view = s.workbooks.on_view = s.sql_boards.on_view = s.bi_collab.viewed
        # Embedded dashboards: portals sign a token for one of their users; the tiles run as that user.
        embed_clients = os.environ.get("CAIRN_EMBED_CLIENTS_FILE")
        if embed_clients:
            from cairn.embed.dashboards import EmbeddedDashboards, load_clients

            s.embedded_dashboards = EmbeddedDashboards(s.engine, s.sql_boards, load_clients(embed_clients), s.audit)
        # The AI builder: a question becomes a dashboard of metric and workbook tiles, each checked as the person.
        from cairn.sqlworkspace.builder import Builder

        def urn_of(connection: str, table: str) -> Optional[str]:
            try:
                parts = table.split(".")
                return s.data_access._source(connection, None).urn(parts[-2], parts[-1], parts[0] if len(parts) == 3 else None)
            except Exception:  # noqa: BLE001 - an unknown connection or table: not usable
                return None
        s.builder = Builder(s.llm, s.settings.ai.model_nlq or s.settings.ai.model, s.ask, s.metrics, s.sql_boards,
                            urn_of, s.audit)
        # Semantic model autopilot: tables profiled as the person and query history's shapes → draft metrics.
        from cairn.sqlworkspace.autopilot import Autopilot

        s.autopilot = Autopilot(s.engine, s.workbooks, s.metrics, lambda: getattr(s, "query_history", None), urn_of,
                                s.llm, s.settings.ai.model_nlq or s.settings.ai.model, s.audit)
        # The pipeline agent: a goal becomes a derived table and checks, tried on Trino as the person, shipped on
        # their approval (needs derived tables).
        if getattr(s, "derived", None) is not None:
            from cairn.ai.pipeline_agent import PipelineAgent

            agent_conn = os.environ.get("CAIRN_DERIVED_CONNECTION") or os.environ.get("CAIRN_MAINTENANCE_CONNECTION")

            def agent_run(actor: str, sql: str, limit: int):
                definition = (s.data_access.registry.definitions() or {}).get(agent_conn) or {}
                return s.data_access.run_on_trino(definition, actor, sql, limit)

            def agent_context(actor: str, goal: str, urns: list, can_view) -> dict:
                from cairn.dataaccess.ask import AskError

                try:
                    _source, chosen = s.ask._tables(goal, urns, can_view)
                except AskError:
                    return {}
                return {ref: cols for ref, cols in chosen.values()}
            s.pipeline_agent = PipelineAgent(s.engine, s.llm, s.settings.ai.model, s.derived, agent_run, agent_context,
                                             s.audit)
        # Stored procedures: parameterized SQL scripts on a Trino connection, run as their caller.
        from cairn.sqlworkspace.procedures import Procedures
        from cairn.sqlworkspace.workspace import WorkspaceError

        def run_procedure(connection: str, actor: str, sql: str):
            definition = s.data_access.registry.definitions().get(connection)
            if definition is None:
                raise WorkspaceError(f"unknown connection {connection!r}", 404)
            columns, rows, _ = s.data_access.run_on_trino(definition, actor, sql, 1000)
            return columns, rows
        s.procedures = Procedures(s.engine, run_procedure, lambda actor, urn: s.authorize(
            Principal(actor, "token"), "EXECUTE_PROCEDURE", urn, raise_on_deny=False), s.audit)
        # Clean rooms: parties join tables in aggregate (k-anonymous), through the room's own Trino identity.
        if getattr(s, "trino_access", None) is not None:
            from cairn.dataaccess.cleanrooms import CleanRooms

            def run_room(actor: str, sql: str):
                conn = os.environ.get("CAIRN_CLEANROOM_CONNECTION", "lakehouse")
                definition = s.data_access.registry.definitions().get(conn)
                if definition is None:
                    raise WorkspaceError(f"unknown connection {conn!r}", 404)
                columns, rows, _ = s.data_access.run_on_trino(definition, actor, sql, 10_000)
                return columns, rows

            def groups_of(urn: str) -> list[str]:
                return list(s.attributes.subject(urn).get("groups", [])) if s.attributes is not None else []

            def may_read_table(actor: str, table: str) -> bool:
                cat, schema, name = table.split(".", 2)
                return s.trino_access.allowed(actor, groups_of(actor), "VIEW_DATASET_PROFILE",
                                              s.trino_access.urn(cat, schema, name))
            from cairn.dataaccess.cleanrooms import catalog_writer

            pipes = getattr(s, "pipes", None)          # outside parties' uploads go where pipes write (CAIRN_PIPES_CATALOG)
            s.clean_rooms = CleanRooms(s.engine, run_room, lambda actor, party: party in groups_of(actor), may_read_table,
                                       s.audit, os.environ.get("CAIRN_CLEANROOM_CATALOG", "lakehouse"),
                                       catalog_writer(pipes.catalog_props) if pipes is not None and pipes.catalog_props else None)
            s.trino_access.cleanroom_tables = s.clean_rooms.tables_of
        # Git folders: saved queries and notebooks kept in git (CAIRN_GIT_FOLDER_URLS: other URLs they may use).
        from cairn.sqlworkspace.gitfolders import GitFolders

        s.git_folders = GitFolders(s.engine, s.sql_workspace, getattr(s, "git", None), getattr(s, "notebook_catalog", None),
                                   s.audit, os.environ.get("CAIRN_GIT_FOLDERS_DIR") or None,
                                   [u.strip() for u in os.environ.get("CAIRN_GIT_FOLDER_URLS", "").split(",")])
        # SQL workflows: derived-table refreshes, checks and reports, scheduled, run as their owner.
        from cairn.sqlworkspace.workflows import Workflows

        def tell_workflow(type_, title, body, recipients, payload):
            hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload, url="/sql"))
        s.sql_workflows = Workflows(s.engine, s.sql_workspace, getattr(s, "derived", None), tell_workflow,
                                lambda actor: s.authorize(Principal(actor, "token"), "MANAGE_SYSTEM_OPERATIONS", None,
                                                          raise_on_deny=False))
        # App actions on dashboards: forms and buttons that write input tables, run workflows or call APIs.
        from cairn.sqlworkspace.actions import Actions

        s.bi_actions = Actions(s.engine, s.sql_boards, s.input_tables, s.sql_workflows, s.audit,
                               lambda actor: s.authorize(Principal(actor, "token"), "MANAGE_SYSTEM_OPERATIONS", None,
                                                         raise_on_deny=False))
        s.sql_boards.actions = s.bi_actions          # a dashboard's actions are saved with it (as code)
        # Scheduled exports: dashboards and workbooks sent as CSV or PDF, each recipient rendered as themselves.
        from cairn.sqlworkspace.reports import Reports

        def tell_report(type_, title, body, recipients, payload):
            # An absolute link (Teams, Slack, email); the file opens only for its own, signed-in recipient.
            link = f"{hub.public_url}/cairn/api/sql/reports/files/{payload['file']}" if payload.get("file") else None
            hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload, url=link))
        s.report_schedules = Reports(s.engine, s.sql_boards, s.workbooks, tell_report, s.audit)
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

    # The assistant's workspace (cairn.ai.workspace): memory, artifacts shared fail-closed, deep research.
    from cairn.ai.workspace import Workspace

    def may_read(actor: str, urn: str) -> bool:
        return s.authorize(Principal(urn=actor, via="delegated"), "VIEW_DATASET_PROFILE", urn, raise_on_deny=False)

    def person_groups(actor: str) -> list[str]:
        return [str(g) for g in (s.attributes.subject(actor).get("groups") or [])] if s.attributes is not None else []
    s.ai_workspace = Workspace(s.engine, may_read, person_groups, s.audit)
    s.ai_workspace.assistant = s.assistant
    s.assistant.workspace = s.ai_workspace
    if s.profiles is not None:            # the assistant and CoWork's tasks know who they work for
        s.ai_workspace.profile_prompt = s.profiles.prompt
    # CoWork (cairn.ai.cowork): the assistant's scheduled tasks for a person, and an outbox they approve.
    from cairn.ai.cowork import CoWork

    def cowork_authorize(actor: str, privilege: str, urn: Optional[str]) -> bool:
        return s.authorize(Principal(urn=actor, via="delegated"), privilege, urn, raise_on_deny=False)

    def cowork_active(person: str) -> bool:
        if s.graph is None:
            return True
        return (s.graph.get_aspect(person, "corpUserStatus") or {}).get("status", "ACTIVE") != "SUSPENDED"
    s.cowork = CoWork(s.engine, s.ai_workspace, s.notify, cowork_authorize, s.audit, active=cowork_active)
    # The AI gateway (cairn.ai.gateway): model calls from code people and agents write, as them, metered.
    from cairn.ai.gateway import AiGateway

    s.ai_gateway = AiGateway.from_env(s.ai_usage)
    if s.ai_gateway is not None:                 # operators' routes: virtual models with fallback (L113)
        from cairn.ai.gateway_routes import GatewayRoutes

        s.ai_gateway.routes = GatewayRoutes(s.engine, s.audit)
        s.ai_gateway.audit = s.audit
    # Agent apps (cairn.ai.agent_apps): code-first agents deployed to Cairn, run as the person asking.
    if os.environ.get("CAIRN_AGENT_APPS_RUNTIME") in ("process", "kubernetes") and s.agents is not None:
        import tempfile
        from pathlib import Path as _Path

        from cairn.ai.agent_apps import AgentApps, ProcessRuntime

        def apps_authorize(actor: str, privilege: str, urn: Optional[str]) -> bool:
            return s.authorize(Principal(urn=actor, via="delegated"), privilege, urn, raise_on_deny=False)
        root = _Path(os.environ.get("CAIRN_AGENT_APPS_DIR") or os.path.join(tempfile.gettempdir(), "cairn-agent-apps"))
        from cairn.ai.agent_apps import MlflowRuns

        cairn_url = os.environ.get("CAIRN_AGENT_APPS_CAIRN_URL", "http://127.0.0.1:9000")
        if os.environ["CAIRN_AGENT_APPS_RUNTIME"] == "kubernetes":
            from cairn.ai.agent_apps_k8s import KubeRuntime

            agent_runtime: Any = KubeRuntime.from_env(cairn_url)    # misconfigured: Cairn refuses to start
        else:
            agent_runtime = ProcessRuntime(root, cairn_url)
        s.agent_apps = AgentApps(s.engine, s.agents, agent_runtime, apps_authorize,
            lambda actor: apps_authorize(actor, "MANAGE_SYSTEM_OPERATIONS", None), s.audit,
            MlflowRuns(os.environ["CAIRN_MLFLOW_URL"]) if os.environ.get("CAIRN_MLFLOW_URL") else None)
        s.agent_apps.intents = s.intents
        if getattr(s, "share_packages", None) is not None:      # built first: agents on shared data use these
            s.share_packages.agent_apps = s.agent_apps

    # Custom agents (cairn.ai.custom_agents): built and deployed in Cairn, run as the person who starts them.
    if getattr(s, "agents", None) is not None:
        from cairn.ai.assistant import TOOL_SPECS
        from cairn.ai.custom_agents import CustomAgents

        def agent_groups(actor: str) -> list[str]:
            return [str(g) for g in (s.attributes.subject(actor).get("groups") or [])] if s.attributes is not None else []
        s.custom_agents = CustomAgents(
            s.engine, s.agents, s.assistant, TOOL_SPECS,
            lambda actor, privilege, urn: s.authorize(Principal(urn=actor, via="delegated"), privilege, urn,
                                                      raise_on_deny=False),
            agent_groups, s.audit)
        s.custom_agents.intents = s.intents
        # Agent trace intelligence (cairn.ai.trace_insights): loops, errors, slow calls, cost and outliers per agent.
        from cairn.ai.trace_insights import TraceInsights

        s.trace_insights = TraceInsights(
            s.engine, s.agents,
            lambda actor, privilege: s.authorize(Principal(urn=actor, via="delegated"), privilege, None,
                                                 raise_on_deny=False),
            s.audit)
        # Agent memory (cairn.ai.agent_memory): what an agent remembers about the people it works for, governed.
        from cairn.ai.agent_memory import AgentMemory
        from cairn.ai.embeddings import HashingEmbedder as _Hashing

        memory_embedder = s.semantic.embedder if s.semantic is not None else _Hashing()

        def memory_may_use(actor: str, agent: dict) -> bool:
            if agent.get("owner") == actor:
                return True
            if agent.get("kind") == "built":
                built = next((r for r in s.custom_agents.list(actor) if r.get("agent_id") == agent["id"]), None)
                return built is not None
            if agent.get("kind") == "app":
                return s.authorize(Principal(urn=actor, via="delegated"), "USE_AGENT",
                                   f"urn:li:cairnAgent:{agent['name']}", raise_on_deny=False)
            return False
        s.agent_memory = AgentMemory(
            s.engine, s.agents, lambda text: memory_embedder.embed([text])[0], memory_may_use,
            lambda actor: s.authorize(Principal(urn=actor, via="delegated"), "MANAGE_SYSTEM_OPERATIONS", None,
                                      raise_on_deny=False),
            agent_groups, s.audit)
        s.custom_agents.memory = s.agent_memory
        # Agentic abuse detection (cairn.ai.abuse): injection in, exfiltration out; findings, auto-pause.
        from cairn.ai.abuse import AbuseGuard

        s.abuse = AbuseGuard(
            s.engine, s.agents,
            lambda actor, privilege: s.authorize(Principal(urn=actor, via="delegated"), privilege, None,
                                                 raise_on_deny=False),
            s.audit, AbuseGuard.judge_from_env())
        s.trace_insights.abuse = s.abuse
        s.custom_agents.abuse = s.abuse
        if s.ai_gateway is not None:
            s.ai_gateway.abuse = s.abuse
        if s.agent_apps is not None:
            s.agent_apps.memory = s.agent_memory

    # Code sandboxes (cairn.ai.sandboxes): code from agents and people, a container per run, data as the person.
    from cairn.ai.sandboxes import Sandboxes, runtime_from_env

    s.sandboxes = Sandboxes(
        s.engine, runtime_from_env(),
        lambda actor, privilege, urn: s.authorize(Principal(urn=actor, via="delegated"), privilege, urn,
                                                  raise_on_deny=False),
        lambda actor: s.authorize(Principal(urn=actor, via="delegated"), "MANAGE_SYSTEM_OPERATIONS", None,
                                  raise_on_deny=False),
        getattr(s, "agents", None), os.environ.get("CAIRN_SANDBOX_CAIRN_URL", ""), s.audit)
    if os.environ.get("CAIRN_SANDBOX_RUNTIME") == "kubernetes" and os.environ.get("CAIRN_SANDBOX_K8S_PROXY_LISTEN"):
        from cairn.ai.sandboxes import ProxyCredentials, TcpProxy

        # Kubernetes sandboxes' only way out: this egress proxy, on every API replica.
        s.sandbox_proxy = TcpProxy(os.environ["CAIRN_SANDBOX_K8S_PROXY_LISTEN"],
                                   ProxyCredentials((os.environ.get("CAIRN_SANDBOX_PROXY_KEY") or "").encode()),
                                   s.sandboxes.cairn_url, s.sandboxes.allow_private, s.sandboxes.max_egress,
                                   s.sandboxes.cairn_ca)

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


def _secret_env(name: str) -> str:
    """A secret from ``<NAME>_FILE`` (a mounted secret) or ``<NAME>``."""
    path = os.environ.get(name + "_FILE", "")
    if path:
        with open(path) as fh:
            return fh.read().strip()
    return os.environ.get(name, "")


def _build_databases(s: Services) -> None:
    """Operational PostgreSQL databases made and handed out through Cairn (cairn.dataaccess.databases), on the server
    given to Cairn (the instance ``default``) and on the instances Cairn runs (cairn.databases.instances)."""
    admin = _secret_env("CAIRN_DATABASES_ADMIN_URL")
    instances = _build_pg_instances(s, admin)
    if not admin and instances is None:
        return
    from cairn.dataaccess.databases import Databases

    def allowed(actor: str, privilege: str, urn: Optional[str]) -> bool:
        return s.authorize(Principal(urn=actor, via="delegated"), privilege, urn, raise_on_deny=False)
    s.databases = Databases(s.engine, admin, os.environ.get("CAIRN_DATABASES_HOST", "localhost"),
                            int(os.environ.get("CAIRN_DATABASES_PORT", "5432")), allowed, s.audit,
                            os.environ.get("CAIRN_DATABASES_SSLMODE", "require"), instances=instances)
    if instances is not None:
        instances.databases = s.databases


def _build_pg_instances(s: Services, admin: str):
    """Managed Postgres (L140): instances on Docker or CloudNativePG (CAIRN_PG_RUNTIME), backed up to the store
    (CAIRN_PG_BACKUP_STORE). Without a runtime, the default instance alone (health and metrics of the server given)."""
    runtime_kind = os.environ.get("CAIRN_PG_RUNTIME", "").strip()
    if not runtime_kind and not admin:
        return None
    try:
        import psycopg  # noqa: F401 - instances are reached (parameters, health, statements) with psycopg
    except ImportError:
        logger.error("managed PostgreSQL instances need psycopg (pip install 'cairn-platform[postgres]'): they are off")
        return None
    import json

    from cairn.databases.instances import BackupStore, InstanceError, Instances

    def allowed(actor: str, privilege: str, urn: Optional[str]) -> bool:
        return s.authorize(Principal(urn=actor, via="delegated"), privilege, urn, raise_on_deny=False)

    store_cfg, store = None, None
    raw = os.environ.get("CAIRN_PG_BACKUP_STORE", "")
    if raw and runtime_kind:
        cfg = json.loads(raw)
        ak, sk = _secret_env("CAIRN_PG_BACKUP_ACCESS_KEY"), _secret_env("CAIRN_PG_BACKUP_SECRET_KEY")
        if not (cfg.get("endpoint") and cfg.get("bucket") and ak and sk):
            logger.error("CAIRN_PG_BACKUP_STORE needs endpoint and bucket, and CAIRN_PG_BACKUP_ACCESS_KEY(_FILE) and "
                         "CAIRN_PG_BACKUP_SECRET_KEY(_FILE): managed instances are off")
            return None
        store = BackupStore(cfg["endpoint"], cfg["bucket"], ak, sk, cfg.get("region") or "us-east-1")
        # The instances' sidecars may reach the store at another address (endpoint_for_instances).
        store_cfg = {"endpoint": cfg.get("endpoint_for_instances") or cfg["endpoint"], "bucket": cfg["bucket"],
                     "region": cfg.get("region") or "us-east-1", "access_key": ak, "secret_key": sk,
                     "endpoint_in_cluster": cfg.get("endpoint_for_instances") or cfg["endpoint"]}
    runtime = None
    try:
        if runtime_kind == "docker":
            from cairn.databases.docker_runtime import DockerRuntime

            images = os.environ.get("CAIRN_PG_DOCKER_IMAGES", "")
            lo, _, hi = os.environ.get("CAIRN_PG_DOCKER_PORTS", "15432-15999").partition("-")
            runtime = DockerRuntime(os.environ.get("CAIRN_PG_DOCKER_SOCKET", "/var/run/docker.sock"),
                                    os.environ.get("CAIRN_PG_DOCKER_BIND", "127.0.0.1"),
                                    os.environ.get("CAIRN_PG_DOCKER_HOST", "127.0.0.1"),
                                    json.loads(images) if images else None,
                                    os.environ.get("CAIRN_PG_DOCKER_AGENT_IMAGE", "python:3.12-slim"),
                                    os.environ.get("CAIRN_PG_DOCKER_NETWORK", ""), store_cfg,
                                    ports=(int(lo), int(hi or lo)))
        elif runtime_kind == "kubernetes":
            from cairn.databases.cnpg import CnpgRuntime

            images = os.environ.get("CAIRN_PG_K8S_IMAGES", "")
            store_k8s = None
            if store_cfg:
                store_k8s = {k: v for k, v in store_cfg.items() if k not in ("access_key", "secret_key")}
            runtime = CnpgRuntime(os.environ.get("CAIRN_PG_K8S_NAMESPACE", "cairn-postgres"),
                                  os.environ.get("CAIRN_PG_K8S_API", ""), os.environ.get("CAIRN_PG_K8S_TOKEN_FILE", ""),
                                  os.environ.get("CAIRN_PG_K8S_CA_FILE", ""), json.loads(images) if images else None,
                                  store_k8s, os.environ.get("CAIRN_PG_K8S_BACKUP_SECRET", ""),
                                  os.environ.get("CAIRN_PG_K8S_ACCESS", "cluster-ip"),
                                  os.environ.get("CAIRN_PG_K8S_NODE_ADDRESS", ""),
                                  os.environ.get("CAIRN_PG_K8S_STORAGE_CLASS", ""))
        elif runtime_kind:
            logger.error("CAIRN_PG_RUNTIME is docker or kubernetes, not %r: managed instances are off", runtime_kind)
            return None
        key = _secret_env("CAIRN_PG_INSTANCE_KEY").encode()
        inst = Instances(s.engine, allowed, s.audit, key, runtime, store if runtime else None,
                         default_admin_url=admin, default_host=os.environ.get("CAIRN_DATABASES_HOST", "localhost"),
                         default_port=int(os.environ.get("CAIRN_DATABASES_PORT", "5432")))
    except Exception as e:  # noqa: BLE001 - a misconfiguration: managed instances are off, said loudly
        logger.error("managed Postgres is off: %s", e if isinstance(e, InstanceError) else repr(e))
        return None
    s.pg_instances = inst
    s.pg_backups = inst if inst.store is not None else None
    return inst


def _build_data_access(s: Services) -> None:
    from cairn.coreproxy.proxy import MaskingService
    from cairn.dataaccess.service import DataAccessService

    masking = MaskingService(s.pdp, s.attributes)

    def can_read(actor: str, urn: str) -> bool:
        p = Principal(urn=actor, via="delegated")
        return s.can_view(p)(urn) and s.authorize(p, "VIEW_DATASET_PROFILE", urn, raise_on_deny=False)

    def known(urn: str) -> bool:
        if s.graph is None or (s.graph.get_aspect(urn, "status") or {}).get("removed"):
            return False
        return any(s.graph.get_aspect(urn, a) for a in ("schemaMetadata", "datasetProperties"))

    def on_read(actor: str, urns: list[str]) -> None:
        """Reads by AI (an MCP or assistant tool, or an agent's key) feed AI usage lineage."""
        from cairn.ai.traces import ai_channel
        from cairn.identity.agents import current_agent

        channel, agent = ai_channel.get(), current_agent.get()
        if s.traces is not None and (channel or agent):
            s.traces.record_reads(actor, agent, channel or "api", urns, "data")

    s.data_access = DataAccessService(s.connections, masking.plan, can_read, s.audit, known=known, on_read=on_read)

    # Aggregation and projection policies (cairn.governance.aggregation): obligations on reading a table.
    from cairn.governance.aggregation import CheckedQueries, from_obligations
    from cairn.governance.pdp import AuthorizeRequest as _AR, ResourceRef as _RR

    def person_groups(actor: str) -> list[str]:
        return [str(g) for g in (s.attributes.subject(actor).get("groups") or [])] if s.attributes is not None else []

    def query_policies(actor: str, urn: str):
        d = s.pdp.decide(_AR(actor=actor, privilege="VIEW_DATASET_PROFILE", actor_groups=person_groups(actor),
                             resource=_RR(type="dataset", urn=urn), environment={"channel": "query"}))
        return from_obligations(d.obligations)
    s.checked_queries = CheckedQueries(s.engine)
    s.data_access.query_policies = query_policies
    s.data_access.checked_identity = lambda actor, urns: s.checked_queries.register(actor, person_groups(actor), urns)

    # Trino asks Cairn before every action (its OPA access-control plugin) when given this key.
    trino_key = _secret_env("CAIRN_TRINO_ACCESS_KEY")
    if trino_key:
        import json as _json

        from cairn.dataaccess.trino import TrinoAccess, parse_catalogs
        from cairn.governance.pdp import AuthorizeRequest, ResourceRef

        if len(trino_key) < 24:
            raise ValueError("CAIRN_TRINO_ACCESS_KEY must be at least 24 characters")
        raw = os.environ.get("CAIRN_TRINO_CATALOGS", "").strip()
        catalogs = parse_catalogs(_json.loads(open(raw).read() if raw and not raw.startswith("{") else (raw or "{}")))

        def decide(actor: str, groups: list[str], privilege: str, urn: Optional[str]):
            return s.pdp.decide(AuthorizeRequest(actor=actor, privilege=privilege, actor_groups=groups,
                                                 resource=ResourceRef(type="function" if urn.startswith("urn:li:function:")
                                                                      else "dataset", urn=urn) if urn else None,
                                                 environment={"channel": "trino"}))

        ai_catalogs = {c.strip() for c in os.environ.get("CAIRN_TRINO_AI_CATALOGS", "llm").split(",") if c.strip()}
        routine_catalogs = {c.strip() for c in os.environ.get("CAIRN_TRINO_ROUTINE_CATALOGS", "routines").split(",") if c.strip()}
        # System tables (cairn.dataaccess.system_tables): Cairn's operational data as Trino tables, governed here.
        from cairn.dataaccess.system_tables import SystemTables

        s.system_tables = SystemTables.from_env(s.engine, s.graph,
                                                lambda u: [str(g) for g in (s.attributes.subject(u).get("groups") or [])],
                                                audit=s.audit)
        if s.system_tables is not None:
            from cairn.dataaccess.trino import CatalogMapping

            name = s.system_tables.trino_catalog
            want = CatalogMapping(platform="iceberg", database=name)
            if name in catalogs and (catalogs[name].platform, catalogs[name].database) != ("iceberg", name):
                raise ValueError(f"CAIRN_TRINO_CATALOGS[{name}] is the system tables' catalog: leave it out")
            catalogs[name] = want
        s.trino_access = TrinoAccess(decide, masking.plan, catalogs, ai_catalogs, routine_catalogs)
        s.trino_access.system_tables = s.system_tables
        s.trino_access.checked = s.checked_queries.lookup
        s.trino_key = trino_key
        # Query history, profiles and tuning hints, from the same events as usage statistics.
        from cairn.dataaccess.query_history import QueryHistory

        s.query_history = QueryHistory(s.engine, s.trino_access.urn)

        def trino_user_of(user: str) -> str:
            """Checked queries are recorded as the person they were checked for."""
            reg = s.checked_queries.lookup(user) if user.startswith("checked-") else None
            return reg["actor"].removeprefix("urn:li:corpuser:") if reg else user
        s.query_history.user_of = trino_user_of
        # AI functions in SQL through Cairn: the provider's key, allowed models, budgets and usage.
        from cairn.ai.sql_gateway import SqlAiGateway

        s.sql_ai = SqlAiGateway.from_env(s.ai_usage) if s.ai_usage is not None else None
        if s.sql_ai is not None:
            s.trino_access.ai_admit = s.sql_ai.admit
        # Published metric views read on their table's terms; looked up when asked, whichever is built first.
        s.trino_access.metric_view_source = lambda t: (s.metric_view_source(t) if getattr(s, "metric_view_source", None)
                                                       else None)
        # Sign-in to Trino: Cairn's tokens (Trino checks them against /v1/trino/jwks.json).
        from cairn.dataaccess.trino_auth import TrinoTokens

        s.trino_tokens = TrinoTokens(s.engine, trino_key, os.environ.get("CAIRN_TRINO_TOKEN_ISSUER", "cairn"),
                                     int(os.environ.get("CAIRN_TRINO_TOKEN_TTL", "3600")))
        s.data_access.trino_tokens = s.trino_tokens
        if s.connections is not None:          # monitors and profiling sign in to Trino as their service user
            s.connections.trino_tokens = s.trino_tokens
        # Sign-in to Trino's web UI and clients' external authentication, with Cairn as the OAuth2 server.
        # Cairn's OAuth2 sign-in for Trino and Airflow: each client its own secret, redirects and audience.
        oauth_secret = _secret_env("CAIRN_TRINO_OAUTH_SECRET")
        airflow_secret = _secret_env("CAIRN_AIRFLOW_OAUTH_SECRET")
        hub_secret = _secret_env("CAIRN_JUPYTERHUB_OAUTH_SECRET")
        if oauth_secret or airflow_secret or hub_secret:
            from cairn.dataaccess.trino_oauth import TrinoOAuth

            s.trino_oauth = TrinoOAuth(s.engine, s.trino_tokens, oauth_secret or "",
                                       os.environ.get("CAIRN_TRINO_OAUTH_REDIRECT_URIS", "").split(","),
                                       int(os.environ.get("CAIRN_TRINO_TOKEN_TTL", "3600")))
            if airflow_secret:
                s.trino_oauth.add_client("airflow", airflow_secret,
                                         os.environ.get("CAIRN_AIRFLOW_OAUTH_REDIRECT_URIS", "").split(","), "airflow")
            if hub_secret:
                # JupyterHub: sign-in through Cairn, kernels with a Cairn token of the person's own.
                from cairn.identity.notebooks import NotebookCatalog, NotebookSessions, table_urn_for

                s.trino_oauth.add_client("jupyterhub", hub_secret,
                                         os.environ.get("CAIRN_JUPYTERHUB_OAUTH_REDIRECT_URIS", "").split(","), "jupyterhub")
                hub_graph = s.graph
                s.notebooks = NotebookSessions(
                    s.trino_tokens, s.audit,
                    (lambda urn: (hub_graph.get_aspect(urn, "corpUserStatus") or {}).get("status", "ACTIVE"))
                    if hub_graph is not None else None)
                if s.identity is not None:
                    s.identity.notebook_lookup = s.notebooks.principal_of
                s.notebook_catalog = NotebookCatalog(s.graph, table_urn_for(s.trino_access),
                                                     os.environ.get("CAIRN_NOTEBOOK_DEFAULT_CATALOG", "lakehouse"),
                                                     s.audit) if s.graph is not None else None
                if getattr(s, "git_folders", None) is not None:     # built before the catalog was
                    s.git_folders.notebooks = s.notebook_catalog
        # Lineage Core cannot take now waits in the database (Trino sends each event once).
        from cairn.dataaccess.trino import LINEAGE_ACTOR
        from cairn.dataaccess.trino_lineage import LineageOutbox

        s.trino_outbox = LineageOutbox(s.engine, lambda events: s.openlineage.handle_many(events, LINEAGE_ACTOR),
                                       s.audit, int(os.environ.get("CAIRN_TRINO_LINEAGE_MAX_AGE_SECONDS", str(7 * 86400))))
        if s.graph is not None:
            from cairn.dataaccess.trino_usage import TrinoUsage

            s.trino_usage = TrinoUsage(s.engine, s.graph, s.trino_access.urn)
            s.trino_usage.user_of = trino_user_of
            from cairn.dataaccess.trino_catalog import TrinoCatalogSync

            s.trino_catalog = TrinoCatalogSync(s.engine, s.graph, s.data_access, s.trino_access, s.audit)
            from cairn.dataaccess.trino_functions import FunctionCatalog

            # Stored functions as catalog assets, from Trino's events and the sync (cairn.dataaccess.trino_functions).
            s.trino_functions = FunctionCatalog(s.engine, s.graph, s.trino_access.routine_catalogs, s.audit)

    if getattr(s, "trino_access", None) is not None:
        # Engines that read files themselves (Spark, pyiceberg): Cairn Catalog asks about every table.
        from cairn.dataaccess.lakehouse import LakehouseAccess

        s.lakehouse = LakehouseAccess(s.trino_access, lambda urn: s.attributes.subject(urn).get("groups", []),
                                      os.environ.get("CAIRN_LAKEHOUSE_TRINO_CATALOG", "lakehouse"),
                                      tuple(os.environ.get("CAIRN_LAKEHOUSE_CATALOG_URIS", "").split(",")))
        if s.openlineage is not None:
            # Datasets as the catalog names them, whoever sent the event: Spark (storage paths, renamed by
            # their symlinks) and anything naming Trino tables (trino://host:port, catalog.schema.table).
            lake, trino_access = s.lakehouse, s.trino_access
            s.openlineage.rewrite = lambda e: trino_access.remap_lineage(lake.remap_lineage(e))
        if getattr(s, "trino_tokens", None) is not None:
            # Workload identity: pipelines exchange their platform's token (Kubernetes, CI) for a Cairn token.
            from cairn.identity.workload import from_env as workload_from_env

            g = s.graph
            s.workload = workload_from_env(
                s.engine, s.trino_tokens, s.audit,
                (lambda urn: (g.get_aspect(urn, "corpUserStatus") or {}).get("status", "ACTIVE")) if g is not None else None)
            if s.workload is not None and s.identity is not None:
                s.identity.workload_lookup = s.workload.principal_of
            from cairn.dataaccess.lakehouse import LakehouseSessions

            graph = s.graph
            s.lakehouse_sessions = LakehouseSessions(
                s.trino_tokens, s.engine, s.audit,
                user_status=(lambda urn: (graph.get_aspect(urn, "corpUserStatus") or {}).get("status", "ACTIVE"))
                if graph is not None else None)
            # Tables shared with recipients outside the organisation, through Iceberg REST.
            from cairn.dataaccess.shares import Shares

            s.shares = Shares(s.engine, s.trino_tokens, s.lakehouse, s.audit)
            s.lakehouse.shares = s.shares
            # The same shares through the open Delta Sharing protocol (pre-signed Parquet files of the tables).
            from cairn.dataaccess.delta_sharing import DeltaSharing

            s.shares.delta = DeltaSharing(s.shares)
            # Usage overview: the quality of the tables recipients read; notices to the share's maker.
            s.shares.quality_status = lambda urns: {u: v.get("quality_status") for u, v in s.trust(urns).items()}

            class _Hub:                                  # built later: looked up when a notice is sent
                def publish(self, event):
                    hub = getattr(s, "notify", None)
                    return hub.publish(event) if hub is not None else []
            s.shares.notify = _Hub()
            # Data packages (the share's semantic context, signed) and the provider's agents on shared data.
            from cairn.dataaccess.share_packages import SharePackages

            s.share_packages = SharePackages(s.engine, s.shares, s.graph, lambda: getattr(s, "metrics", None),
                                             getattr(s, "agent_apps", None), s.audit)

    # MLflow behind Cairn sign-in, with models in the catalog (CAIRN_MLFLOW_URL).
    from cairn.ml.mlflow_gateway import MlflowGateway

    lake = getattr(s, "lakehouse", None)
    s.ml = MlflowGateway.from_env(
        s.engine, s.audit, s.graph,
        lake.urn if lake is not None else
        (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{'.'.join(ns)}.{t},PROD)"),
        lambda p, privilege, urn: s.authorize(p, privilege, urn, raise_on_deny=False))
    # Model monitoring: every call to a served model logged; drift from a baseline, accuracy from labels.
    if s.ml is not None:
        from cairn.ml.monitoring import ModelMonitoring

        mon_catalog = os.environ.get("CAIRN_ML_MONITOR_CATALOG") or os.environ.get("CAIRN_PIPES_CATALOG")
        read_table = None
        if mon_catalog:
            from cairn.pipelines.pipes import _settings as _mon_settings

            mon_props = _mon_settings("CAIRN_ML_MONITOR_CATALOG" if os.environ.get("CAIRN_ML_MONITOR_CATALOG")
                                      else "CAIRN_PIPES_CATALOG")

            def read_table(table: str, columns):
                from pyiceberg.catalog.rest import RestCatalog

                from cairn.ml.monitoring import MAX_BASELINE

                t = RestCatalog("cairn-ml-monitor", **mon_props).load_table(tuple(table.split(".")))
                scan = t.scan(selected_fields=tuple(columns), limit=MAX_BASELINE) if columns else t.scan(limit=MAX_BASELINE)
                return scan.to_arrow().to_pylist()

        def tell_ml(type_, title, body, recipients, payload):
            from cairn.notify.hub import Event as _Event

            if s.notify is not None:
                s.notify.publish(_Event(type=type_, title=title, body=body, recipients=recipients, payload=payload,
                                        url=f"/ml/models/{payload.get('model', '')}"))
        gw = s.ml
        s.ml_monitoring = ModelMonitoring(
            s.engine, lambda p, m: gw.may_view(p, m), lambda p, m: gw.may_manage(p, m), gw.owner, read_table,
            tell_ml, s.audit)
        gw.monitoring = s.ml_monitoring
    # Managed model serving: registered versions deployed from Cairn, scaled with their load (CAIRN_ML_SERVING_RUNTIME).
    if s.ml is not None:
        from cairn.ml.serving import ModelServing, runtime_from_env

        s.ml_serving = ModelServing(s.engine, runtime_from_env(), s.ml, s.audit)
    # Feature tables: lakehouse tables declared as ML features, in the catalog with their lineage.
    if s.graph is not None:
        from cairn.ml.features import FeatureTables

        s.feature_tables = FeatureTables(
            s.graph, lake.urn if lake is not None else
            (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{'.'.join(ns)}.{t},PROD)"),
            lambda p, privilege, urn: s.authorize(p, privilege, urn, raise_on_deny=False), s.audit)
        # Online feature store: feature tables served by entity key, synced from the lakehouse by Cairn's
        # own catalog client (CAIRN_ONLINE_FEATURES_CATALOG, or the pipes'), looked up under the table's policy.
        online_catalog = os.environ.get("CAIRN_ONLINE_FEATURES_CATALOG") or os.environ.get("CAIRN_PIPES_CATALOG")
        if online_catalog:
            from cairn.ml.online import OnlineFeatures
            from cairn.pipelines.pipes import _settings as _catalog_settings

            props = _catalog_settings("CAIRN_ONLINE_FEATURES_CATALOG" if os.environ.get("CAIRN_ONLINE_FEATURES_CATALOG")
                                      else "CAIRN_PIPES_CATALOG")

            def online_catalog_client():
                from pyiceberg.catalog.rest import RestCatalog

                return RestCatalog("cairn-online-features", **props)

            def online_policy(actor: str, urn: str):
                from cairn.governance.pdp import AuthorizeRequest, ResourceRef

                d = s.pdp.decide(AuthorizeRequest(actor=actor, privilege="VIEW_DATASET_PROFILE",
                                                  resource=ResourceRef(type="dataset", urn=urn),
                                                  environment={"channel": "online-features"}))
                if d.decision != "ALLOW":
                    return False, False, None
                return True, any(o.type == "row_filter" for o in d.obligations), masking.plan(actor, urn)
            store_url = os.environ.get("CAIRN_ONLINE_STORE_URL", "").strip()
            store_engine = None
            if store_url:
                from sqlalchemy import create_engine

                store_engine = create_engine(store_url, pool_pre_ping=True)
            s.online_features = OnlineFeatures(
                s.engine, s.feature_tables, online_catalog_client, online_policy,
                lambda p, privilege, urn: s.authorize(p, privilege, urn, raise_on_deny=False), s.audit, store_engine)
            s.feature_tables.on_remove = s.online_features._drop     # a removed feature table is not served

    # Data apps (Streamlit and other web apps) behind Cairn sign-in, each viewer governed as themselves.
    apps_secret = os.environ.get("CAIRN_APPS_SECRET") or getattr(s.settings, "service_token", "") or ""
    if apps_secret:
        from cairn.dataaccess.dataapps import DataApps

        def viewer_token(urn: str):
            tt = getattr(s, "trino_tokens", None)
            return tt.mint(urn, 600)["token"] if tt is not None else None
        from cairn.dataaccess.apps_k8s import KubeApps

        # Apps from git (CAIRN_APP_GIT_URLS: URL prefixes besides the configured repositories), and on
        # Kubernetes when CAIRN_APPS_K8S_NAMESPACE is set (a misconfiguration stops Cairn at start-up).
        s.data_apps = DataApps(s.engine, apps_secret, s.audit, viewer_token, git=getattr(s, "git", None),
                               kube=KubeApps.from_env(),
                               git_urls=[u.strip() for u in os.environ.get("CAIRN_APP_GIT_URLS", "").split(",") if u.strip()])
        # App packages (cairn.dataaccess.app_packages): apps others install on their own data; each install reads
        # through Trino as its own identity, bound tables only.
        from cairn.dataaccess.app_packages import AppPackages

        def app_identity_token(identity: str):
            tt = getattr(s, "trino_tokens", None)
            return tt.mint(identity, 600)["token"] if tt is not None else None
        s.app_packages = AppPackages(s.engine, s.data_apps,
                                     lambda actor, priv, urn: s.authorize(Principal(urn=actor, via="delegated"), priv,
                                                                          urn, raise_on_deny=False),
                                     s.audit, app_identity_token)
        if getattr(s, "trino_access", None) is not None:
            s.trino_access.app_identity = s.app_packages.identity
        # Talking to an app (cairn.dataaccess.app_chat): its declared context, the asker's own data access, the model
        # through the AI gateway as them.
        from cairn.dataaccess.app_chat import AppChat

        s.app_chat = AppChat(s.data_apps, lambda: getattr(s, "data_access", None),
                             lambda: getattr(s, "ai_gateway", None), s.authorize, s.can_view, s.graph, s.audit)

    # Fine-tuning (cairn.ml.finetune): models tuned on data read through the governed query path, as the asker.
    if os.environ.get("CAIRN_FINETUNE_MODEL_DIR"):
        from cairn.ml.finetune import FineTunes

        s.finetunes = FineTunes(
            s.engine, lambda actor, sql, conn, limit: s.data_access.query(actor, sql, conn, None, limit),
            lambda actor, priv, urn: s.authorize(Principal(urn=actor, via="delegated"), priv, urn, raise_on_deny=False),
            s.audit)

    # Delta Lake tables converted to Iceberg in place (CAIRN_DELTA_STORE / CAIRN_DELTA_CATALOG, or the pipes').
    from cairn.pipelines.delta_convert import from_env as delta_from_env

    s.delta_convert = delta_from_env(s.audit)

    # Document parsing: tables from PDFs' text layer, charts read by Cairn's multimodal model (metered, as the person).
    from cairn.pipelines.parse import ParsedDocuments

    s.documents = ParsedDocuments(s.engine, s.llm if s.llm is None or isinstance(s.llm, MeteredProvider)
                                  else MeteredProvider(s.llm, s.ai_usage), s.settings.ai.model, s.audit)
    # Vector search on lakehouse tables (the pipes' catalog, or CAIRN_VECTORS_CATALOG).
    vec_catalog = os.environ.get("CAIRN_VECTORS_CATALOG") or os.environ.get("CAIRN_PIPES_CATALOG")
    if vec_catalog:
        from cairn.ai.embeddings import HashingEmbedder
        from cairn.pipelines.pipes import _settings
        from cairn.pipelines.vectors import VectorIndexes

        def may_read(p, table: str) -> tuple[bool, bool]:
            lk = getattr(s, "lakehouse", None)
            if lk is None:
                return (s.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False), False)
            ns, _, name = table.rpartition(".")
            d = lk.decide(p.urn, [{"namespace": ns.split("."), "name": name}])["tables"][0]
            return bool(d.get("read")), bool(d.get("restricted"))
        s.vectors = VectorIndexes(s.engine, _settings("CAIRN_VECTORS_CATALOG" if os.environ.get("CAIRN_VECTORS_CATALOG")
                                                      else "CAIRN_PIPES_CATALOG"),
                                  s.semantic.embedder if s.semantic is not None else HashingEmbedder(), s.audit, may_read,
                                  # answers with sources: always through the metered, budgeted provider
                                  llm=s.llm if s.llm is None or isinstance(s.llm, MeteredProvider)
                                  else MeteredProvider(s.llm, s.ai_usage), model=s.settings.ai.model)
        if getattr(s, "trino_key", None):
            # ai_search(index, query, k) in Trino SQL (Cairn's Trino functions plugin), searched as the query's user.
            from cairn.ai.sql_search import SqlSearch

            s.sql_search = SqlSearch.from_env(s.vectors, s.ai_usage)

    # Zero-copy Parquet: files already in the store registered as Iceberg tables in place (cairn.pipelines.parquet_tables).
    pq_store = os.environ.get("CAIRN_PARQUET_STORE") or os.environ.get("CAIRN_PIPES_STORE")
    pq_catalog = os.environ.get("CAIRN_PARQUET_CATALOG") or os.environ.get("CAIRN_PIPES_CATALOG")
    if pq_store and pq_catalog:
        from cairn.pipelines.parquet_tables import READ_FILES, ParquetTables, path_urn
        from cairn.pipelines.pipes import _settings

        def may_read_files(actor: str, bucket: str, prefix: str) -> bool:
            """A volume's grant for its bucket; READ_FILES on the path (Cairn's policies) for any other."""
            vols = getattr(s, "volumes", None)
            if bucket.startswith("cairn-vol-"):
                if vols is None:
                    return False
                try:
                    return vols.access(actor, vols._vol(bucket.removeprefix("cairn-vol-"))) in ("read", "write")
                except Exception:  # noqa: BLE001 - no such volume, or it cannot be looked up: no read
                    return False
            return s.authorize(Principal(urn=actor, via="delegated"), READ_FILES, path_urn(bucket, prefix),
                               raise_on_deny=False)

        def table_urn(ns: str, t: str) -> str:
            lk = getattr(s, "lakehouse", None)
            return lk.urn([ns], t) if lk is not None else f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)"
        s.parquet_tables = ParquetTables(
            s.engine, _settings("CAIRN_PARQUET_STORE" if os.environ.get("CAIRN_PARQUET_STORE") else "CAIRN_PIPES_STORE"),
            _settings("CAIRN_PARQUET_CATALOG" if os.environ.get("CAIRN_PARQUET_CATALOG") else "CAIRN_PIPES_CATALOG"),
            s.audit, getattr(s, "graph", None), table_urn,
            [b.strip() for b in os.environ.get("CAIRN_PARQUET_BUCKETS", "").split(",") if b.strip()],
            may_read_files, getattr(s, "dataset_changes", None))

    if s.external_quality is not None and s.graph is not None:
        # dbt runs: docs, lineage and tests on the datasets they built, named as Trino names them.
        from cairn.pipelines.dbt import DbtRuns

        s.dbt_runs = DbtRuns(s.graph, s.external_quality, s.audit,
                             s.trino_access.urn if getattr(s, "trino_access", None) is not None else None)

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
                       s.settings.ai.model_nlq or s.settings.ai.model, s.audit,
                       lambda refs: s.metrics.for_tables(refs) if getattr(s, "metrics", None) is not None else [])
    # Cairn Ontology (CAIRN_ONTOLOGY_URL): the compiler, fed from the catalog and the query history; its
    # relationships ground Ask-your-data and its /validate-sql checks the SQL it writes.
    from cairn.context.ontology_compiler import OntologyCompiler

    s.ontology = OntologyCompiler.from_env(s.engine, s.graph, context=s.context,
                                           query_history=getattr(s, "query_history", None), data_access=s.data_access,
                                           audit=s.audit, agents=s.agents)
    s.ask.ontology = s.ontology
    if s.context is not None:
        from cairn.context.semantic import SemanticService

        def catalog_datasets() -> list[str]:
            from cairn.quality.auto import Planner

            return [u for u, _, _ in Planner(s.graph).datasets()] if s.graph is not None else []

        s.semantic_layer = SemanticService(s.context, s.data_access, catalog_datasets, s.audit)
    # Cairn in Excel and Google Sheets: grids for the add-ins' functions, over the same governed reads.
    from cairn.dataaccess.sheets import SheetsService

    s.sheets = SheetsService(s.data_access, lambda: getattr(s, "metrics", None), lambda: s.semantic_layer,
                             lambda: s.ask)


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
    # The lakehouse's own storage, sized every day with the warehouses (CAIRN_COST_LAKEHOUSE_USD_PER_TB_MONTH).
    from cairn.pipelines.pipes import _settings as _catalog_settings

    pipes = getattr(s, "pipes", None)
    lake_props = _catalog_settings("CAIRN_COST_LAKEHOUSE_CATALOG") or (pipes.catalog_props if pipes is not None else None)
    if lake_props:
        s.costs.lakehouse = (lake_props, pipes.urn if pipes is not None else
                             (lambda ns, t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)"))
    # Hosted signup and Stripe billing (the hosting control plane only: CAIRN_SIGNUP).
    from cairn.hosting.signup import Signup, load_config as load_signup

    signup_cfg = load_signup()           # a misconfigured control plane stops at start-up
    s.signup = Signup(s.engine, signup_cfg, audit=s.audit) if signup_cfg else None
    # Usage metering: tenants report their daily usage; the control plane bills it through Stripe's meters.
    from cairn.hosting.metering import Metering, UsageReporter
    from cairn.hosting.signup import _read as _read_secret, signups as _signups

    s.metering = None
    if s.signup is not None and signup_cfg.get("metering_key_file"):
        def customer_of(tenant: str):
            with s.engine.connect() as conn:
                row = conn.execute(_signups.select().where(_signups.c.tenant == tenant)).first()
            return row.stripe_customer if row is not None and row.status in ("active", "paid", "canceled", "failed") \
                else None
        s.metering = Metering(s.engine, _read_secret(signup_cfg["metering_key_file"]) or "", signup_cfg.get("meters") or {},
                              s.signup.stripe, customer_of, int(signup_cfg.get("settle_hours", 6)))
    s.usage_reporter = UsageReporter.from_env(s.engine, getattr(s, "licence", None))
    # Status page: components probed, uptime, incidents and maintenance (CAIRN_STATUS).
    from cairn.hosting.status import StatusPage, load_config as load_status

    status_cfg = load_status()
    s.status_page = StatusPage(s.engine, status_cfg, audit=s.audit) if status_cfg else None
    # SIEM export: the audit log pushed to Splunk HEC, syslog (RFC 5424, CEF) or HTTPS sinks (CAIRN_SIEM_SINKS).
    from cairn.common.siem import SiemExport, load_sinks

    sinks = load_sinks()                  # a misconfigured sink stops Cairn at start-up, not silently
    s.siem = SiemExport(s.engine, s.audit, sinks) if sinks and s.audit is not None else None
    # Cost budgets: a scope's spend per period against an amount, alerts at thresholds and on forecast.
    from cairn.reports.budgets import Budgets

    def tell_budget(type_, title, body, recipients, payload):
        hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload, url="/costs"))

    def warehouse_names() -> list[str]:
        raw = os.environ.get("CAIRN_WAREHOUSES", "").strip()
        if not raw:
            return []
        import json as _json

        cfg = _json.loads(open(raw).read() if not raw.startswith("{") else raw)
        return [w.get("name") for w in cfg.get("warehouses") or [] if isinstance(w, dict)]
    s.budgets = Budgets(s.engine, s.graph, tell_budget,
                        lambda p: s.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False),
                        warehouse_names, s.audit)
    # Chargeback statements per cost centre (cairn.reports.chargeback): the same figures, frozen monthly.
    from cairn.reports.chargeback import Chargeback

    s.chargeback = Chargeback(s.engine, lambda u: s.attributes.subject(u).get("groups", []), s.graph, s.audit)


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


def _build_records(s: Services) -> None:
    """Legal holds, retention that deletes and erasure with a signed proof (cairn.governance.records)."""
    if s.privacy is None:
        return
    from cairn.governance.records import Records
    from cairn.notify.hub import Event

    signer = None
    if getattr(s, "trino_key", None):
        from cairn.dataaccess.trino_auth import TrinoTokens

        signer = TrinoTokens(s.engine, s.trino_key, os.environ.get("CAIRN_TRINO_TOKEN_ISSUER", "cairn"), 3600,
                             key_name="privacy-proof")

    def proved(officer: str, rid: str) -> None:
        if s.notify is not None:
            s.notify.publish(Event(type="privacy.erasure_proved", title=f"Subject request {rid}: erased, with proof",
                                   body="Every table is done; the signed report is ready.", recipients=[officer],
                                   url=f"/governance/privacy/requests/{rid}/proof"))
    s.records = Records(s.engine, s.privacy, s.data_access, signer, s.audit, proved)
    if getattr(s, "trino_access", None) is not None:
        s.trino_access.held = s.records.held


def _build_passkeys(s: Services) -> None:
    """Passkeys and step-up (cairn.identity.passkeys): on with CAIRN_PASSKEY_RP_ID and CAIRN_PASSKEY_ORIGIN; the
    listed actions (detokenising, forgetting tokens, sharing outside) then need a fresh passkey check."""
    from cairn.identity.passkeys import Passkeys

    def tell(user: str, text: str) -> None:
        hub = getattr(s, "notify", None)
        if hub is not None:
            from cairn.notify.hub import Event

            hub.publish(Event(type="security.passkey_added", title="A passkey was added", body=text, recipients=[user]))
    s.passkeys = Passkeys(s.engine, s.audit, tell)
    if s.passkeys.enabled:
        for attr in ("tokens", "shares", "secret_scopes"):
            svc = getattr(s, attr, None)
            if svc is not None:
                svc.step_up = s.passkeys.require


def _build_posture(s: Services) -> None:
    """The security posture scan (cairn.governance.posture): Cairn's own configuration and state, checked daily."""
    from cairn.governance.posture import Posture
    from cairn.governance.residency import Residency

    def sensitive_datasets() -> int:
        """Datasets with a column classified sensitive (a PII term on a field)."""
        from cairn.governance.pii import TERM_PREFIX

        n = 0
        for row in s.graph.scroll_entities("dataset", ["schemaMetadata", "editableSchemaMetadata"]):
            fields = [*((row.get("schemaMetadata") or {}).get("fields") or []),
                      *((row.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or [])]
            if any(t.get("urn", "").startswith(TERM_PREFIX) for f in fields
                   for t in (f.get("glossaryTerms") or {}).get("terms") or []):
                n += 1
        return n

    contacts = [c.strip() for c in os.environ.get("CAIRN_SECURITY_CONTACTS", "").split(",") if c.strip()]

    def tell(findings: list[dict]) -> None:
        hub = getattr(s, "notify", None)
        if hub is None or not contacts:
            logger.warning("security posture: %d new finding(s); set CAIRN_SECURITY_CONTACTS to be told", len(findings))
            return
        from cairn.notify.hub import Event

        for f in findings:
            hub.publish(Event(type="security.posture_finding", title=f"[{f['severity']}] {f['title']}",
                              body=f["fix"], recipients=contacts, payload=f))
    s.posture = Posture(s.engine, s.settings, s.pdp, s.audit, getattr(s, "residency", None) or Residency({}, None),
                        sensitive_datasets, getattr(s, "llm", None) is not None, tell)


def _build_tokens(s: Services) -> None:
    """Reversible tokenisation (cairn.governance.tokens): on with CAIRN_TOKENS_KEY; pipes and streams tokenise the
    columns they are told to as data lands."""
    from cairn.governance.tokens import Tokens

    def may(actor: str, privilege: str, urn: str) -> bool:
        return bool(s.authorize(Principal(urn=actor, via="delegated"), privilege, urn, raise_on_deny=False))
    s.tokens = Tokens(s.engine, s.audit, may)
    for attr in ("pipes", "streams"):
        if getattr(s, attr, None) is not None:
            getattr(s, attr).tokens = s.tokens


def _wire_residency(s: Services) -> None:
    """Data residency (cairn.governance.residency): pinned namespaces read and written only by a deployment in a
    region their pin allows, and sent only to such regions. An invalid CAIRN_RESIDENCY_FILE stops Cairn here."""
    from cairn.governance.residency import Residency

    s.residency = r = Residency.from_env()
    if not r.active:
        return
    default_catalog = os.environ.get("CAIRN_LAKEHOUSE_TRINO_CATALOG", "lakehouse")

    def lake_catalog() -> str:            # the Trino catalog that is the lakehouse (the pins name its schemas)
        lake = getattr(s, "lakehouse", None)
        return getattr(lake, "trino_catalog", None) or default_catalog
    if getattr(s, "trino_access", None) is not None:
        from cairn.dataaccess.trino import COPY_OPS
        from cairn.governance.residency import CopyGuard

        s.trino_access.resident = lambda catalog, schema: catalog != lake_catalog() or r.processing_allowed(schema)
        s.residency_copies = guard = CopyGuard(s.engine, r)

        def copies(op: str, query_id, catalog: str, schema: str):
            pinned_as = schema if catalog == lake_catalog() else None
            ns = f"{catalog}.{schema}".lower()
            return guard.write(query_id, ns, pinned_as) if op in COPY_OPS else guard.read(query_id, ns, pinned_as)
        s.trino_access.copies = copies
    if getattr(s, "shares", None) is not None:
        s.shares.residency = r
    if getattr(s, "reverse_syncs", None) is not None:
        rs = s.reverse_syncs
        s.reverse_syncs.may_leave = lambda schema, region: rs.catalog != lake_catalog() or r.transfer_allowed(schema, region)
    logger.info("data residency: region %s, %d pin(s)", r.region or "(none)", len(r.pins))


def _build_consent(s: Services) -> None:
    """Consent (cairn.governance.consent), and row filters on direct warehouse connections: policy row filters and
    consent filters, which Trino applies itself for run_as_user connections."""
    import sqlglot

    from cairn.governance.consent import ConsentService
    from cairn.governance.pdp import AuthorizeRequest, ResourceRef
    from cairn.governance.privacy import load_identifiers

    def membership(actor: str) -> tuple[list[str], list[str]]:
        subject = s.attributes.subject(actor) if s.attributes is not None else {"urn": actor, "groups": []}
        personas = (s.personas.enrich_subject(subject, int(time.time() * 1000)).get("personas") or []) \
            if getattr(s, "personas", None) is not None else []
        return list(subject.get("groups") or []), list(personas)

    def resource(urn: str) -> dict:
        return s.attributes.resource(urn) if s.attributes is not None else {}

    def overflow(pid: str, state: str) -> None:
        logger.warning("consent purpose %s: %s", pid, state)
    identifiers = s.privacy.identifiers if getattr(s, "privacy", None) is not None else load_identifiers()
    s.consent = ConsentService(s.engine, resource, membership, identifiers, s.audit, overflow)

    def row_filters(actor: str, urn: str, dialect: str) -> list[str]:
        d = s.pdp.decide(AuthorizeRequest(actor=actor, privilege="VIEW_DATASET_PROFILE",
                                          resource=ResourceRef(type="dataset", urn=urn)))
        user = "'" + actor.removeprefix("urn:li:corpuser:").replace("'", "''") + "'"
        out = []
        for o in d.obligations:
            if o.type != "row_filter":
                continue
            expr = str(o.params.get("expression") or "").strip()
            if not expr:
                return ["1 = 0"]                          # a filter without an expression: nothing passes
            out.append(sqlglot.transpile(expr.replace("{user}", user), read="trino", write=dialect)[0])
        return out + s.consent.filters(actor, urn, dialect)
    if getattr(s, "data_access", None) is not None:
        s.data_access.row_filters = row_filters
    if getattr(s, "trino_access", None) is not None:
        s.trino_access.consent = lambda actor, urn, dialect, groups: s.consent.filters(actor, urn, dialect, groups)


def _build_serving(s: Services) -> None:
    """Serving tables (cairn.dataaccess.serving): lakehouse tables kept as row stores in a PostgreSQL database Cairn
    manages (CAIRN_SERVING_STORE_URL, or the database cairn_serving on the server of CAIRN_DATABASES_ADMIN_URL), read
    from Cairn's catalog (CAIRN_SERVING_CATALOG, or the pipes'), every answer decided as the data preview decides a
    read of the source table."""
    from cairn.pipelines.pipes import _settings as _catalog_settings

    env = "CAIRN_SERVING_CATALOG" if os.environ.get("CAIRN_SERVING_CATALOG") else "CAIRN_PIPES_CATALOG"
    props = _catalog_settings(env)
    store, admin = _secret_env("CAIRN_SERVING_STORE_URL"), _secret_env("CAIRN_DATABASES_ADMIN_URL")
    if not props or not (store or admin) or getattr(s, "data_access", None) is None:
        return
    from cairn.dataaccess.serving import ServingTables, ensure_database

    def catalog_client():
        from pyiceberg.catalog.rest import RestCatalog

        return RestCatalog("cairn-serving", **props)

    def store_dsn() -> str:
        return store or ensure_database(admin, os.environ.get("CAIRN_SERVING_DATABASE", "cairn_serving"))

    def urn_of(table: str) -> str:
        ns, t = table.rsplit(".", 1)
        lake = getattr(s, "lakehouse", None)
        return lake.urn(ns.split("."), t) if lake is not None else f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{table},PROD)"

    s.serving_tables = ServingTables(
        s.engine, catalog_client, store_dsn, s.data_access,
        lambda p, privilege, urn: s.authorize(p, privilege, urn, raise_on_deny=False), urn_of, s.audit,
        int(os.environ.get("CAIRN_SERVING_STATEMENT_TIMEOUT_MS", "5000")))


def _build_secret_scopes(s: Services) -> None:
    """Secret scopes (cairn.identity.secret_scopes): the routes are always there; without CAIRN_SECRETS_KEY they say 503."""
    from cairn.identity.secret_scopes import SecretScopes

    def groups_of(actor: str) -> list[str]:
        return [str(g) for g in (s.attributes.subject(actor).get("groups") or [])]

    def is_operator(actor: str) -> bool:
        return s.authorize(Principal(urn=actor, via="delegated"), "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False)
    s.secret_scopes = SecretScopes(s.engine, groups_of, is_operator, s.audit)


def _build_volumes(s: Services) -> None:
    """Governed volumes (cairn.dataaccess.volumes): the routes are always there; without a store key they say 503."""
    from cairn.dataaccess.volumes import PRIVILEGE, StoreClient, Volumes

    key = os.environ.get("CAIRN_VOLUMES_ACCESS_KEY", "") or os.environ.get("CAIRN_STORE_ADMIN_ACCESS_KEY", "")
    secret = _secret_env("CAIRN_VOLUMES_SECRET_KEY") or _secret_env("CAIRN_STORE_ADMIN_SECRET_KEY")
    url = os.environ.get("CAIRN_STORE_URL", "http://cairn-store:8333")
    store = StoreClient(url, key, secret, os.environ.get("CAIRN_STORE_REGION", "us-east-1")) if key and secret else None

    def groups_of(actor: str) -> list[str]:
        return [str(g) for g in (s.attributes.subject(actor).get("groups") or [])]

    def allowed(privilege: str):
        return lambda actor: s.authorize(Principal(urn=actor, via="delegated"), privilege, None, raise_on_deny=False)
    s.volumes = Volumes(s.engine, store, groups_of, allowed(PRIVILEGE), allowed("MANAGE_SYSTEM_OPERATIONS"), s.audit,
                        os.environ.get("CAIRN_VOLUMES_ENDPOINT", url))


def _build_storage_locations(s: Services) -> None:
    """Storage credentials and external locations (cairn.dataaccess.storage_locations): always on; their grants are
    the policy decision point's ``storage`` layer."""
    from cairn.dataaccess.storage_locations import StorageLocations

    def authorize(actor: str, privilege: str, resource: Optional[str]) -> bool:
        return bool(s.authorize(Principal(urn=actor, via="delegated"), privilege, resource, raise_on_deny=False))
    s.storage_locations = StorageLocations(s.engine, s.pdp, authorize, s.audit)


def _build_saas(s: Services) -> None:
    """SaaS connectors (cairn.pipelines.saas): Salesforce and HubSpot into Iceberg; on with a catalog to load into."""
    from cairn.pipelines.pipes import _settings
    from cairn.pipelines.saas import SaasSyncs

    catalog = _settings("CAIRN_SAAS_CATALOG") if os.environ.get("CAIRN_SAAS_CATALOG") else (
        _settings("CAIRN_PIPES_CATALOG") if os.environ.get("CAIRN_PIPES_CATALOG") else None)
    if not catalog or s.secret_scopes is None:
        return

    class _Changes:                  # built later (pipeline health): looked up when a sync loads rows
        def record(self, *a, **k):
            c = getattr(s, "dataset_changes", None)
            if c is not None:
                c.record(*a, **k)

    def urn(ns: str, t: str) -> str:
        lake = getattr(s, "lakehouse", None)
        return lake.urn([ns], t) if lake is not None else f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)"

    def notify(type_, title, body, recipients, payload):
        hub = getattr(s, "notify", None)
        if hub is not None:
            from cairn.notify.hub import Event

            hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload))
    s.saas = SaasSyncs(s.engine, catalog, s.secret_scopes.read,
                       lambda owner, u: s.authorize(Principal(urn=owner, via="delegated"), "WRITE_DATASET", u,
                                                    raise_on_deny=False),
                       urn, s.audit, _Changes(), notify)


def _build_doc_sources(s: Services) -> None:
    """Document sources (cairn.pipelines.doc_sources): SharePoint and Google Drive folders kept in a store prefix."""
    from cairn.pipelines.pipes import _settings

    if not os.environ.get("CAIRN_DOC_SOURCES_STORE") or s.secret_scopes is None:
        return
    from cairn.pipelines.doc_sources import DocumentSources

    s.doc_sources = DocumentSources(s.engine, _settings("CAIRN_DOC_SOURCES_STORE"), s.secret_scopes.read, s.audit)


def _build_reverse_etl(s: Services) -> None:
    """Reverse ETL (cairn.pipelines.reverse_etl): governed tables out to SaaS apps, read on Trino as the owner."""
    conn_name = (os.environ.get("CAIRN_REVERSE_ETL_CONNECTION") or os.environ.get("CAIRN_CHANGE_FEED_CONNECTION")
                 or os.environ.get("CAIRN_DERIVED_CONNECTION") or os.environ.get("CAIRN_MAINTENANCE_CONNECTION"))
    if not conn_name or s.secret_scopes is None or getattr(s, "data_access", None) is None:
        return
    from cairn.pipelines.reverse_etl import PRIVILEGE, ReverseSyncs

    def run_sql(owner: str, sql: str, limit: int):
        definition = (s.data_access.registry.definitions() or {}).get(conn_name) or {}
        return s.data_access.run_on_trino(definition, owner, sql, limit)

    def urn(ns: str, t: str) -> str:
        lake = getattr(s, "lakehouse", None)
        return lake.urn([ns], t) if lake is not None else f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{ns}.{t},PROD)"

    def notify(type_, title, body, recipients, payload):
        hub = getattr(s, "notify", None)
        if hub is not None:
            from cairn.notify.hub import Event

            hub.publish(Event(type=type_, title=title, body=body, recipients=recipients, payload=payload))
    s.reverse_syncs = ReverseSyncs(
        s.engine, run_sql, s.data_access.plan_for,
        lambda owner, u: s.authorize(Principal(urn=owner, via="delegated"), PRIVILEGE, u, raise_on_deny=False),
        s.secret_scopes.read, urn, os.environ.get("CAIRN_DERIVED_CATALOG", "lakehouse"), s.audit, notify)


def _build_branches(s: Services) -> None:
    """Table branches, tags, write-audit-publish and data diff (cairn.pipelines.branches): refs through the
    lakehouse catalog as Cairn (CAIRN_PIPES_CATALOG), rows on Trino as the person asking."""
    conn_name = (os.environ.get("CAIRN_BRANCHES_CONNECTION") or os.environ.get("CAIRN_DERIVED_CONNECTION")
                 or os.environ.get("CAIRN_MAINTENANCE_CONNECTION"))
    pipes = getattr(s, "pipes", None)
    if not conn_name or pipes is None or getattr(s, "data_access", None) is None:
        return
    from cairn.pipelines.branches import Branches

    def run_sql(actor: str, sql: str, limit: int):
        definition = (s.data_access.registry.definitions() or {}).get(conn_name) or {}
        return s.data_access.run_on_trino(definition, actor, sql, limit)

    def urn(ns: str, t: str) -> str:
        lake = getattr(s, "lakehouse", None)
        return lake.urn([ns], t) if lake is not None else pipes.urn(ns, t)
    s.branches = Branches(
        pipes.catalog_props, lambda actor, priv, u: s.authorize(Principal(urn=actor, via="delegated"), priv, u,
                                                                raise_on_deny=False),
        urn, run_sql, os.environ.get("CAIRN_DERIVED_CATALOG", "lakehouse"), s.audit)
    # Synthetic test data (cairn.pipelines.synthetic): statistics on Trino as the person, written as Cairn.
    from cairn.pipelines.synthetic import Synthetic

    def sensitive(u: str) -> set:
        return set((s.attributes.resource(u) or {}).get("sensitive_fields") or {})
    s.synthetic = Synthetic(pipes.catalog_props, s.branches.authorize, urn, run_sql,
                            os.environ.get("CAIRN_DERIVED_CATALOG", "lakehouse"), sensitive, s.data_access.plan_for, s.audit)


def _build_saml(s: Services) -> None:
    """SAML single sign-on, handed on as OIDC (cairn.identity.saml): on only when every setting it needs is there."""
    issuer = os.environ.get("CAIRN_SSO_ISSUER_URL", "").rstrip("/")
    passphrase = os.environ.get("CAIRN_SSO_SIGNING_PASSPHRASE", "")
    clients_file = os.environ.get("CAIRN_SSO_CLIENTS_FILE", "")
    md_url, md_file = os.environ.get("CAIRN_SAML_IDP_METADATA_URL", ""), os.environ.get("CAIRN_SAML_IDP_METADATA_FILE", "")
    if not (issuer and (md_url or md_file)):
        return
    if not passphrase or not clients_file:
        raise RuntimeError("SAML sign-in needs CAIRN_SSO_SIGNING_PASSPHRASE and CAIRN_SSO_CLIENTS_FILE")
    from cairn.dataaccess.trino_auth import TrinoTokens
    from cairn.identity.saml import SamlBridge, load_clients

    def idp_metadata() -> bytes:
        if md_file:
            with open(md_file, "rb") as f:
                return f.read()
        import httpx

        r = httpx.get(md_url, timeout=15, follow_redirects=False)
        r.raise_for_status()
        return r.content
    signer = TrinoTokens(s.engine, passphrase, issuer, 3600, key_name="sso-oidc")
    s.saml = SamlBridge(s.engine, signer, issuer, load_clients(clients_file), idp_metadata, s.audit,
                        os.environ.get("CAIRN_SAML_SP_ENTITY_ID") or None)


def _build_external_catalogs(s: Services) -> None:
    """External Iceberg catalogs (cairn.dataaccess.external_catalogs): on with Trino's access control."""
    if getattr(s, "trino_access", None) is None or s.graph is None:
        return
    from cairn.dataaccess.external_catalogs import ExternalCatalogs

    scopes = s.secret_scopes
    s.external_catalogs = ExternalCatalogs(
        s.engine, s.graph, s.trino_access, s.data_access, s.audit,
        read_secret=(scopes.read if scopes is not None and scopes.enabled else None),
        broker_url=os.environ.get("CAIRN_EXTERNAL_CATALOGS_BROKER_URL", ""),
        trino_connection=os.environ.get("CAIRN_EXTERNAL_CATALOGS_TRINO_CONNECTION", ""),
        trino_key=s.trino_key)
    s.trino_access.external_catalog = s.external_catalogs.mapping
