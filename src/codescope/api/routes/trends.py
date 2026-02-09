"""Historical trend tracking endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from codescope.trends.store import TrendStore, TrendSnapshot

router = APIRouter()
_store: Optional[TrendStore] = None


def _get_store() -> TrendStore:
    global _store
    if _store is None:
        _store = TrendStore()
    return _store


class SnapshotCreate(BaseModel):
    project: str
    timestamp: str
    commit_sha: str = ""
    branch: str = ""
    total_issues: int = 0
    bugs: int = 0
    vulnerabilities: int = 0
    code_smells: int = 0
    blockers: int = 0
    critical: int = 0
    major: int = 0
    minor: int = 0
    coverage: float = 0.0
    duplication_pct: float = 0.0
    quality_gate: str = "none"
    extra: dict = {}


@router.post("/trends/snapshots")
async def record_snapshot(req: SnapshotCreate):
    """Record a new analysis snapshot for trend tracking."""
    snapshot = TrendSnapshot(**req.dict())
    row_id = _get_store().record(snapshot)
    return {"id": row_id, "message": "Snapshot recorded"}


@router.get("/trends/{project}")
async def get_trends(project: str, limit: int = 50, branch: Optional[str] = None):
    """Get trend data for a project (chronological order)."""
    data = _get_store().get_trends(project, limit=limit, branch=branch)
    if not data:
        raise HTTPException(404, f"No trend data for project: {project}")
    return {"project": project, "snapshots": data, "count": len(data)}


@router.get("/trends/{project}/delta")
async def get_delta(project: str):
    """Compare the two most recent snapshots — what changed."""
    delta = _get_store().get_delta(project)
    if delta is None:
        raise HTTPException(404, "Need at least 2 snapshots to compute delta")
    return {"project": project, "delta": delta}


@router.get("/trends")
async def list_trend_projects():
    """List projects that have trend data."""
    return {"projects": _get_store().get_projects()}


@router.delete("/trends/{project}")
async def delete_trend_data(project: str):
    """Delete all trend data for a project."""
    count = _get_store().delete_project(project)
    return {"message": f"Deleted {count} snapshots for {project}"}
