"""Duplications API routes."""

from pathlib import Path
from fastapi import APIRouter, HTTPException

from codescope.api.models import (
    DuplicationResponse,
    DuplicationGroupResponse,
    DuplicateBlockResponse,
)
from codescope.analyzers.duplication import DuplicationAnalyzer
from codescope.api.routes.analysis import _analyses

router = APIRouter()


@router.get("/analyses/{analysis_id}/duplications", response_model=DuplicationResponse)
async def get_duplications(analysis_id: str):
    """Get duplication analysis for a project."""
    if analysis_id not in _analyses:
        raise HTTPException(status_code=404, detail="Analysis not found")

    data = _analyses[analysis_id]
    path = Path(data.get("path", "."))

    # Run duplication analysis
    analyzer = DuplicationAnalyzer()

    # Get all source files
    files = []
    for ext in ["*.py", "*.js", "*.ts", "*.java", "*.go", "*.rb", "*.php"]:
        files.extend(path.rglob(ext))

    result = analyzer.analyze(files)

    return DuplicationResponse(
        total_duplicated_lines=result.total_duplicated_lines,
        total_duplicated_blocks=result.total_duplicated_blocks,
        duplication_percentage=result.duplication_percentage,
        groups=[
            DuplicationGroupResponse(
                id=f"dup-{i}",
                fingerprint=g.fingerprint,
                token_count=g.token_count,
                line_count=g.line_count,
                blocks=[
                    DuplicateBlockResponse(
                        file_path=b.file_path,
                        start_line=b.start_line,
                        end_line=b.end_line,
                        lines=b.lines,
                    )
                    for b in g.blocks
                ],
            )
            for i, g in enumerate(result.groups)
        ],
    )


@router.post("/duplications/analyze", response_model=DuplicationResponse)
async def analyze_duplications(
    path: str,
    min_lines: int = 6,
    min_tokens: int = 50,
):
    """Run duplication analysis on a path."""
    project_path = Path(path)
    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    analyzer = DuplicationAnalyzer(
        min_block_lines=min_lines,
        min_block_tokens=min_tokens,
    )

    # Get all source files
    files = []
    for ext in ["*.py", "*.js", "*.ts", "*.java", "*.go", "*.rb", "*.php"]:
        files.extend(project_path.rglob(ext))

    result = analyzer.analyze(files)

    return DuplicationResponse(
        total_duplicated_lines=result.total_duplicated_lines,
        total_duplicated_blocks=result.total_duplicated_blocks,
        duplication_percentage=result.duplication_percentage,
        groups=[
            DuplicationGroupResponse(
                id=f"dup-{i}",
                fingerprint=g.fingerprint,
                token_count=g.token_count,
                line_count=g.line_count,
                blocks=[
                    DuplicateBlockResponse(
                        file_path=b.file_path,
                        start_line=b.start_line,
                        end_line=b.end_line,
                        lines=b.lines,
                    )
                    for b in g.blocks
                ],
            )
            for i, g in enumerate(result.groups)
        ],
    )
