"""Remediation suggestion endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from codescope.remediation.suggestions import RemediationEngine

router = APIRouter()
_engine = RemediationEngine()


@router.get("/remediation/{rule_id}")
async def get_remediation(rule_id: str):
    """Get remediation suggestion for a specific rule."""
    rem = _engine.get(rule_id)
    if not rem:
        raise HTTPException(404, f"No remediation found for rule: {rule_id}")
    return rem.to_dict()


@router.get("/remediation")
async def list_remediations():
    """List all available remediation suggestions."""
    return _engine.list_all()


class BatchRequest(BaseModel):
    issues: list[dict]


@router.post("/remediation/batch")
async def batch_remediation(req: BatchRequest):
    """Get remediation suggestions for a batch of issues."""
    results = _engine.get_batch(req.issues)
    matched = sum(1 for r in results if r["remediation"] is not None)
    return {
        "total": len(results),
        "matched": matched,
        "results": results,
    }
