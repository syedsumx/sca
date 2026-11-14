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


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    urns: list[str] = Field(default_factory=list, max_length=6)   # empty: Cairn picks the datasets
    run: bool = True                                               # false: write the SQL, do not run it
    limit: int = Field(default=200, ge=1, le=1000)


def check_agent_sql(svc: DataAccessService, intents, p: Principal, sql: str, connection: Optional[str],
                    urn: Optional[str]) -> None:
    """An agent's SQL is checked against its declared intent: by statement kind, and the tables it reads (403)."""
    if intents is None or p.via != "agent":
        return
    from cairn.identity.intents import IntentDenied

    try:
        read = svc.urns_of(sql, connection or svc.source_for(urn)[0].connection)
    except Exception:  # noqa: BLE001 - unreadable SQL is refused below by its kind, or by the query itself
        read = []
    try:
        intents.check_sql(p, sql, [*read, *([urn] if urn else [])])
    except IntentDenied as e:
        raise HTTPException(403, f"this agent may not do this ({e})")


def build_router(svc: DataAccessService, current_principal, ask=None, can_view=None, ai_actor=None,
                 intents=None) -> APIRouter:
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
        check_agent_sql(svc, intents, p, body.sql, body.connection, body.urn)
        return _wrap(lambda: svc.query(p.urn, body.sql, body.connection, body.urn, body.limit))

    @router.post("/ask")
    def ask_data(body: AskIn, p: Principal = Depends(current_principal)):
        """Answer a question with read-only SQL over the caller's datasets, run under their permissions."""
        from cairn.dataaccess.ask import AskError

        if ask is None or not ask.configured:
            raise HTTPException(503, "asking questions of data is not configured (no LLM provider)")
        if ai_actor is not None:
            ai_actor(p)
        try:
            return ask.ask(p.urn, body.question, can_view(p), body.urns, body.run, body.limit)
        except AskError as e:
            raise HTTPException(e.status, str(e))

    return router
