"""Projects API routes."""

from typing import Optional
from fastapi import APIRouter, HTTPException

from codescope.api.models import ProjectResponse, ProjectListResponse
from codescope.core.enums import QualityGateStatus

router = APIRouter()

# In-memory storage for demo
_projects: dict[str, dict] = {}


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(
    limit: int = 20,
    offset: int = 0,
):
    """List all projects."""
    projects = list(_projects.values())[offset:offset + limit]

    return ProjectListResponse(
        projects=[
            ProjectResponse(
                project_name=p["project_name"],
                last_analysis=p.get("last_analysis"),
                quality_gate_status=p.get("quality_gate_status"),
                bugs=p.get("bugs", 0),
                vulnerabilities=p.get("vulnerabilities", 0),
                code_smells=p.get("code_smells", 0),
                coverage=p.get("coverage"),
                duplications=p.get("duplications"),
                lines_of_code=p.get("lines_of_code", 0),
                reliability_rating=p.get("reliability_rating", "A"),
                security_rating=p.get("security_rating", "A"),
                maintainability_rating=p.get("maintainability_rating", "A"),
            )
            for p in projects
        ],
        total=len(_projects),
    )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str):
    """Get a project by ID."""
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="Project not found")

    p = _projects[project_id]

    return ProjectResponse(
        project_name=p["project_name"],
        last_analysis=p.get("last_analysis"),
        quality_gate_status=p.get("quality_gate_status"),
        bugs=p.get("bugs", 0),
        vulnerabilities=p.get("vulnerabilities", 0),
        code_smells=p.get("code_smells", 0),
        coverage=p.get("coverage"),
        duplications=p.get("duplications"),
        lines_of_code=p.get("lines_of_code", 0),
        reliability_rating=p.get("reliability_rating", "A"),
        security_rating=p.get("security_rating", "A"),
        maintainability_rating=p.get("maintainability_rating", "A"),
    )


@router.post("/projects", response_model=ProjectResponse)
async def create_project(
    project_name: str,
    path: str,
):
    """Create a new project."""
    if project_name in _projects:
        raise HTTPException(status_code=409, detail="Project already exists")

    _projects[project_name] = {
        "project_name": project_name,
        "path": path,
        "bugs": 0,
        "vulnerabilities": 0,
        "code_smells": 0,
        "lines_of_code": 0,
    }

    return ProjectResponse(
        project_name=project_name,
        bugs=0,
        vulnerabilities=0,
        code_smells=0,
        lines_of_code=0,
    )


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """Delete a project."""
    if project_id not in _projects:
        raise HTTPException(status_code=404, detail="Project not found")

    del _projects[project_id]
    return {"status": "deleted"}


@router.get("/dashboard/stats")
async def get_dashboard_stats():
    """Get dashboard statistics."""
    projects = list(_projects.values())

    total_issues = sum(
        p.get("bugs", 0) + p.get("vulnerabilities", 0) + p.get("code_smells", 0)
        for p in projects
    )

    passing = sum(
        1 for p in projects
        if p.get("quality_gate_status") == QualityGateStatus.PASSED
    )

    failing = sum(
        1 for p in projects
        if p.get("quality_gate_status") == QualityGateStatus.FAILED
    )

    return {
        "total_projects": len(projects),
        "total_issues": total_issues,
        "total_vulnerabilities": sum(p.get("vulnerabilities", 0) for p in projects),
        "projects_passing": passing,
        "projects_failing": failing,
        "recent_analyses": [],
    }
