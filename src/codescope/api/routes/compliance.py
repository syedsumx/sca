"""Compliance & Policy Engine API routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from codescope.compliance import ComplianceEngine

router = APIRouter()


class ComplianceRequest(BaseModel):
    frameworks: list[str] | None = None
    issues: list[dict] | None = None
    project_path: str | None = None


@router.get("/compliance/frameworks")
async def list_frameworks():
    """List available compliance frameworks."""
    return {"frameworks": ComplianceEngine.available_frameworks()}


@router.post("/compliance/evaluate")
async def evaluate_compliance(request: ComplianceRequest):
    """Evaluate issues against compliance frameworks."""
    engine = ComplianceEngine(framework_ids=request.frameworks)

    if request.issues:
        report = engine.evaluate_issues(request.issues)
        return report.to_dict()

    if request.project_path:
        from pathlib import Path
        from codescope.analyzers import analyze_path
        from codescope.core.config import Config

        scan_path = Path(request.project_path)
        if not scan_path.exists():
            raise HTTPException(status_code=404, detail="Project path not found")

        results = analyze_path(scan_path, Config())
        report = engine.evaluate(results)
        return report.to_dict()

    raise HTTPException(status_code=400, detail="Provide either 'issues' or 'project_path'")
