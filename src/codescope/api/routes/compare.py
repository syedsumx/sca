"""PR diff analysis — compare two analysis snapshots for new vs fixed issues."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

# In-memory analysis results cache (keyed by analysis_id)
# In production this would read from the analysis DB
_analysis_cache: dict[str, dict] = {}


class CompareRequest(BaseModel):
    """Compare two analyses by their IDs."""
    base_id: str  # older / target branch analysis
    head_id: str  # newer / PR branch analysis


class AnalysisUpload(BaseModel):
    """Upload analysis results for comparison."""
    analysis_id: str
    project: str = ""
    branch: str = ""
    commit_sha: str = ""
    issues: list[dict] = []


def _issue_fingerprint(issue: dict) -> str:
    """Create a stable fingerprint for an issue to detect new vs fixed."""
    rule = issue.get("rule_id", issue.get("rule", ""))
    loc = issue.get("location", {})
    file_path = loc.get("file", issue.get("file", ""))
    line = loc.get("start_line", issue.get("line", 0))
    msg = issue.get("message", "")[:80]
    return f"{rule}::{file_path}::{line}::{msg}"


def _issue_file_fingerprint(issue: dict) -> str:
    """Fingerprint without line numbers (for moved-code detection)."""
    rule = issue.get("rule_id", issue.get("rule", ""))
    file_path = (issue.get("location", {}).get("file", "") or issue.get("file", ""))
    msg = issue.get("message", "")[:80]
    return f"{rule}::{file_path}::{msg}"


@router.post("/compare/upload")
async def upload_analysis(req: AnalysisUpload):
    """Upload analysis results for later comparison."""
    _analysis_cache[req.analysis_id] = req.dict()
    return {"message": f"Uploaded {len(req.issues)} issues as '{req.analysis_id}'"}


@router.post("/compare")
async def compare_analyses(req: CompareRequest):
    """Compare two analyses and return new, fixed, and unchanged issues."""
    base = _analysis_cache.get(req.base_id)
    head = _analysis_cache.get(req.head_id)

    if not base:
        raise HTTPException(404, f"Base analysis '{req.base_id}' not found. Upload it first via /compare/upload.")
    if not head:
        raise HTTPException(404, f"Head analysis '{req.head_id}' not found. Upload it first via /compare/upload.")

    base_issues = base.get("issues", [])
    head_issues = head.get("issues", [])

    # Build fingerprint maps
    base_fps = {_issue_fingerprint(i): i for i in base_issues}
    head_fps = {_issue_fingerprint(i): i for i in head_issues}

    # Also build relaxed fingerprints (without line numbers)
    base_file_fps = {}
    for i in base_issues:
        fp = _issue_file_fingerprint(i)
        base_file_fps.setdefault(fp, []).append(i)

    new_issues = []
    unchanged = []
    for fp, issue in head_fps.items():
        if fp in base_fps:
            unchanged.append(issue)
        else:
            # Check relaxed match (same rule+file+msg, different line = probably moved)
            relaxed = _issue_file_fingerprint(issue)
            if relaxed in base_file_fps:
                unchanged.append(issue)
            else:
                new_issues.append(issue)

    fixed_issues = []
    head_file_fps = {}
    for i in head_issues:
        fp = _issue_file_fingerprint(i)
        head_file_fps.setdefault(fp, []).append(i)

    for fp, issue in base_fps.items():
        if fp not in head_fps:
            relaxed = _issue_file_fingerprint(issue)
            if relaxed not in head_file_fps:
                fixed_issues.append(issue)

    # Severity breakdown for new issues
    new_by_sev: dict[str, int] = {}
    for i in new_issues:
        s = i.get("severity", "UNKNOWN")
        new_by_sev[s] = new_by_sev.get(s, 0) + 1

    new_by_type: dict[str, int] = {}
    for i in new_issues:
        t = i.get("type", "UNKNOWN")
        new_by_type[t] = new_by_type.get(t, 0) + 1

    return {
        "base_id": req.base_id,
        "head_id": req.head_id,
        "summary": {
            "base_total": len(base_issues),
            "head_total": len(head_issues),
            "new_issues": len(new_issues),
            "fixed_issues": len(fixed_issues),
            "unchanged": len(unchanged),
            "net_change": len(new_issues) - len(fixed_issues),
        },
        "new_by_severity": new_by_sev,
        "new_by_type": new_by_type,
        "new_issues": new_issues,
        "fixed_issues": fixed_issues,
    }


@router.get("/compare/analyses")
async def list_uploaded_analyses():
    """List all uploaded analysis snapshots available for comparison."""
    return [
        {
            "analysis_id": k,
            "project": v.get("project", ""),
            "branch": v.get("branch", ""),
            "commit_sha": v.get("commit_sha", ""),
            "issue_count": len(v.get("issues", [])),
        }
        for k, v in _analysis_cache.items()
    ]
