"""FastAPI application factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from tether import __version__
from tether.api.routes import audit, deletions, memory, models, rag, subjects
from tether.config import get_settings
from tether.deletion.service import DeletionNotFoundError
from tether.lineage.store import UnknownArtifactError

DESCRIPTION = """
Tether tracks personal data from source tables into AI systems (chunks, vectors,
training snapshots, models, agent memory), and it can carry out deletion requests
from start to finish, ending with an auditable certificate.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    stop = None
    if get_settings().run_worker_in_api:
        from tether.deletion.worker import start_background_worker

        _, stop = start_background_worker()
    yield
    if stop is not None:
        stop.set()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Tether", version=__version__, description=DESCRIPTION, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    @app.exception_handler(DeletionNotFoundError)
    async def _not_found(_: Request, exc: DeletionNotFoundError) -> JSONResponse:
        return JSONResponse(
            {"detail": f"deletion request {exc} not found"}, status.HTTP_404_NOT_FOUND
        )

    @app.exception_handler(UnknownArtifactError)
    async def _unknown(_: Request, exc: UnknownArtifactError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status.HTTP_404_NOT_FOUND)

    @app.get("/health", tags=["meta"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    for router in (subjects, deletions, models, audit, rag, memory):
        app.include_router(router.router)
    return app


app = create_app()
