"""Custom rules management and execution endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from codescope.rules.custom.engine import CustomRulesEngine

router = APIRouter()

# In-memory rule storage
_custom_rules: list[dict] = []


class CustomRuleCreate(BaseModel):
    id: str
    name: str
    description: str = ""
    pattern: str
    message: str
    severity: str = "MAJOR"
    type: str = "CODE_SMELL"
    languages: list[str] = []
    file_pattern: str = ""
    cwe: list[int] = []
    tags: list[str] = []
    enabled: bool = True
    multiline: bool = False
    negate: bool = False


class CustomRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    pattern: Optional[str] = None
    message: Optional[str] = None
    severity: Optional[str] = None
    type: Optional[str] = None
    languages: Optional[list[str]] = None
    file_pattern: Optional[str] = None
    enabled: Optional[bool] = None
    multiline: Optional[bool] = None
    negate: Optional[bool] = None


class ScanRequest(BaseModel):
    path: str = "."


@router.get("/custom-rules")
async def list_custom_rules():
    """List all custom rules."""
    return _custom_rules


@router.post("/custom-rules")
async def create_custom_rule(rule: CustomRuleCreate):
    """Create a new custom rule."""
    # Check for duplicate ID
    for existing in _custom_rules:
        if existing["id"] == rule.id:
            raise HTTPException(409, f"Rule with id '{rule.id}' already exists")
    # Validate regex
    import re
    try:
        re.compile(rule.pattern)
    except re.error as exc:
        raise HTTPException(400, f"Invalid regex pattern: {exc}")

    rule_dict = rule.dict()
    _custom_rules.append(rule_dict)
    return rule_dict


@router.put("/custom-rules/{rule_id}")
async def update_custom_rule(rule_id: str, update: CustomRuleUpdate):
    """Update an existing custom rule."""
    for rule in _custom_rules:
        if rule["id"] == rule_id:
            for k, v in update.dict(exclude_none=True).items():
                rule[k] = v
            if update.pattern is not None:
                import re
                try:
                    re.compile(update.pattern)
                except re.error as exc:
                    raise HTTPException(400, f"Invalid regex pattern: {exc}")
            return rule
    raise HTTPException(404, f"Rule '{rule_id}' not found")


@router.delete("/custom-rules/{rule_id}")
async def delete_custom_rule(rule_id: str):
    """Delete a custom rule."""
    for i, rule in enumerate(_custom_rules):
        if rule["id"] == rule_id:
            _custom_rules.pop(i)
            return {"message": f"Rule '{rule_id}' deleted"}
    raise HTTPException(404, f"Rule '{rule_id}' not found")


@router.post("/custom-rules/scan")
async def run_custom_scan(req: ScanRequest):
    """Run all custom rules against a directory."""
    if not _custom_rules:
        raise HTTPException(400, "No custom rules defined")

    engine = CustomRulesEngine()
    engine.load_from_dict(_custom_rules)
    matches = engine.scan_directory(req.path)

    issues = []
    for m in matches:
        issues.append({
            "rule_id": m.rule.id,
            "rule_name": m.rule.name,
            "type": m.rule.type,
            "severity": m.rule.severity,
            "message": m.rule.message,
            "file": m.file,
            "line": m.line,
            "column": m.column,
            "matched_text": m.matched_text,
        })

    return {
        "total": len(issues),
        "rules_applied": len(engine.rules),
        "issues": issues,
    }
