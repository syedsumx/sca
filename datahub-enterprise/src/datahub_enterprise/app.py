"""ASGI application factories for the DHE HTTP services.

* ``create_api_app``: quality, governance, AI and agent-fleet admin APIs (``dhe api``)
* ``create_pdp_app``: governance/PDP only, scaled independently (``dhe pdp``)
* ``create_gateway_app``: the agent protocol, exposed to private networks (``dhe gateway``)
"""

from __future__ import annotations

import logging
import time
from typing import Iterable, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from datahub_enterprise import __version__
from datahub_enterprise.common.security import AuthenticationError, Principal
from datahub_enterprise.services import Forbidden, Services

logger = logging.getLogger(__name__)

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

    _REQUESTS = Counter("dhe_http_requests_total", "HTTP requests", ["app", "method", "route", "status"])
    _LATENCY = Histogram("dhe_http_request_seconds", "HTTP latency", ["app", "route"])
except ImportError:  # pragma: no cover
    _REQUESTS = _LATENCY = None


def _principal_dependency(services: Services):
    def current_principal(request: Request) -> Principal:
        try:
            return services.identity.resolve(dict(request.headers), dict(request.cookies))
        except AuthenticationError as e:
            raise HTTPException(401, str(e), headers={"WWW-Authenticate": "Bearer"})
    return current_principal


def _authorize(services: Services):
    def authorize(principal: Principal, privilege: str, resource_urn: Optional[str], raise_on_deny: bool = True):
        try:
            return services.authorize(principal, privilege, resource_urn, raise_on_deny)
        except Forbidden as e:
            raise HTTPException(403, str(e))
    return authorize


def _base_app(name: str, services: Services) -> FastAPI:
    app = FastAPI(title=f"DataHub Enterprise — {name}", version=__version__)
    app.state.services = services

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


DEFAULT_MODULES = ("quality", "governance", "ai", "agents", "collab", "marketplace", "context", "notify", "embed")


def create_api_app(services: Services, modules: Iterable[str] = DEFAULT_MODULES) -> FastAPI:
    from datahub_enterprise.agent.api import build_router as agents_router
    from datahub_enterprise.ai.api import build_router as ai_router
    from datahub_enterprise.governance.api import build_router as governance_router
    from datahub_enterprise.quality.api import build_router as quality_router

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
    _include_active_metadata(app, services, modules, cp, authz)
    return app


def _include_active_metadata(app: FastAPI, services: Services, modules: set, cp, authz) -> None:
    from datahub_enterprise.collab.api import build_router as collab_router
    from datahub_enterprise.context.api import build_router as context_router
    from datahub_enterprise.embed.api import build_router as embed_router
    from datahub_enterprise.governance.personas_api import build_router as personas_router
    from datahub_enterprise.marketplace.api import build_router as marketplace_router
    from datahub_enterprise.notify.api import build_router as notify_router

    if "governance" in modules and services.personas is not None:
        app.include_router(personas_router(services.personas, cp, authz, services.graph))
    if "collab" in modules and services.collab is not None:
        app.include_router(collab_router(services.collab, cp, authz, services.can_view, services.summaries,
                                         services.trust, services.explicitly_denied))
    if "marketplace" in modules and services.marketplace is not None:
        app.include_router(marketplace_router(services.marketplace, cp, services.can_view,
                                              lambda p: services.subject_of(p.urn)))
    if "context" in modules and services.context is not None:
        app.include_router(context_router(services.context, cp, authz, services.can_view))
    if "notify" in modules and services.notify is not None:
        app.include_router(notify_router(services.notify, services.slack, cp, authz))
    if "embed" in modules and services.embed is not None:
        app.include_router(embed_router(services.embed, cp, services.can_view))

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
        return {
            "actor": p.urn,
            "personas": personas,
            "is_admin": services.is_admin(p.urn),
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
                "masking_proxy": bool(cfg.session_signing_key),
            },
        }


def create_pdp_app(services: Services) -> FastAPI:
    from datahub_enterprise.governance.api import build_router as governance_router

    app = _base_app("pdp", services)
    app.include_router(governance_router(services.pdp, services.policy_repo, services.workflows, services.audit,
                                         services.graph, _principal_dependency(services), _authorize(services)))
    return app


def create_gateway_app(services: Services) -> FastAPI:
    from datahub_enterprise.agent.api import build_router as agents_router

    if services.gateway is None:
        raise RuntimeError("gateway service not configured")
    app = _base_app("gateway", services)
    app.include_router(agents_router(services.gateway, admin=False, protocol=True))
    return app
