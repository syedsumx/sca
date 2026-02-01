"""API routes for Monorepo & Workspace support."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from codescope.monorepo.detector import MonorepoDetector
from codescope.monorepo.scanner import MonorepoScanner

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class DetectRequest(BaseModel):
    """Request body for workspace detection."""

    project_path: str = Field(..., description="Absolute path to the project root.")


class ScanRequest(BaseModel):
    """Request body for a full monorepo scan."""

    project_path: str = Field(..., description="Absolute path to the project root.")
    project_names: list[str] | None = Field(
        default=None,
        description="Optional list of sub-project names to scan. Scans all if omitted.",
    )
    parallel: bool = Field(
        default=False,
        description="Scan sub-projects in parallel when True.",
    )


class ScanChangedRequest(BaseModel):
    """Request body for scanning only changed sub-projects."""

    project_path: str = Field(..., description="Absolute path to the project root.")
    base_branch: str = Field(
        default="main",
        description="Git branch to diff against.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_project_path(raw_path: str) -> Path:
    """Resolve and validate the project path."""
    path = Path(raw_path).resolve()
    if not path.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Project path does not exist or is not a directory: {raw_path}",
        )
    return path


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/monorepo/detect", summary="Detect workspace type")
async def detect_workspace(body: DetectRequest) -> dict[str, Any]:
    """Detect the monorepo / workspace type for the given project path."""
    project_path = _validate_project_path(body.project_path)
    detector = MonorepoDetector()
    workspace = detector.detect(project_path)
    return workspace.to_dict()


@router.post("/monorepo/scan", summary="Scan monorepo")
async def scan_monorepo(body: ScanRequest) -> dict[str, Any]:
    """Scan all (or selected) sub-projects in a monorepo."""
    project_path = _validate_project_path(body.project_path)
    scanner = MonorepoScanner(project_path)
    result = scanner.scan(
        project_names=body.project_names,
        parallel=body.parallel,
    )
    return result.to_dict()


@router.post("/monorepo/scan-changed", summary="Scan changed projects")
async def scan_changed(body: ScanChangedRequest) -> dict[str, Any]:
    """Scan only the sub-projects that have changes relative to a base branch."""
    project_path = _validate_project_path(body.project_path)
    scanner = MonorepoScanner(project_path)
    result = scanner.scan_changed(base_branch=body.base_branch)
    return result.to_dict()


@router.get("/monorepo/projects", summary="List sub-projects")
async def list_projects(
    project_path: str = Query(..., description="Absolute path to the project root."),
) -> dict[str, Any]:
    """List detected sub-projects for the given project path."""
    path = _validate_project_path(project_path)
    detector = MonorepoDetector()
    workspace = detector.detect(path)
    return {
        "workspace_type": workspace.workspace_type.value,
        "total_projects": workspace.total_projects,
        "projects": [
            {
                "name": p.name,
                "path": str(p.path),
                "language": p.language,
                "has_config": p.has_config,
                "dependencies": p.dependencies,
            }
            for p in workspace.projects
        ],
    }
