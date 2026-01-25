"""Rule engine module for CodeScope."""

from codescope.rules.base import Rule, RuleResult
from codescope.rules.registry import RuleRegistry, get_rules, get_rule

__all__ = [
    "Rule",
    "RuleResult",
    "RuleRegistry",
    "get_rules",
    "get_rule",
]
