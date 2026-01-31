"""Rule registry for managing detection rules."""

from typing import Type

from codescope.rules.base import Rule


class RuleRegistry:
    """Registry for detection rules."""

    _rules: dict[str, Rule] = {}
    _rules_by_language: dict[str, list[Rule]] = {}

    @classmethod
    def register(cls, rule_class: Type[Rule]) -> Type[Rule]:
        """Register a rule class.

        Can be used as a decorator:
            @RuleRegistry.register
            class MyRule(Rule):
                ...
        """
        rule = rule_class()
        cls._rules[rule.id] = rule

        for lang in rule.languages:
            if lang not in cls._rules_by_language:
                cls._rules_by_language[lang] = []
            cls._rules_by_language[lang].append(rule)

        return rule_class

    @classmethod
    def get_rule(cls, rule_id: str) -> Rule | None:
        """Get rule by ID."""
        return cls._rules.get(rule_id)

    @classmethod
    def get_rules_for_language(cls, language: str) -> list[Rule]:
        """Get all rules for a language."""
        return cls._rules_by_language.get(language, [])

    @classmethod
    def get_all_rules(cls) -> dict[str, Rule]:
        """Get all registered rules."""
        return cls._rules.copy()

    @classmethod
    def get_rules_by_type(cls, issue_type: str) -> list[Rule]:
        """Get all rules of a specific type."""
        from codescope.core.enums import IssueType

        target_type = IssueType(issue_type)
        return [r for r in cls._rules.values() if r.issue_type == target_type]

    @classmethod
    def get_rules_by_tag(cls, tag: str) -> list[Rule]:
        """Get all rules with a specific tag."""
        return [r for r in cls._rules.values() if tag in r.tags]


def get_rules(language: str | None = None) -> list[Rule]:
    """Get rules, optionally filtered by language.

    Args:
        language: Optional language filter.

    Returns:
        List of matching rules.
    """
    if language:
        return RuleRegistry.get_rules_for_language(language)
    return list(RuleRegistry.get_all_rules().values())


def get_rule(rule_id: str) -> Rule | None:
    """Get a specific rule by ID.

    Args:
        rule_id: Rule identifier.

    Returns:
        Rule or None if not found.
    """
    return RuleRegistry.get_rule(rule_id)


# Import rules to trigger registration
def _register_rules() -> None:
    """Register all built-in rules."""
    # Import Python rules
    from codescope.rules.python import security  # noqa: F401
    from codescope.rules.python import smells  # noqa: F401
    from codescope.rules.python import bugs  # noqa: F401


_register_rules()
