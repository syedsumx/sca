"""Suppression management API routes."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from codescope.suppress.engine import (
    Suppression,
    SuppressionEngine,
    SuppressionSource,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class SuppressionCreate(BaseModel):
    """Request body for creating a new suppression."""

    rule_id: str = Field(..., description="Rule ID to suppress, or '*' for all rules")
    file_pattern: str = Field(
        "**/*",
        description="Glob pattern for matching file paths",
    )
    line: int | None = Field(None, description="Specific line number, or null for entire file")
    reason: str = Field("", description="Justification for the suppression")
    author: str = Field("", description="Who created the suppression")
    expires_at: str | None = Field(
        None,
        description="Optional ISO-8601 expiry timestamp",
    )


class SuppressionResponse(BaseModel):
    """Single suppression in API responses."""

    id: str
    rule_id: str
    file_pattern: str
    line: int | None
    reason: str
    author: str
    created_at: str
    expires_at: str | None
    source: str
    is_active: bool


class SuppressionListResponse(BaseModel):
    """Paginated list of suppressions."""

    suppressions: list[SuppressionResponse]
    total: int


class SuppressionCheckRequest(BaseModel):
    """Request body for checking whether an issue would be suppressed."""

    rule_id: str
    file_path: str
    line: int | None = None


class SuppressionCheckResponse(BaseModel):
    """Result of a suppression check."""

    suppressed: bool
    suppression: SuppressionResponse | None = None


class CleanupResponse(BaseModel):
    """Result of the expired-suppression cleanup."""

    removed: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_engine() -> SuppressionEngine:
    """Build a :class:`SuppressionEngine` for the current project root.

    The project root is resolved from the ``CODESCOPE_PROJECT_ROOT``
    environment variable, falling back to the current working directory.
    """
    project_root = Path(
        os.environ.get("CODESCOPE_PROJECT_ROOT", os.getcwd()),
    ).resolve()
    engine = SuppressionEngine(project_root)
    # Eagerly load file-based sources so the engine is ready for queries
    engine.load_ignorefile()
    return engine


def _to_response(s: Suppression) -> SuppressionResponse:
    """Convert a domain-level :class:`Suppression` to an API response."""
    return SuppressionResponse(
        id=s.id,
        rule_id=s.rule_id,
        file_pattern=s.file_pattern,
        line=s.line,
        reason=s.reason,
        author=s.author,
        created_at=s.created_at.isoformat(),
        expires_at=s.expires_at.isoformat() if s.expires_at else None,
        source=s.source.value,
        is_active=s.is_active,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/suppressions", response_model=SuppressionListResponse)
async def list_suppressions(
    active_only: bool = Query(False, description="Return only active (non-expired) suppressions"),
    rule_id: Optional[str] = Query(None, description="Filter by rule ID"),
    file_path: Optional[str] = Query(None, description="Filter by file path (exact or glob match)"),
) -> SuppressionListResponse:
    """List suppressions with optional filters."""
    engine = _get_engine()
    store = engine.store

    if rule_id:
        suppressions = store.find_by_rule(rule_id)
    elif file_path:
        suppressions = store.find_by_file(file_path)
    else:
        suppressions = store.list_active() if active_only else store.list_all()

    # If both rule_id and active_only are set, post-filter for active
    if rule_id and active_only:
        suppressions = [s for s in suppressions if s.is_active]

    # If file_path was combined with rule_id, we already filtered by rule;
    # apply file filter as a post-step
    if rule_id and file_path:
        from fnmatch import fnmatch

        suppressions = [
            s for s in suppressions
            if fnmatch(file_path, s.file_pattern) or s.file_pattern == file_path
        ]

    return SuppressionListResponse(
        suppressions=[_to_response(s) for s in suppressions],
        total=len(suppressions),
    )


@router.post("/suppressions", response_model=SuppressionResponse, status_code=201)
async def create_suppression(body: SuppressionCreate) -> SuppressionResponse:
    """Create a new API-sourced suppression."""
    expires_at: datetime | None = None
    if body.expires_at:
        try:
            expires_at = datetime.fromisoformat(body.expires_at)
        except (ValueError, TypeError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid expires_at format: {exc}",
            ) from exc

    suppression = Suppression(
        rule_id=body.rule_id,
        file_pattern=body.file_pattern,
        line=body.line,
        reason=body.reason,
        author=body.author,
        expires_at=expires_at,
        source=SuppressionSource.API,
    )

    engine = _get_engine()
    engine.store.add(suppression)

    return _to_response(suppression)


@router.delete("/suppressions/{suppression_id}", status_code=200)
async def delete_suppression(suppression_id: str) -> dict[str, Any]:
    """Remove a suppression by ID."""
    engine = _get_engine()
    removed = engine.store.remove(suppression_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Suppression not found")
    return {"detail": "Suppression deleted", "id": suppression_id}


@router.get("/suppressions/expired", response_model=SuppressionListResponse)
async def list_expired_suppressions() -> SuppressionListResponse:
    """Return all expired suppressions."""
    engine = _get_engine()
    expired = engine.store.list_expired()
    return SuppressionListResponse(
        suppressions=[_to_response(s) for s in expired],
        total=len(expired),
    )


@router.post("/suppressions/cleanup", response_model=CleanupResponse)
async def cleanup_expired() -> CleanupResponse:
    """Remove all expired suppressions from the store."""
    engine = _get_engine()
    count = engine.store.clear_expired()
    return CleanupResponse(removed=count)


@router.post("/suppressions/check", response_model=SuppressionCheckResponse)
async def check_suppression(body: SuppressionCheckRequest) -> SuppressionCheckResponse:
    """Check whether a hypothetical issue would be suppressed.

    Evaluates against all suppression sources (inline, ignorefile, config,
    and the persistent store).
    """
    from codescope.core.enums import IssueType, Severity
    from codescope.core.models import Issue, Location

    engine = _get_engine()

    # Build a synthetic issue to test against
    issue = Issue(
        rule_id=body.rule_id,
        rule_name="",
        message="",
        location=Location(
            file_path=Path(body.file_path),
            start_line=body.line or 1,
            end_line=body.line or 1,
        ),
        severity=Severity.INFO,
        issue_type=IssueType.CODE_SMELL,
    )

    suppression = engine.get_suppression_for(issue)
    if suppression:
        return SuppressionCheckResponse(
            suppressed=True,
            suppression=_to_response(suppression),
        )
    return SuppressionCheckResponse(suppressed=False)
