"""AI Code Vetting API route."""

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)


class AIVetRequest(BaseModel):
    path: str
    min_confidence: float = 0.5
    category: Optional[str] = None


class AIVetFindingResponse(BaseModel):
    pattern_id: str
    name: str
    category: str
    severity: str
    message: str
    file_path: str
    start_line: int
    end_line: int
    confidence: float


class AIVetResponse(BaseModel):
    files_scanned: int
    total_findings: int
    risk_score: float
    risk_level: str
    summary: dict[str, int]
    findings: list[AIVetFindingResponse]


@router.post("/ai-vet", response_model=AIVetResponse)
async def run_ai_vetting(request: AIVetRequest):
    """Run AI code vetting analysis on a directory."""
    from codescope.analyzers.aivetting import AIVettingAnalyzer

    target = Path(request.path).resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {request.path}")

    analyzer = AIVettingAnalyzer(min_confidence=request.min_confidence)

    if target.is_file():
        findings = analyzer.analyze_file(target)
        files_scanned = 1
        summary: dict[str, int] = {}
        for f in findings:
            summary[f.category] = summary.get(f.category, 0) + 1
    else:
        report = analyzer.analyze_directory(target)
        findings = report.findings
        files_scanned = report.files_scanned
        summary = report.summary

    # Filter by category
    if request.category:
        findings = [f for f in findings if f.category == request.category]

    # Calculate risk
    severity_weights = {
        "BLOCKER": 10.0,
        "CRITICAL": 7.0,
        "MAJOR": 4.0,
        "MINOR": 1.5,
        "INFO": 0.5,
    }
    risk_score = min(
        100.0,
        sum(severity_weights.get(f.severity, 1.0) * f.confidence for f in findings),
    )
    if risk_score >= 50:
        risk_level = "HIGH"
    elif risk_score >= 20:
        risk_level = "MEDIUM"
    elif risk_score > 0:
        risk_level = "LOW"
    else:
        risk_level = "NONE"

    return AIVetResponse(
        files_scanned=files_scanned,
        total_findings=len(findings),
        risk_score=round(risk_score, 1),
        risk_level=risk_level,
        summary=summary,
        findings=[
            AIVetFindingResponse(
                pattern_id=f.pattern_id,
                name=f.name,
                category=f.category,
                severity=f.severity,
                message=f.message,
                file_path=f.file_path,
                start_line=f.start_line,
                end_line=f.end_line,
                confidence=f.confidence,
            )
            for f in findings
        ],
    )


@router.get("/ai-vet/patterns")
async def list_patterns():
    """List all available AI vetting patterns."""
    from codescope.analyzers.aivetting import BUILTIN_PATTERNS

    return [
        {
            "pattern_id": p.pattern_id,
            "name": p.name,
            "description": p.description,
            "category": p.category.value,
            "severity": p.severity.value,
            "languages": p.languages,
            "confidence": p.confidence,
        }
        for p in BUILTIN_PATTERNS
    ]
