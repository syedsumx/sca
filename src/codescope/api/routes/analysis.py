"""Analysis API routes."""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, BackgroundTasks

from codescope.api.models import (
    AnalysisRequest,
    AnalysisResponse,
    AnalysisSummary,
    QualityGateResponse,
    QualityGateConditionResponse,
    MetricsResponse,
)
from codescope.analyzers.orchestrator import AnalysisOrchestrator
from codescope.core.config import AnalysisConfig
from codescope.core.enums import QualityGateStatus

router = APIRouter()

# In-memory storage for demo (use database in production)
_analyses: dict[str, dict] = {}


@router.post("/analyses", response_model=dict)
async def create_analysis(
    request: AnalysisRequest,
    background_tasks: BackgroundTasks,
):
    """Trigger a new analysis."""
    path = Path(request.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {request.path}")

    analysis_id = str(uuid.uuid4())[:8]

    # Store pending analysis
    _analyses[analysis_id] = {
        "analysis_id": analysis_id,
        "project_name": path.name,
        "path": str(path),
        "status": "PENDING",
        "timestamp": datetime.now(),
    }

    # Run analysis in background
    background_tasks.add_task(run_analysis, analysis_id, path, request)

    return {"analysis_id": analysis_id, "status": "PENDING"}


async def run_analysis(analysis_id: str, path: Path, request: AnalysisRequest):
    """Run analysis in background."""
    try:
        _analyses[analysis_id]["status"] = "RUNNING"

        analysis_config = AnalysisConfig(
            project_path=path,
            exclude_patterns=request.exclude_patterns or [],
        )

        orchestrator = AnalysisOrchestrator(analysis_config.to_config())
        result = orchestrator.analyze()

        _analyses[analysis_id].update({
            "status": "SUCCESS" if result.status.value == "completed" else "FAILURE",
            "result": result,
            "duration_seconds": result.duration_seconds,
            "issues_count": len(result.issues),
        })
    except Exception as e:
        _analyses[analysis_id].update({
            "status": "FAILURE",
            "error": str(e),
        })


@router.get("/analyses/{analysis_id}", response_model=AnalysisResponse)
async def get_analysis(analysis_id: str):
    """Get analysis results by ID."""
    if analysis_id not in _analyses:
        raise HTTPException(status_code=404, detail="Analysis not found")

    data = _analyses[analysis_id]

    if data["status"] == "PENDING" or data["status"] == "RUNNING":
        raise HTTPException(status_code=202, detail=f"Analysis {data['status']}")

    if data["status"] == "FAILURE":
        raise HTTPException(
            status_code=500,
            detail=data.get("error", "Analysis failed")
        )

    result = data.get("result")
    if not result:
        raise HTTPException(status_code=500, detail="No result available")

    return AnalysisResponse(
        analysis_id=analysis_id,
        project_name=data["project_name"],
        timestamp=data["timestamp"],
        duration_seconds=data.get("duration_seconds", 0),
        status=data["status"],
        issues_count=len(result.issues),
        quality_gate=QualityGateResponse(
            status=result.quality_gate_status or QualityGateStatus.PASSED,
            conditions=[],
        ),
        metrics=MetricsResponse(
            total_files=result.files_analyzed,
            total_lines=sum(m.lines_of_code for m in result.file_metrics),
            total_code_lines=sum(m.lines_of_code for m in result.file_metrics),
            total_comment_lines=sum(m.comment_lines for m in result.file_metrics),
            total_blank_lines=sum(m.blank_lines for m in result.file_metrics),
            average_complexity=sum(m.complexity for m in result.file_metrics) / max(len(result.file_metrics), 1),
            total_functions=sum(m.functions for m in result.file_metrics),
            total_classes=sum(m.classes for m in result.file_metrics),
            languages={},
        ),
    )


@router.get("/analyses", response_model=list[AnalysisSummary])
async def list_analyses(
    project_name: Optional[str] = None,
    limit: int = 10,
):
    """List recent analyses."""
    analyses = list(_analyses.values())

    if project_name:
        analyses = [a for a in analyses if a["project_name"] == project_name]

    # Sort by timestamp descending
    analyses.sort(key=lambda x: x["timestamp"], reverse=True)

    return [
        AnalysisSummary(
            analysis_id=a["analysis_id"],
            project_name=a["project_name"],
            timestamp=a["timestamp"],
            duration_seconds=a.get("duration_seconds", 0),
            status=a["status"],
            issues_count=a.get("issues_count", 0),
            quality_gate_status=QualityGateStatus.PASSED,
        )
        for a in analyses[:limit]
    ]


@router.delete("/analyses/{analysis_id}")
async def delete_analysis(analysis_id: str):
    """Delete an analysis."""
    if analysis_id not in _analyses:
        raise HTTPException(status_code=404, detail="Analysis not found")

    del _analyses[analysis_id]
    return {"status": "deleted"}
