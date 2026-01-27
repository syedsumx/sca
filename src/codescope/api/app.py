"""FastAPI application factory."""

from fastapi import FastAPI
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
)


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

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Configure appropriately for production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Include routers
    app.include_router(analysis.router, prefix="/api/v1", tags=["Analysis"])
    app.include_router(projects.router, prefix="/api/v1", tags=["Projects"])
    app.include_router(issues.router, prefix="/api/v1", tags=["Issues"])
    app.include_router(duplications.router, prefix="/api/v1", tags=["Duplications"])
    app.include_router(dependencies.router, prefix="/api/v1", tags=["Dependencies"])
    app.include_router(coverage.router, prefix="/api/v1", tags=["Coverage"])
    app.include_router(rules.router, prefix="/api/v1", tags=["Rules"])
    app.include_router(git.router, prefix="/api/v1", tags=["Git"])

    @app.get("/api/health")
    async def health_check():
        """Health check endpoint."""
        return {"status": "healthy", "version": "0.1.0"}

    return app


# Create default app instance
app = create_app()
