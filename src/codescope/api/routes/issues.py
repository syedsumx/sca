"""Issues API routes."""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from codescope.api.models import IssueResponse, IssueListResponse, LocationSchema
from codescope.core.enums import Severity, IssueType
from codescope.api.routes.analysis import _analyses

router = APIRouter()


@router.get("/analyses/{analysis_id}/issues", response_model=IssueListResponse)
async def get_issues(
    analysis_id: str,
    severity: Optional[list[Severity]] = Query(None),
    issue_type: Optional[list[IssueType]] = Query(None),
    file_path: Optional[str] = None,
    rule_id: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    """Get issues for an analysis."""
    if analysis_id not in _analyses:
        raise HTTPException(status_code=404, detail="Analysis not found")

    data = _analyses[analysis_id]
    result = data.get("result")

    if not result:
        return IssueListResponse(issues=[], total=0, page=page, page_size=page_size)

    issues = result.issues

    # Apply filters
    if severity:
        issues = [i for i in issues if i.severity in severity]

    if issue_type:
        issues = [i for i in issues if i.issue_type in issue_type]

    if file_path:
        issues = [i for i in issues if file_path in i.location.file_path]

    if rule_id:
        issues = [i for i in issues if i.rule_id == rule_id]

    total = len(issues)

    # Paginate
    start = (page - 1) * page_size
    end = start + page_size
    issues = issues[start:end]

    return IssueListResponse(
        issues=[
            IssueResponse(
                id=i.id,
                rule_id=i.rule_id,
                rule_name=i.rule_name,
                severity=i.severity,
                issue_type=i.issue_type,
                message=i.message,
                location=LocationSchema(
                    file_path=i.location.file_path,
                    start_line=i.location.start_line,
                    end_line=i.location.end_line,
                    start_column=i.location.start_column,
                    end_column=i.location.end_column,
                ),
                effort_minutes=i.effort_minutes,
                tags=i.tags,
                snippet=i.snippet,
                suggestion=i.suggestion,
            )
            for i in issues
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/analyses/{analysis_id}/issues/{issue_id}", response_model=IssueResponse)
async def get_issue(analysis_id: str, issue_id: str):
    """Get a specific issue by ID."""
    if analysis_id not in _analyses:
        raise HTTPException(status_code=404, detail="Analysis not found")

    data = _analyses[analysis_id]
    result = data.get("result")

    if not result:
        raise HTTPException(status_code=404, detail="Issue not found")

    for i in result.issues:
        if i.id == issue_id:
            return IssueResponse(
                id=i.id,
                rule_id=i.rule_id,
                rule_name=i.rule_name,
                severity=i.severity,
                issue_type=i.issue_type,
                message=i.message,
                location=LocationSchema(
                    file_path=i.location.file_path,
                    start_line=i.location.start_line,
                    end_line=i.location.end_line,
                    start_column=i.location.start_column,
                    end_column=i.location.end_column,
                ),
                effort_minutes=i.effort_minutes,
                tags=i.tags,
                snippet=i.snippet,
                suggestion=i.suggestion,
            )

    raise HTTPException(status_code=404, detail="Issue not found")


@router.get("/analyses/{analysis_id}/issues/summary")
async def get_issues_summary(analysis_id: str):
    """Get issues summary for an analysis."""
    if analysis_id not in _analyses:
        raise HTTPException(status_code=404, detail="Analysis not found")

    data = _analyses[analysis_id]
    result = data.get("result")

    if not result:
        return {
            "total": 0,
            "by_severity": {},
            "by_type": {},
        }

    issues = result.issues

    by_severity = {}
    by_type = {}

    for i in issues:
        sev = i.severity.value
        typ = i.issue_type.value

        by_severity[sev] = by_severity.get(sev, 0) + 1
        by_type[typ] = by_type.get(typ, 0) + 1

    return {
        "total": len(issues),
        "by_severity": by_severity,
        "by_type": by_type,
    }
