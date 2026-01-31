"""CI/CD pipeline generation endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from codescope.cicd.generators import (
    generate_github_actions,
    generate_gitlab_ci,
    generate_jenkins,
    generate_azure_pipelines,
)

router = APIRouter()

_GENERATORS = {
    "github": generate_github_actions,
    "gitlab": generate_gitlab_ci,
    "jenkins": generate_jenkins,
    "azure": generate_azure_pipelines,
}

_FILENAMES = {
    "github": ".github/workflows/codescope.yml",
    "gitlab": ".gitlab-ci.yml",
    "jenkins": "Jenkinsfile",
    "azure": "azure-pipelines.yml",
}


class CICDRequest(BaseModel):
    project_name: str = "my-project"
    quality_gate: bool = True
    coverage_threshold: int = 80
    fail_on_vulnerabilities: bool = True
    ai_vetting: bool = False
    schedule: Optional[str] = None


@router.post("/cicd/generate/{platform}")
async def generate_pipeline(platform: str, request: CICDRequest):
    """Generate CI/CD pipeline configuration."""
    gen_fn = _GENERATORS.get(platform)
    if not gen_fn:
        raise HTTPException(400, f"Unknown platform: {platform}. Supported: {', '.join(_GENERATORS)}")

    kwargs: dict = {
        "project_name": request.project_name,
        "quality_gate": request.quality_gate,
        "coverage_threshold": request.coverage_threshold,
        "fail_on_vulnerabilities": request.fail_on_vulnerabilities,
    }
    if platform in ("github", "gitlab"):
        kwargs["ai_vetting"] = request.ai_vetting
    if platform == "github" and request.schedule:
        kwargs["schedule"] = request.schedule

    return {
        "platform": platform,
        "filename": _FILENAMES[platform],
        "content": gen_fn(**kwargs),
    }


@router.get("/cicd/platforms")
async def list_platforms():
    """List supported CI/CD platforms."""
    return [
        {"id": "github", "name": "GitHub Actions", "filename": _FILENAMES["github"]},
        {"id": "gitlab", "name": "GitLab CI", "filename": _FILENAMES["gitlab"]},
        {"id": "jenkins", "name": "Jenkins", "filename": _FILENAMES["jenkins"]},
        {"id": "azure", "name": "Azure Pipelines", "filename": _FILENAMES["azure"]},
    ]
