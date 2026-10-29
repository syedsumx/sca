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

    @app.middleware("http")
    async def metrics(request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        if _REQUESTS is not None:
            route = getattr(request.scope.get("route"), "path", "unmatched")
            _REQUESTS.labels(name, request.method, route, response.status_code).inc()
            _LATENCY.labels(name, route).observe(time.perf_counter() - started)
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

    app = _base_app("api", services)
    cp, authz = _principal_dependency(services), _authorize(services)
    modules = set(modules)
    if "quality" in modules:
        app.include_router(quality_router(services.quality, cp, authz))
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
    if "quality" in modules and services.data_access is not None:
        from cairn.dataaccess.api import build_router as data_router

        app.include_router(data_router(services.data_access, cp, services.ask, services.can_view, services.ai_actor))
    if "governance" in modules and services.reports is not None:
        from cairn.reports.api import build_router as reports_router

        app.include_router(reports_router(services.reports, cp, services.authorize, services.can_view,
                                          services.summaries, services.costs))
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
        app.include_router(context_router(services.context, cp, authz, services.can_view, services.semantic_layer))
    if "notify" in modules and services.notify is not None:
        app.include_router(notify_router(services.notify, services.slack, cp, authz,
                                         services.slack_events, services.teams_bot))
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
            "gateway_url": services.settings.gateway.public_url,
            "public_url": cfg.public_url,
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
