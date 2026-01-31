"""Rules API routes."""

from typing import Optional
from fastapi import APIRouter, Query

from codescope.api.models import RuleResponse, RuleListResponse
from codescope.rules.registry import RuleRegistry
from codescope.core.enums import Severity, IssueType

router = APIRouter()


@router.get("/rules", response_model=RuleListResponse)
async def list_rules(
    language: Optional[str] = None,
    severity: Optional[list[Severity]] = Query(None),
    issue_type: Optional[list[IssueType]] = Query(None),
    tags: Optional[list[str]] = Query(None),
    enabled_only: bool = False,
):
    """List all available rules."""
    registry = RuleRegistry()
    rules = registry.get_all_rules()

    result = []

    for rule in rules:
        # Apply filters
        if language and rule.language.lower() != language.lower():
            continue

        if severity and rule.severity not in severity:
            continue

        if issue_type and rule.issue_type not in issue_type:
            continue

        if tags:
            rule_tags = set(rule.tags)
            if not rule_tags.intersection(set(tags)):
                continue

        if enabled_only and not rule.enabled:
            continue

        result.append(
            RuleResponse(
                id=rule.rule_id,
                name=rule.name,
                description=rule.description,
                severity=rule.severity,
                issue_type=rule.issue_type,
                language=rule.language,
                tags=rule.tags,
                enabled=rule.enabled,
            )
        )

    return RuleListResponse(
        rules=result,
        total=len(result),
    )


@router.get("/rules/{rule_id}", response_model=RuleResponse)
async def get_rule(rule_id: str):
    """Get a specific rule by ID."""
    registry = RuleRegistry()
    rule = registry.get_rule(rule_id)

    if not rule:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Rule not found")

    return RuleResponse(
        id=rule.rule_id,
        name=rule.name,
        description=rule.description,
        severity=rule.severity,
        issue_type=rule.issue_type,
        language=rule.language,
        tags=rule.tags,
        enabled=rule.enabled,
    )


@router.get("/rules/languages")
async def list_languages():
    """List all supported languages."""
    registry = RuleRegistry()
    rules = registry.get_all_rules()

    languages = set()
    for rule in rules:
        languages.add(rule.language)

    return {
        "languages": sorted(languages),
        "count": len(languages),
    }


@router.get("/rules/tags")
async def list_tags():
    """List all available rule tags."""
    registry = RuleRegistry()
    rules = registry.get_all_rules()

    tags = set()
    for rule in rules:
        tags.update(rule.tags)

    return {
        "tags": sorted(tags),
        "count": len(tags),
    }


@router.put("/rules/{rule_id}/enable")
async def enable_rule(rule_id: str):
    """Enable a rule."""
    registry = RuleRegistry()
    rule = registry.get_rule(rule_id)

    if not rule:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Rule not found")

    rule.enabled = True
    return {"status": "enabled", "rule_id": rule_id}


@router.put("/rules/{rule_id}/disable")
async def disable_rule(rule_id: str):
    """Disable a rule."""
    registry = RuleRegistry()
    rule = registry.get_rule(rule_id)

    if not rule:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Rule not found")

    rule.enabled = False
    return {"status": "disabled", "rule_id": rule_id}
