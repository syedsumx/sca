"""Secret scanning endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from codescope.analyzers.secrets.scanner import SecretScanner

router = APIRouter()


class SecretScanRequest(BaseModel):
    path: str = "."
    scan_git_history: bool = False
    max_commits: int = 50


@router.post("/secrets/scan")
async def scan_secrets(req: SecretScanRequest):
    """Scan a directory for hardcoded secrets."""
    scanner = SecretScanner()
    findings = scanner.scan_directory(req.path)

    if req.scan_git_history:
        git_findings = scanner.scan_git_history(req.path, max_commits=req.max_commits)
        findings.extend(git_findings)

    by_severity: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_rule[f.rule_name] = by_rule.get(f.rule_name, 0) + 1

    return {
        "total": len(findings),
        "by_severity": by_severity,
        "by_rule": by_rule,
        "findings": [f.to_dict() for f in findings],
    }


@router.get("/secrets/patterns")
async def list_secret_patterns():
    """List all secret detection patterns."""
    from codescope.analyzers.secrets.scanner import _SECRET_PATTERNS
    return [
        {"id": p["id"], "name": p["name"], "severity": p["severity"]}
        for p in _SECRET_PATTERNS
    ]
