"""ASGI application factories for the Cairn HTTP services.

* ``create_api_app``: quality, governance, AI and agent-fleet admin APIs (``cairn api``)
* ``create_pdp_app``: governance/PDP only, scaled independently (``cairn pdp``)
* ``create_gateway_app``: the agent protocol, exposed to private networks (``cairn gateway``)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Iterable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from cairn import __version__
from cairn.common.security import AuthenticationError, Principal
from cairn.services import Forbidden, Services

logger = logging.getLogger(__name__)

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    _REQUESTS = Counter("cairn_http_requests_total", "HTTP requests", ["app", "method", "route", "status"])
    _LATENCY = Histogram("cairn_http_request_seconds", "HTTP latency", ["app", "route"])
except ImportError:  # pragma: no cover
    _REQUESTS = _LATENCY = None


def _principal_dependency(services: Services):
    from cairn.common.licence import LicenceRefused

    def current_principal(request: Request) -> Principal:
        try:
            principal = services.identity.resolve(dict(request.headers), dict(request.cookies))
        except AuthenticationError as e:
            raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"})
        if principal.via == "agent":
            from cairn.common.licence import READ_POSTS
            from cairn.identity.agents import required_scope

            route = getattr(request.scope.get("route"), "path", request.url.path)
            need = required_scope(request.method, route, READ_POSTS)
            if need is None or need not in principal.scopes:
                why = "not allowed for agents" if need is None else f"needs scope {need}"
                raise HTTPException(403, f"this agent may not do this ({why})")
            from cairn.identity.agents import AgentError

            try:
                services.agents.admit(principal.agent)
            except AgentError as e:
                raise HTTPException(e.status, str(e), headers={"Retry-After": "60"})
        if getattr(services, "limits", None) is not None and not principal.is_service:
            verdict = services.limits.admit_user(principal.urn)
            if verdict is not None and not verdict[0]:
                _ok, limit, _remaining, retry = verdict
                raise HTTPException(429, f"you are over your limit of {limit} requests a minute", headers={
                    "Retry-After": str(retry), "X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Scope": "user"})
        if services.licence is not None:
            route = getattr(request.scope.get("route"), "path", request.url.path)
            try:
                services.licence.check(principal.urn, principal.is_service or principal.via == "extension",
                                       request.method, route)
            except LicenceRefused as e:
                raise HTTPException(402, str(e))
        return principal
    return current_principal


def _authorize(services: Services):
    def authorize(principal: Principal, privilege: str, resource_urn: Optional[str], raise_on_deny: bool = True):
        try:
            return services.authorize(principal, privilege, resource_urn, raise_on_deny)
        except Forbidden as e:
            raise HTTPException(403, str(e))
    return authorize


class _AgentAttribution:
    """Pure ASGI: name the agent behind an agent credential for everything the request records
    (audit entries), before routing, so the endpoint's worker thread sees it too."""

    def __init__(self, app, services: Services):
        self.app = app
        self.services = services

    async def __call__(self, scope, receive, send):
        agent = None
        if scope["type"] == "http" and self.services.agents is not None:
            auth = next((v.decode() for k, v in scope.get("headers", []) if k == b"authorization"), "")
            if auth[:7].lower() == "bearer " and auth[7:].strip().startswith(("ca_", "cat_")):
                try:
                    grant = self.services.agents.resolve(auth[7:].strip())
                except Exception:  # noqa: BLE001 - paused: the request is refused when it is authenticated
                    grant = None
                agent = grant.agent if grant else None
        if agent is None:
            await self.app(scope, receive, send)
            return
        from cairn.identity.agents import current_agent

        token = current_agent.set(agent)
        try:
            await self.app(scope, receive, send)
        finally:
            current_agent.reset(token)


def _base_app(name: str, services: Services) -> FastAPI:
    from cairn.common.tracing import setup_tracing

    app = FastAPI(title=f"Cairn — {name}", version=__version__)
    app.state.services = services
    setup_tracing(app, name, getattr(services, "engine", None))
    app.add_middleware(_AgentAttribution, services=services)
    if getattr(services, "limits", None) is not None:      # outermost: the IP allowlist and the tenant's quota
        from cairn.common.limits import LimitsMiddleware

        app.add_middleware(LimitsMiddleware, limits=services.limits)

    @app.middleware("http")
    async def metrics(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        if _REQUESTS is not None:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            _REQUESTS.labels(name, request.method, route, response.status_code).inc()
            _LATENCY.labels(name, route).observe(time.perf_counter() - started)
        # Whatever is in front (the Compose edge, an ingress) or not: API answers are not sniffed, not
        # cached by browsers or proxies, and send no referrer. Framing is left to the edge (embeds are framed).
        h = response.headers
        h.setdefault("x-content-type-options", "nosniff")
        h.setdefault("referrer-policy", "no-referrer")
        if not request.url.path.startswith("/embed/v1/sdk"):
            h.setdefault("cache-control", "no-store")
        return response

    @app.exception_handler(Forbidden)
    async def forbidden(_request: Request, exc: Forbidden):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok", "service": name, "version": __version__}

    @app.get("/metrics", include_in_schema=False)
    def prometheus():
        if _REQUESTS is None:
            return PlainTextResponse("prometheus_client not installed\n", status_code=501)
        return PlainTextResponse(generate_latest().decode(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/me")
    def me(p: Principal = Depends(_principal_dependency(services))):
        return {"urn": p.urn, "via": p.via}

    return app


DEFAULT_MODULES = ("quality", "governance", "ai", "agents", "collab", "marketplace", "context", "notify", "embed",
                   "scim")


def create_api_app(services: Services, modules: Iterable[str] = DEFAULT_MODULES) -> FastAPI:
    from cairn.agent.api import build_router as agents_router
    from cairn.ai.api import build_router as ai_router
    from cairn.governance.api import build_router as governance_router
    from cairn.quality.api import build_router as quality_router

    _map_tiles()                       # a bad tile server setting stops the API at start, not each page later
    app = _base_app("api", services)
    cp, authz = _principal_dependency(services), _authorize(services)
    modules = set(modules)
    if "quality" in modules:
        app.include_router(quality_router(services.quality, cp, authz, services.external_quality))
    if "governance" in modules:
        app.include_router(governance_router(services.pdp, services.policy_repo, services.workflows,
                                             services.audit, services.graph, cp, authz))
    if "ai" in modules:
        app.include_router(ai_router(services, cp))
    if "agents" in modules and services.gateway is not None:
        app.include_router(agents_router(services.gateway, cp, authz, admin=True, protocol=False))
    if "scim" in modules and services.graph is not None:
        from cairn.identity.scim import ScimService, install_error_handler
        from cairn.identity.scim import build_router as scim_router

        public = services.settings.active.public_url.rstrip("/")
        scim = ScimService(services.engine, _provisioning_graph(services), services.audit,
                           f"{public}/cairn/api/scim/v2", on_change=services.attributes.invalidate)
        app.include_router(scim_router(scim))
        install_error_handler(app)
    if services.ai_systems is not None:
        from cairn.governance.ai_systems_api import build_router as ai_systems_router

        app.include_router(ai_systems_router(services.ai_systems, cp, authz))
    if services.extensions is not None:
        from cairn.extensions.api import build_router as extensions_router

        app.include_router(extensions_router(services.extensions, cp, authz))
    if services.licence is not None:
        from cairn.common.licence_api import build_router as licence_router

        app.include_router(licence_router(services.licence, cp, authz))
    if services.slos is not None and services.marketplace is not None:
        from cairn.marketplace.slos_api import build_router as slos_router

        app.include_router(slos_router(services.slos, services.marketplace, cp, services.can_view))
    if services.history is not None:
        from cairn.governance.history_api import build_router as history_router

        app.include_router(history_router(services.history, cp, services.can_view, authz, services.is_admin))
    if services.store_admin is not None:
        from cairn.storage.store_api import build_router as storage_router

        app.include_router(storage_router(services.store_admin, cp, services.is_admin, services.audit))
    if services.workspaces is not None:
        from cairn.governance.workspaces_api import build_router as workspaces_router

        app.include_router(workspaces_router(services.workspaces, cp, services.is_admin))
    if services.privacy is not None:
        from cairn.governance.privacy_api import build_router as privacy_router

        app.include_router(privacy_router(services.privacy, cp, authz))
        if getattr(services, "records", None) is not None:      # holds, retention that deletes, erasure
            from cairn.governance.records import build_records_router

            app.include_router(build_records_router(services.records, cp, authz))
    if services.sources is not None:
        from cairn.sources.api import build_router as sources_router

        app.include_router(sources_router(services.sources, cp, authz))
    if services.trino_access is not None:
        from cairn.dataaccess.trino import build_trino_router

        app.include_router(build_trino_router(services.trino_access, services.trino_key, audit=services.audit,
                                              lineage=services.openlineage, tokens=services.trino_tokens,
                                              current_principal=cp, outbox=services.trino_outbox,
                                              usage=services.trino_usage, catalog_sync=services.trino_catalog,
                                              authorize=authz, oauth=services.trino_oauth,
                                              groups_of=lambda urn: services.attributes.subject(urn).get("groups", []),
                                              history=services.query_history,
                                              functions=getattr(services, "trino_functions", None)))
        if services.query_history is not None:
            from cairn.dataaccess.query_history import build_queries_router

            app.include_router(build_queries_router(services.query_history, cp, authz))
    if services.lakehouse is not None and services.trino_tokens is not None:
        from cairn.dataaccess.lakehouse import build_lakehouse_router

        app.include_router(build_lakehouse_router(services.lakehouse, services.lakehouse_sessions, cp, services.audit,
                                                  authz, getattr(services, "dataset_changes", None)))
    if getattr(services, "change_feed", None) is not None:
        from cairn.pipelines.change_feed import build_change_feed_router

        app.include_router(build_change_feed_router(services.change_feed, cp))
    if getattr(services, "sql_workflows", None) is not None:
        from cairn.sqlworkspace.workflows import build_workflows_router

        app.include_router(build_workflows_router(services.sql_workflows, cp))
    if getattr(services, "vectors", None) is not None:
        from cairn.pipelines.vectors import build_vectors_router

        app.include_router(build_vectors_router(services.vectors, cp, authz))
    if getattr(services, "feature_tables", None) is not None:
        from cairn.ml.features import build_features_router

        app.include_router(build_features_router(services.feature_tables, cp))
    if getattr(services, "online_features", None) is not None:
        from cairn.ml.online import build_online_router

        app.include_router(build_online_router(services.online_features, cp))
    if getattr(services, "clean_rooms", None) is not None:
        from cairn.dataaccess.cleanrooms import build_clean_rooms_router

        app.include_router(build_clean_rooms_router(services.clean_rooms, cp))
    if getattr(services, "data_apps", None) is not None:
        from cairn.dataaccess.dataapps import build_apps_router

        app.include_router(build_apps_router(services.data_apps, services))
    if getattr(services, "procedures", None) is not None:
        from cairn.sqlworkspace.procedures import build_procedures_router

        app.include_router(build_procedures_router(services.procedures, cp))
    if getattr(services, "git_folders", None) is not None:
        from cairn.sqlworkspace.gitfolders import build_git_folders_router

        app.include_router(build_git_folders_router(services.git_folders, cp))
    if getattr(services, "delta_convert", None) is not None:
        from cairn.pipelines.delta_convert import build_delta_router

        app.include_router(build_delta_router(services.delta_convert, cp, authz))
    if getattr(services, "notebooks", None) is not None:
        from cairn.identity.notebooks import build_notebooks_router

        gf = getattr(services, "git_folders", None)
        app.include_router(build_notebooks_router(services.notebooks, services.notebook_catalog, cp,
                                                  gf.notebook_saved if gf is not None else None))
    if getattr(services, "ml_monitoring", None) is not None:   # before the MLflow pass-through, which takes /v1/ml/*
        from cairn.ml.monitoring import build_monitoring_router

        app.include_router(build_monitoring_router(services.ml_monitoring, cp))
    if getattr(services, "ml_serving", None) is not None:      # before the MLflow pass-through, which takes /v1/ml/*
        from cairn.ml.serving import build_serving_router

        app.include_router(build_serving_router(services.ml_serving, cp))
        if services.ml_serving.runtime is not None:

            @app.on_event("startup")
            async def _model_autoscaler():                # sample load and scale deployments, in this process
                import asyncio

                services.ml_serving._task = asyncio.get_running_loop().create_task(services.ml_serving.run())
    if getattr(services, "custom_agents", None) is not None:
        from cairn.ai.custom_agents import build_custom_agents_router

        app.include_router(build_custom_agents_router(services.custom_agents, cp))
    if getattr(services, "ml", None) is not None:
        from cairn.ml.mlflow_gateway import build_ml_router

        app.include_router(build_ml_router(services.ml, cp))
    if getattr(services, "shares", None) is not None:
        from cairn.dataaccess.shares import build_shares_router

        app.include_router(build_shares_router(services.shares, cp, authz,
                                               lambda urn: services.attributes.subject(urn).get("groups", [])))
    if services.dataset_changes is not None:
        from cairn.pipelines.changes import build_changes_router

        ta = services.trino_access
        app.include_router(build_changes_router(services.dataset_changes, cp, authz,
                                                ta.urn if ta is not None else None))
    if services.workload is not None:
        from cairn.identity.workload import build_workload_router

        app.include_router(build_workload_router(services.workload, cp, authz))
    if services.dbt_runs is not None:
        from cairn.pipelines.dbt import build_dbt_router

        app.include_router(build_dbt_router(services.dbt_runs, cp, authz))
    if services.pipes is not None:
        from cairn.pipelines.pipes import build_pipes_router

        app.include_router(build_pipes_router(services.pipes, cp, authz))
    from cairn.dataaccess.warehouse_status import WarehouseStatus, build_warehouses_router

    warehouse_status = WarehouseStatus.from_env()
    if warehouse_status is not None:
        app.include_router(build_warehouses_router(warehouse_status, cp, authz, services.audit,
                                                   getattr(services, "costs", None), services.trino_key))
    if getattr(services, "sql_ai", None) is not None and services.trino_key:
        from cairn.ai.sql_gateway import build_sql_ai_router

        app.include_router(build_sql_ai_router(services.sql_ai, services.trino_key))
    if services.bundle_state is not None:
        from cairn.iac.state import build_bundles_router

        app.include_router(build_bundles_router(services.bundle_state, cp, authz))
    if services.derived is not None:
        from cairn.pipelines.derived import build_derived_router

        app.include_router(build_derived_router(services.derived, cp, authz))
    if services.streams is not None:
        from cairn.pipelines.streams import build_ingest_router, build_streams_router

        app.include_router(build_streams_router(services.streams, cp, authz))
        app.include_router(build_ingest_router(services.streams, cp, authz))
    if services.maintenance is not None:
        from cairn.pipelines.maintenance import build_maintenance_router

        app.include_router(build_maintenance_router(services.maintenance, cp, authz))
    if os.environ.get("CAIRN_SPARK_HISTORY_URL"):
        from cairn.dataaccess.spark_history import SparkHistory, build_spark_history_router

        def is_operator(p) -> bool:
            try:
                authz(p, "MANAGE_SYSTEM_OPERATIONS", None)
                return True
            except HTTPException:
                return False

        app.include_router(build_spark_history_router(SparkHistory(os.environ["CAIRN_SPARK_HISTORY_URL"]), cp,
                                                      is_operator))
    if services.openlineage is not None:
        from cairn.sources.api import build_openlineage_router

        sessions = getattr(services, "lakehouse_sessions", None)
        app.include_router(build_openlineage_router(services.openlineage, cp, authz,
                                                    sessions.lineage_user if sessions is not None else None))
    if services.git is not None:
        from cairn.sources.api import build_git_router

        app.include_router(build_git_router(services.git, cp, authz))
    if services.playbooks is not None:
        from cairn.ai.playbooks_api import build_router as playbooks_router

        app.include_router(playbooks_router(services.playbooks, cp, authz))
    if services.agents is not None:
        from cairn.identity.agents_api import build_router as agents_iam_router

        app.include_router(agents_iam_router(services.agents, services, cp, authz))
    _include_active_metadata(app, services, modules, cp, authz)
    return app


def _provisioning_graph(services: Services):
    """SCIM writes people and groups as cairn-provisioner, never as the control-plane account (which
    must not be able to change group membership). Its token sits next to the control plane's."""
    core = services.settings.core
    path = os.environ.get("CAIRN_SCIM_CORE_TOKEN_FILE") or (
        os.path.join(os.path.dirname(core.token_file), "provisioner_token") if core.token_file else None)
    if not path or not core.url:
        return services.graph            # tests and single-process demos
    from cairn.common.core import CoreClient

    return CoreClient(core.url, None, core.timeout_seconds, token_file=path)


def _include_active_metadata(app: FastAPI, services: Services, modules: set, cp, authz) -> None:
    from cairn.collab.api import build_router as collab_router
    from cairn.context.api import build_router as context_router
    from cairn.embed.api import build_router as embed_router
    from cairn.governance.personas_api import build_router as personas_router
    from cairn.marketplace.api import build_router as marketplace_router
    from cairn.notify.api import build_router as notify_router

    if "governance" in modules and services.personas is not None:
        app.include_router(personas_router(services.personas, cp, authz, services.graph))
    if "governance" in modules and services.forms is not None:
        from cairn.governance.forms_api import build_router as forms_router

        app.include_router(forms_router(services.forms, cp, services.authorize, services.can_view,
                                        services.subject_of))
    if "governance" in modules and services.bulk is not None and services.catalog is not None:
        from cairn.governance.bulk_api import build_router as bulk_router

        app.include_router(bulk_router(
            services.bulk, cp, services.authorize, services.can_view,
            lambda query, limit: services.catalog.search(query, ["dataset"], count=limit),
            lambda urn: services.attributes.resource(urn).get("classification") or []))
    if "quality" in modules and services.contracts is not None:
        from cairn.quality.contracts_api import build_router as contracts_router

        app.include_router(contracts_router(services.contracts, cp, services.authorize, services.can_view))
    if "ai" in modules and services.steward is not None:
        from cairn.ai.steward_api import build_router as steward_router

        app.include_router(steward_router(services.steward, cp, services.authorize, services.can_view,
                                          services.summaries))
    if "notify" in modules and services.ticketing is not None:
        from cairn.notify.ticketing_api import build_router as ticketing_router

        app.include_router(ticketing_router(services.ticketing, cp, services.authorize))
    if getattr(services, "limits", None) is not None:
        from cairn.common.limits import build_limits_router

        app.include_router(build_limits_router(services.limits, cp, authz))
    if getattr(services, "secret_scopes", None) is not None:   # secrets that notebooks and jobs read as the person
        from cairn.identity.secret_scopes import build_secret_scopes_router

        app.include_router(build_secret_scopes_router(services.secret_scopes, cp))
    if getattr(services, "reverse_syncs", None) is not None:  # reverse ETL: governed tables out to SaaS apps
        from cairn.pipelines.reverse_etl import build_reverse_router

        app.include_router(build_reverse_router(services.reverse_syncs, cp))
    if getattr(services, "saas", None) is not None:          # SaaS connectors: Salesforce, HubSpot into Iceberg
        from cairn.pipelines.saas import build_saas_router

        app.include_router(build_saas_router(services.saas, cp))
    if getattr(services, "volumes", None) is not None:       # governed volumes: files under Cairn's grants
        from cairn.dataaccess.volumes import build_volumes_router

        app.include_router(build_volumes_router(services.volumes, cp))
    if getattr(services, "saml", None) is not None:          # SAML sign-in, handed on as OIDC (public routes)
        from cairn.identity.saml import build_sso_router

        app.include_router(build_sso_router(services.saml))
    if "quality" in modules and services.data_access is not None:
        from cairn.dataaccess.api import build_router as data_router

        app.include_router(data_router(services.data_access, cp, services.ask, services.can_view, services.ai_actor))
        if services.sql_workspace is not None:
            from cairn.sqlworkspace import build_router as sql_router

            if getattr(services, "workbooks", None) is not None:   # /sql/workbooks before /sql/{...} routes
                from cairn.sqlworkspace.workbooks import build_workbooks_router

                app.include_router(build_workbooks_router(services.workbooks, cp))
            if getattr(services, "report_schedules", None) is not None:
                from cairn.sqlworkspace.reports import build_reports_router

                app.include_router(build_reports_router(services.report_schedules, cp))
            if getattr(services, "bi_actions", None) is not None:
                from cairn.sqlworkspace.actions import build_actions_router

                app.include_router(build_actions_router(services.bi_actions, cp))
            if getattr(services, "builder", None) is not None:
                from cairn.sqlworkspace.builder import build_builder_router

                app.include_router(build_builder_router(services.builder, cp, services.can_view))
            if getattr(services, "metrics", None) is not None:
                from cairn.sqlworkspace.metrics import build_metrics_router

                app.include_router(build_metrics_router(services.metrics, cp))
            if getattr(services, "input_tables", None) is not None:
                from cairn.sqlworkspace.input_tables import build_input_tables_router

                app.include_router(build_input_tables_router(
                    services.input_tables, cp,
                    lambda p: services.authorize(p, "MANAGE_SYSTEM_OPERATIONS", None, raise_on_deny=False)))
            app.include_router(sql_router(services.sql_workspace, cp, services.can_view))
            if services.sql_boards is not None:
                from cairn.sqlworkspace.boards import build_boards_router

                app.include_router(build_boards_router(services.sql_boards, cp))
                if getattr(services, "bi_collab", None) is not None:
                    from cairn.sqlworkspace.collab import build_collab_router
                    from cairn.sqlworkspace.sharing import _table as _bi_table

                    def _owner_of(kind: str, aid: str) -> str:
                        t = _bi_table(kind)
                        with services.engine.connect() as conn:
                            return conn.execute(t.select().with_only_columns(t.c.owner).where(t.c.id == aid)).scalar() or ""
                    app.include_router(build_collab_router(services.bi_collab, cp, _owner_of))
                if getattr(services, "metric_alerts", None) is not None:
                    from cairn.sqlworkspace.metric_alerts import build_metric_alerts_router

                    app.include_router(build_metric_alerts_router(services.metric_alerts, cp))
                if getattr(services, "jobs", None) is not None:
                    from cairn.pipelines.jobs import build_jobs_router

                    app.include_router(build_jobs_router(services.jobs, cp))
                if getattr(services, "bi_versions", None) is not None:
                    from cairn.sqlworkspace.versions import build_versions_router

                    app.include_router(build_versions_router(services.bi_versions, cp))
                if getattr(services, "bi_catalog", None) is not None:
                    from cairn.sqlworkspace.bi_catalog import build_bi_catalog_router

                    app.include_router(build_bi_catalog_router(services.bi_catalog, cp))
                if getattr(services, "bi_sharing", None) is not None:
                    from cairn.sqlworkspace.sharing import build_sharing_router

                    app.include_router(build_sharing_router(services.bi_sharing, cp))
    if "governance" in modules and services.reports is not None:
        from cairn.reports.api import build_router as reports_router

        app.include_router(reports_router(services.reports, cp, services.authorize, services.can_view,
                                          services.summaries, services.costs,
                                          lambda u: [str(g) for g in (services.attributes.subject(u).get("groups") or [])]))
    if getattr(services, "signup", None) is not None:      # public: signing up, Stripe's webhook, the portal
        from cairn.hosting.signup import build_signup_router

        app.include_router(build_signup_router(services.signup))
    if getattr(services, "status_page", None) is not None:   # public: /status, /status.json, the feed
        from cairn.hosting.status import build_status_router

        app.include_router(build_status_router(services.status_page, cp, services.authorize))
    if getattr(services, "metering", None) is not None:   # tenants' usage reports (signed), operators' view
        from cairn.hosting.metering import build_metering_router

        app.include_router(build_metering_router(services.metering, cp, services.authorize))
    if getattr(services, "siem", None) is not None:
        from cairn.common.siem import build_siem_router

        app.include_router(build_siem_router(services.siem, cp, services.authorize))
    if getattr(services, "budgets", None) is not None:
        from cairn.reports.budgets import build_budgets_router

        app.include_router(build_budgets_router(services.budgets, cp))
    if "governance" in modules and services.automations is not None:
        from cairn.governance.automations_api import build_router as automations_router

        app.include_router(automations_router(services.automations, cp, services.authorize))
    if "governance" in modules and services.tag_sync is not None:
        from cairn.governance.tag_sync_api import build_router as tag_sync_router

        app.include_router(tag_sync_router(services.tag_sync, cp, services.authorize))
    if "collab" in modules and services.collab is not None:
        app.include_router(collab_router(services.collab, cp, authz, services.can_view, services.summaries,
                                         services.trust, services.explicitly_denied))
    if "marketplace" in modules and services.marketplace is not None:
        app.include_router(marketplace_router(services.marketplace, cp, services.can_view,
                                              lambda p: services.subject_of(p.urn)))
        if services.publisher is not None:
            from cairn.marketplace.publish_api import build_router as publish_router

            app.include_router(publish_router(services.publisher, services.marketplace, cp, authz))
    if "context" in modules and services.context is not None:
        app.include_router(context_router(services.context, cp, authz, services.can_view, services.semantic_layer,
                                      services.knowledge))
    if "notify" in modules and services.notify is not None:
        app.include_router(notify_router(services.notify, services.slack, cp, authz,
                                         services.slack_events, services.teams_bot))
    if getattr(services, "embedded_dashboards", None) is not None:   # portals' users, signed by the portal
        from cairn.embed.dashboards import build_embedded_dashboards_router

        app.include_router(build_embedded_dashboards_router(services.embedded_dashboards))
    if "embed" in modules and services.embed is not None:
        app.include_router(embed_router(services.embed, cp, services.can_view))

    def _allowed(p: Principal, privilege: str) -> bool:
        try:
            return services.authorize(p, privilege, None, raise_on_deny=False)
        except Exception:  # noqa: BLE001 - a UI hint; the endpoints enforce
            return False

    @app.get("/ui/config")
    def ui_config(p: Principal = Depends(cp)):
        """Feature flags + caller context for the consumer UI (one round-trip on mount)."""
        cfg = services.settings.active
        personas = []
        if services.personas is not None:
            try:
                ids = services.subject_of(p.urn).get("personas", [])
                personas = [{"id": i, "name": getattr(services.personas.get_persona(i), "name", i)} for i in ids]
            except Exception:
                logger.exception("persona lookup failed for %s", p.urn)
        display_name = p.urn.split(":", 3)[-1]
        try:
            info = services.attributes.subject(p.urn).get("attributes", {})
            display_name = info.get("display_name") or display_name
        except Exception:
            pass
        return {
            "actor": p.urn,
            "user": {"urn": p.urn, "display_name": display_name},
            "personas": personas,
            "slack_enabled": services.slack is not None,
            "teams_enabled": bool(cfg.teams_webhook_url),
            "embed_allowed_origins": cfg.embed_allowed_origins,
            "is_admin": services.is_admin(p.urn),
            "can_manage_agents": services.gateway is not None and _allowed(p, "MANAGE_INGESTION"),
            "can_manage_sources": services.sources is not None and _allowed(p, "MANAGE_INGESTION"),
            "storage_enabled": services.store_admin is not None,
            "workspaces": services.workspaces.mine(p.urn) if services.workspaces is not None else [],
            "gateway_url": services.settings.gateway.public_url,
            "public_url": cfg.public_url,
            "map_tiles": _map_tiles(),
            "map_attribution": os.environ.get("CAIRN_MAP_ATTRIBUTION", "")[:200] or None,
            "features": {
                "assistant": services.assistant is not None,
                "assistant_llm": services.llm is not None,
                "marketplace": services.marketplace is not None,
                "context": services.context is not None,
                "semantic_search": services.semantic is not None,
                "slack": services.slack is not None,
                "teams": bool(cfg.teams_webhook_url),
                "email": bool(cfg.smtp_url),
                "embeds": bool(cfg.embed_signing_secret),
                "masking_proxy": cfg.masking_proxy_enabled or bool(cfg.session_signing_key),
            },
        }


def _map_tiles() -> Optional[str]:
    """The tile server map charts draw on (``CAIRN_MAP_TILES_URL``, ``https://…/{z}/{x}/{y}.png``), or none: map
    charts then draw their points on a plain grid, and browsers fetch nothing from elsewhere."""
    url = os.environ.get("CAIRN_MAP_TILES_URL", "").strip()
    if not url:
        return None
    if not url.startswith("https://") or not all(p in url for p in ("{z}", "{x}", "{y}")) or any(c in url for c in "\"'<> "):
        raise ValueError("CAIRN_MAP_TILES_URL: an https:// URL with {z}, {x} and {y} in it")
    return url


def create_pdp_app(services: Services) -> FastAPI:
    from cairn.governance.api import build_router as governance_router

    app = _base_app("pdp", services)
    app.include_router(governance_router(services.pdp, services.policy_repo, services.workflows, services.audit,
                                         services.graph, _principal_dependency(services), _authorize(services)))
    return app


def create_gateway_app(services: Services) -> FastAPI:
    from cairn.agent.api import build_router as agents_router

    if services.gateway is None:
        raise RuntimeError("gateway service not configured")
    app = _base_app("gateway", services)
    app.include_router(agents_router(services.gateway, admin=False, protocol=True))
    # The gateway faces private networks directly: cap request bodies before anything reads them.
    app.add_middleware(BodyLimit, max_bytes=int(os.environ.get("CAIRN_GATEWAY_MAX_BODY_BYTES", 32 << 20)))
    return app


class BodyLimit:
    """ASGI middleware: 413 for request bodies over ``max_bytes`` (declared or streamed)."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            return await _too_large(send)
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited, send)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send) -> None:
    body = b'{"detail":"request body too large"}'
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
