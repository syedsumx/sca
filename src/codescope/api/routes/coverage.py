"""Coverage API routes."""

from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException

from codescope.api.models import (
    CoverageResponse,
    FileCoverageResponse,
)
from codescope.analyzers.coverage.parser import CoverageReport
from codescope.analyzers.coverage.formats import (
    CoberturaParser,
    LcovParser,
    CoveragePyParser,
    JacocoParser,
)

router = APIRouter()


def detect_and_parse_coverage(report_path: Path) -> Optional[CoverageReport]:
    """Detect format and parse coverage report."""
    parsers = [
        CoberturaParser(),
        LcovParser(),
        CoveragePyParser(),
        JacocoParser(),
    ]

    for parser in parsers:
        if parser.can_parse(report_path):
            return parser.parse(report_path)

    return None


@router.get("/analyses/{analysis_id}/coverage", response_model=CoverageResponse)
async def get_coverage(analysis_id: str):
    """Get coverage data for an analysis."""
    # For now, return empty coverage if no report is available
    # In production, this would look up stored coverage data
    return CoverageResponse(
        line_coverage=0.0,
        branch_coverage=None,
        total_lines=0,
        covered_lines=0,
        uncovered_lines=0,
        files=[],
    )


@router.post("/coverage/parse", response_model=CoverageResponse)
async def parse_coverage_report(
    report_path: str,
    format: Optional[str] = None,
):
    """Parse a coverage report file."""
    path = Path(report_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Report not found: {report_path}")

    # Parse based on format or auto-detect
    report = None

    if format:
        format_lower = format.lower()
        if format_lower == "cobertura":
            parser = CoberturaParser()
        elif format_lower == "lcov":
            parser = LcovParser()
        elif format_lower in ["coverage.py", "coveragepy"]:
            parser = CoveragePyParser()
        elif format_lower == "jacoco":
            parser = JacocoParser()
        else:
            raise HTTPException(status_code=400, detail=f"Unknown format: {format}")

        if parser.can_parse(path):
            report = parser.parse(path)
    else:
        report = detect_and_parse_coverage(path)

    if not report:
        raise HTTPException(
            status_code=400,
            detail="Unable to parse coverage report. Check format."
        )

    return CoverageResponse(
        line_coverage=report.summary.line_coverage,
        branch_coverage=report.summary.branch_coverage,
        total_lines=report.summary.total_lines,
        covered_lines=report.summary.covered_lines,
        uncovered_lines=report.summary.total_lines - report.summary.covered_lines,
        files=[
            FileCoverageResponse(
                file_path=f.file_path,
                line_coverage=f.line_coverage,
                branch_coverage=f.branch_coverage,
                covered_lines=f.covered_lines,
                total_lines=f.total_lines,
                uncovered_lines=f.uncovered_lines,
            )
            for f in report.files
        ],
    )


@router.get("/coverage/file")
async def get_file_coverage(
    analysis_id: str,
    file_path: str,
):
    """Get coverage for a specific file."""
    # In production, this would look up stored coverage data
    return {
        "file_path": file_path,
        "line_coverage": 0.0,
        "uncovered_lines": [],
    }
