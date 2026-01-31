"""Tests for the rules module."""

import pytest
from pathlib import Path

from codescope.parsers.python.parser import PythonParser
from codescope.rules import get_rules, get_rule
from codescope.rules.python.security import (
    SQLInjectionRule,
    CommandInjectionRule,
    HardcodedSecretRule,
)
from codescope.rules.python.smells import (
    FunctionTooLongRule,
    CyclomaticComplexityRule,
    DeepNestingRule,
)
from codescope.rules.python.bugs import (
    MutableDefaultArgumentRule,
    ComparisonToNoneRule,
    BareExceptRule,
)


@pytest.fixture
def parser():
    """Create a Python parser."""
    return PythonParser()


class TestRuleRegistry:
    """Tests for RuleRegistry."""

    def test_get_python_rules(self):
        """Test getting rules for Python."""
        rules = get_rules("python")
        assert len(rules) > 0
        assert all(r.id.startswith("python:") for r in rules)

    def test_get_specific_rule(self):
        """Test getting a specific rule by ID."""
        rule = get_rule("python:S3649")
        assert rule is not None
        assert rule.name == "SQL Injection"


class TestSecurityRules:
    """Tests for security rules."""

    def test_sql_injection_detection(self, parser):
        """Test SQL injection detection."""
        code = '''
def get_user(name):
    query = "SELECT * FROM users WHERE name = '%s'" % name
    return query
'''
        parsed = parser.parse(code)
        rule = SQLInjectionRule()
        result = rule.check(parsed)

        assert len(result.issues) >= 1
        assert any("SQL" in i.message for i in result.issues)

    def test_sql_injection_fstring(self, parser):
        """Test SQL injection via f-string."""
        code = '''
def get_user(name):
    query = f"SELECT * FROM users WHERE name = '{name}'"
    return query
'''
        parsed = parser.parse(code)
        rule = SQLInjectionRule()
        result = rule.check(parsed)

        assert len(result.issues) >= 1

    def test_command_injection(self, parser):
        """Test command injection detection."""
        code = '''
import os
def run(cmd):
    os.system(cmd)
'''
        parsed = parser.parse(code)
        rule = CommandInjectionRule()
        result = rule.check(parsed)

        assert len(result.issues) >= 1

    def test_hardcoded_password(self, parser):
        """Test hardcoded password detection."""
        code = '''
password = "<your_password>"
api_key = "<your_api_key>"
'''
        parsed = parser.parse(code)
        rule = HardcodedSecretRule()
        result = rule.check(parsed)

        assert len(result.issues) >= 1


class TestCodeSmellRules:
    """Tests for code smell rules."""

    def test_function_too_long(self, parser):
        """Test detection of long functions."""
        # Generate a long function
        lines = ["def long_function():"]
        for i in range(60):
            lines.append(f"    x{i} = {i}")
        lines.append("    return x0")
        code = "\n".join(lines)

        parsed = parser.parse(code)
        rule = FunctionTooLongRule({"max_lines": 50})
        result = rule.check(parsed)

        assert len(result.issues) == 1

    def test_deep_nesting(self, parser):
        """Test detection of deep nesting."""
        code = '''
def deeply_nested(x):
    if x > 0:
        if x > 1:
            if x > 2:
                if x > 3:
                    if x > 4:
                        return x
    return 0
'''
        parsed = parser.parse(code)
        rule = DeepNestingRule({"max_depth": 4})
        result = rule.check(parsed)

        assert len(result.issues) >= 1


class TestBugRules:
    """Tests for bug detection rules."""

    def test_mutable_default(self, parser):
        """Test mutable default argument detection."""
        code = '''
def append_item(items=[]):
    items.append(1)
    return items
'''
        parsed = parser.parse(code)
        rule = MutableDefaultArgumentRule()
        result = rule.check(parsed)

        assert len(result.issues) == 1

    def test_comparison_to_none(self, parser):
        """Test comparison to None detection."""
        code = '''
def check_none(x):
    if x == None:
        return True
    return False
'''
        parsed = parser.parse(code)
        rule = ComparisonToNoneRule()
        result = rule.check(parsed)

        assert len(result.issues) == 1

    def test_bare_except(self, parser):
        """Test bare except detection."""
        code = '''
try:
    risky()
except:
    pass
'''
        parsed = parser.parse(code)
        rule = BareExceptRule()
        result = rule.check(parsed)

        assert len(result.issues) == 1


class TestCleanCode:
    """Tests that clean code doesn't trigger false positives."""

    def test_clean_code_no_issues(self, parser, clean_python_code):
        """Test that clean code has minimal issues."""
        parsed = parser.parse(clean_python_code)

        # Check security rules
        sql_rule = SQLInjectionRule()
        cmd_rule = CommandInjectionRule()
        secret_rule = HardcodedSecretRule()

        assert len(sql_rule.check(parsed).issues) == 0
        assert len(cmd_rule.check(parsed).issues) == 0
        assert len(secret_rule.check(parsed).issues) == 0
