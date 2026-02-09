"""Auto-Fix / Code Suggestions API endpoints."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from codescope.autofix.engine import AutoFixEngine, FixDifficulty
from codescope.core.enums import IssueType, Severity
from codescope.core.models import Issue, Location

router = APIRouter()
_engine = AutoFixEngine()

# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class LocationPayload(BaseModel):
    file_path: str
    start_line: int
    end_line: int
    start_column: int = 0
    end_column: int = 0
    snippet: str = ""


class IssuePayload(BaseModel):
    rule_id: str
    rule_name: str = ""
    message: str = ""
    location: LocationPayload
    severity: str = "MAJOR"
    issue_type: str = "VULNERABILITY"


class SuggestRequest(BaseModel):
    issues: list[IssuePayload]


class SuggestionPayload(BaseModel):
    rule_id: str
    title: str = ""
    description: str = ""
    original_code: str
    suggested_code: str
    file_path: str
    start_line: int
    end_line: int
    difficulty: str = "EASY"
    confidence: float = 0.8
    is_safe_to_apply: bool = False


class ApplyRequest(BaseModel):
    suggestions: list[SuggestionPayload]
    dry_run: bool = Field(default=True, description="When True, report what would change without modifying files.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload_to_issue(payload: IssuePayload) -> Issue:
    """Convert an API payload into a core Issue model."""
    loc = Location(
        file_path=Path(payload.location.file_path),
        start_line=payload.location.start_line,
        end_line=payload.location.end_line,
        start_column=payload.location.start_column,
        end_column=payload.location.end_column,
        snippet=payload.location.snippet,
    )

    try:
        severity = Severity(payload.severity.upper())
    except ValueError:
        severity = Severity.MAJOR

    try:
        issue_type = IssueType(payload.issue_type.upper())
    except ValueError:
        issue_type = IssueType.VULNERABILITY

    return Issue(
        rule_id=payload.rule_id,
        rule_name=payload.rule_name or payload.rule_id,
        message=payload.message,
        location=loc,
        severity=severity,
        issue_type=issue_type,
    )


def _payload_to_suggestion(payload: SuggestionPayload):
    """Convert an API payload into a FixSuggestion."""
    from codescope.autofix.engine import FixSuggestion

    try:
        difficulty = FixDifficulty(payload.difficulty.upper())
    except ValueError:
        difficulty = FixDifficulty.EASY

    return FixSuggestion(
        rule_id=payload.rule_id,
        title=payload.title,
        description=payload.description,
        original_code=payload.original_code,
        suggested_code=payload.suggested_code,
        file_path=payload.file_path,
        start_line=payload.start_line,
        end_line=payload.end_line,
        difficulty=difficulty,
        confidence=payload.confidence,
        is_safe_to_apply=payload.is_safe_to_apply,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/autofix/suggest")
async def suggest_fixes(req: SuggestRequest):
    """Generate auto-fix suggestions for a list of issues.

    Accepts issue objects and returns concrete code-change suggestions
    for every issue that has a registered fixer.
    """
    issues = [_payload_to_issue(p) for p in req.issues]
    result = _engine.suggest_fixes(issues)
    return result.to_dict()


@router.post("/autofix/apply")
async def apply_fixes(req: ApplyRequest):
    """Apply a list of fix suggestions to source files.

    Set ``dry_run: false`` to actually modify files on disk.  By default
    the endpoint only reports what *would* be changed.
    """
    suggestions = [_payload_to_suggestion(p) for p in req.suggestions]
    result = _engine.apply_fixes(suggestions, dry_run=req.dry_run)
    return result.to_dict()


@router.get("/autofix/fixers")
async def list_fixers():
    """Return metadata about every auto-fixer that is currently registered."""
    fixers = _engine.get_available_fixers()
    return {
        "total": len(fixers),
        "fixers": fixers,
    }
