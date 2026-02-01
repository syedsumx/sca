"""FastAPI application factory."""

import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from codescope.api.routes import (
    analysis,
    projects,
    issues,
    duplications,
    dependencies,
    coverage,
    rules,
    git,
    export,
    aivetting,
    auth,
    cicd,
    webhooks,
    trends,
    sarif_import,
    custom_rules,
    compare,
    secrets,
    sbom,
    teams,
    remediation,
    suppressions,
    autofix,
    monorepo,
    license_policy,
)
from codescope.auth.database import AuthDatabase
from codescope.auth.middleware import get_current_user, init_auth, require_role
from codescope.auth.models import Role
from codescope.auth.tokens import TokenManager


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="CodeScope API",
        description="REST API for CodeScope Static Code Analysis",
        version="0.1.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    # ── Auth setup ──────────────────────────────────────────────
    auth_db = AuthDatabase()
    token_mgr = TokenManager()
    init_auth(auth_db, token_mgr)

    # ── CORS middleware ─────────────────────────────────────────
    cors_origins = os.environ.get(
        "CODESCOPE_CORS_ORIGINS",
        "http://localhost:3000,http://localhost:5173",
    ).split(",")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

    # ── Public routes (no auth required) ────────────────────────
    app.include_router(auth.router, prefix="/api/v1", tags=["Auth"])

    # ── Protected routes (require authentication) ───────────────
    # Viewer+ access (read-only)
    viewer_deps = [Depends(get_current_user)]
    app.include_router(
        projects.router, prefix="/api/v1", tags=["Projects"],
        dependencies=viewer_deps,
    )
    app.include_router(
        issues.router, prefix="/api/v1", tags=["Issues"],
        dependencies=viewer_deps,
    )
    app.include_router(
        duplications.router, prefix="/api/v1", tags=["Duplications"],
        dependencies=viewer_deps,
    )
    app.include_router(
        dependencies.router, prefix="/api/v1", tags=["Dependencies"],
        dependencies=viewer_deps,
    )
    app.include_router(
        coverage.router, prefix="/api/v1", tags=["Coverage"],
        dependencies=viewer_deps,
    )
    app.include_router(
        rules.router, prefix="/api/v1", tags=["Rules"],
        dependencies=viewer_deps,
    )
    app.include_router(
        git.router, prefix="/api/v1", tags=["Git"],
        dependencies=viewer_deps,
    )
    app.include_router(
        export.router, prefix="/api/v1", tags=["Export"],
        dependencies=viewer_deps,
    )
    app.include_router(
        trends.router, prefix="/api/v1", tags=["Trends"],
        dependencies=viewer_deps,
    )
    app.include_router(
        remediation.router, prefix="/api/v1", tags=["Remediation"],
        dependencies=viewer_deps,
    )
    app.include_router(
        compare.router, prefix="/api/v1", tags=["Compare"],
        dependencies=viewer_deps,
    )

    # Analyst+ access (can trigger scans and modify)
    analyst_deps = [Depends(require_role(Role.ANALYST))]
    app.include_router(
        analysis.router, prefix="/api/v1", tags=["Analysis"],
        dependencies=analyst_deps,
    )
    app.include_router(
        aivetting.router, prefix="/api/v1", tags=["AI Vetting"],
        dependencies=analyst_deps,
    )
    app.include_router(
        secrets.router, prefix="/api/v1", tags=["Secrets"],
        dependencies=analyst_deps,
    )
    app.include_router(
        sbom.router, prefix="/api/v1", tags=["SBOM"],
        dependencies=analyst_deps,
    )
    app.include_router(
        sarif_import.router, prefix="/api/v1", tags=["SARIF Import"],
        dependencies=analyst_deps,
    )
    app.include_router(
        custom_rules.router, prefix="/api/v1", tags=["Custom Rules"],
        dependencies=analyst_deps,
    )
    app.include_router(
        cicd.router, prefix="/api/v1", tags=["CI/CD"],
        dependencies=analyst_deps,
    )
    app.include_router(
        suppressions.router, prefix="/api/v1", tags=["Suppressions"],
        dependencies=analyst_deps,
    )
    app.include_router(
        autofix.router, prefix="/api/v1", tags=["Auto-Fix"],
        dependencies=analyst_deps,
    )
    app.include_router(
        monorepo.router, prefix="/api/v1", tags=["Monorepo"],
        dependencies=analyst_deps,
    )
    app.include_router(
        license_policy.router, prefix="/api/v1", tags=["License Policy"],
        dependencies=analyst_deps,
    )

    # Admin access
    admin_deps = [Depends(require_role(Role.ADMIN))]
    app.include_router(
        webhooks.router, prefix="/api/v1", tags=["Webhooks"],
        dependencies=admin_deps,
    )
    app.include_router(
        teams.router, prefix="/api/v1", tags=["Teams"],
        dependencies=admin_deps,
    )

    # ── Public endpoints ────────────────────────────────────────
    @app.get("/api/health")
    async def health_check():
        """Health check endpoint (no auth required)."""
        return {"status": "healthy", "version": "0.1.0"}

    return app


# Create default app instance
app = create_app()
