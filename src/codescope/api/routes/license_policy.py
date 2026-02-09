"""License Compliance Policy API routes."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from codescope.license_policy import (
    LicensePolicyEngine,
    LicensePolicy,
    LicenseCategory,
    SPDXEvaluator,
)

router = APIRouter()


class PolicyRequest(BaseModel):
    project_path: str
    policy: dict | None = None
    preset: str | None = None


class CheckLicenseRequest(BaseModel):
    spdx_id: str
    package_name: str = "unknown"
    policy: dict | None = None
    preset: str | None = None


def _build_policy(policy_dict: dict | None, preset: str | None) -> LicensePolicy:
    if policy_dict:
        return LicensePolicy.from_dict(policy_dict)
    if preset:
        from codescope.license_policy.engine import PRESET_POLICIES
        builder = PRESET_POLICIES.get(preset)
        if not builder:
            raise HTTPException(status_code=400, detail=f"Unknown preset: {preset}")
        return builder()
    return LicensePolicy()


@router.get("/license-policy/presets")
async def list_presets():
    """List available policy presets."""
    return {"presets": LicensePolicyEngine.available_presets()}


@router.get("/license-policy/categories")
async def list_categories():
    """List all license categories with their SPDX IDs."""
    return {"categories": SPDXEvaluator.get_all_categories()}


@router.post("/license-policy/evaluate")
async def evaluate_policy(request: PolicyRequest):
    """Evaluate a project's dependencies against a license policy."""
    project = Path(request.project_path)
    if not project.exists():
        raise HTTPException(status_code=404, detail="Project path not found")

    policy = _build_policy(request.policy, request.preset)
    engine = LicensePolicyEngine(policy=policy)
    result = engine.evaluate(project)
    return result.to_dict()


@router.post("/license-policy/check")
async def check_license(request: CheckLicenseRequest):
    """Quick check: is a specific license allowed by a policy?"""
    policy = _build_policy(request.policy, request.preset)
    engine = LicensePolicyEngine(policy=policy)
    violation = engine.check_single(request.spdx_id, request.package_name)

    if violation:
        return {"allowed": False, "violation": violation.to_dict()}
    return {"allowed": True, "violation": None}


@router.get("/license-policy/parse-spdx")
async def parse_spdx(expression: str = Query(..., description="SPDX expression to parse")):
    """Parse an SPDX license expression into individual IDs with categories."""
    ids = SPDXEvaluator.parse_expression(expression)
    return {
        "expression": expression,
        "licenses": [
            {"spdx_id": lid, "category": SPDXEvaluator.categorize(lid).value}
            for lid in ids
        ],
    }
