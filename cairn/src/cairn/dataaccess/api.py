"""``/data``: preview a table and run read-only SQL, authorized per table and masked per caller."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from cairn.common.security import Principal
from cairn.dataaccess.service import DataAccessError, DataAccessService


class QueryIn(BaseModel):
    sql: str = Field(min_length=1, max_length=20_000)
    connection: Optional[str] = None
    urn: Optional[str] = None            # run on the connection that serves this dataset
    limit: int = Field(default=200, ge=1, le=1000)


def build_router(svc: DataAccessService, current_principal) -> APIRouter:
    router = APIRouter(prefix="/data", tags=["data"])

    def _wrap(fn):
        try:
            return fn()
        except DataAccessError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/sources")
    def sources(p: Principal = Depends(current_principal)):
        """Connections that serve catalog datasets (names and platforms only)."""
        return [{"connection": s.connection, "platform": s.platform, "database": s.database} for s in svc.sources()]

    @router.get("/preview")
    def preview(urn: str = Query(...), limit: int = Query(100, ge=1, le=1000), p: Principal = Depends(current_principal)):
        return _wrap(lambda: svc.preview(p.urn, urn, limit))

    @router.post("/query")
    def query(body: QueryIn, p: Principal = Depends(current_principal)):
        if not body.connection and not body.urn:
            raise HTTPException(422, "give a connection or a dataset urn")
        return _wrap(lambda: svc.query(p.urn, body.sql, body.connection, body.urn, body.limit))

    return router
