# Developer Guide: Extending CodeScope

This guide covers how to extend CodeScope with custom rules, analyzers, and integrations.

## Table of Contents

- [Rule Development](#rule-development)
  - [Rule Architecture](#rule-architecture)
  - [Creating a Pattern Rule](#creating-a-pattern-rule)
  - [Creating an AST Rule](#creating-an-ast-rule)
  - [Rule Registration](#rule-registration)
  - [Rule Parameters](#rule-parameters)
  - [Testing Rules](#testing-rules)
- [YAML Custom Rules](#yaml-custom-rules)
  - [YAML Rule Format](#yaml-rule-format)
  - [Pattern Syntax](#pattern-syntax)
  - [Examples](#yaml-examples)
- [Parser Extensions](#parser-extensions)
- [Analyzer Development](#analyzer-development)
- [API Extensions](#api-extensions)
- [Best Practices](#best-practices)

---

## Rule Development

### Rule Architecture

CodeScope provides three base classes for implementing detection rules:

```
Rule (Abstract Base)
├── PatternRule (Regex-based detection)
└── ASTRule (Abstract Syntax Tree analysis)
```

**Base Rule Class Attributes:**

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `id` | `str` | Yes | Unique identifier (format: `language:SXXXX`) |
| `name` | `str` | Yes | Human-readable name |
| `description` | `str` | Yes | Detailed description of what the rule detects |
| `severity` | `Severity` | Yes | BLOCKER, CRITICAL, MAJOR, MINOR, INFO |
| `issue_type` | `IssueType` | Yes | BUG, VULNERABILITY, CODE_SMELL, SECURITY_HOTSPOT |
| `languages` | `list[str]` | Yes | Target languages (e.g., `["python"]`) |
| `effort_minutes` | `int` | No | Estimated fix time (default: 5) |
| `cwe_ids` | `list[int]` | No | CWE vulnerability IDs |
| `owasp_categories` | `list[str]` | No | OWASP Top 10 categories (e.g., `["A03:2021"]`) |
| `tags` | `list[str]` | No | Categorization tags |
| `default_params` | `dict` | No | Configurable parameters |

### Creating a Pattern Rule

Pattern rules use regex for fast, line-by-line detection. Best for simple patterns.

```python
# src/codescope/rules/python/security.py

from codescope.rules.base import PatternRule
from codescope.rules.registry import RuleRegistry
from codescope.core.enums import Severity, IssueType

@RuleRegistry.register
class HardcodedPasswordRule(PatternRule):
    """Detect hardcoded passwords in code."""

    id = "python:S2068"
    name = "Hardcoded Password"
    description = (
        "Hardcoded passwords create security vulnerabilities. "
        "Passwords should be stored in secure configuration or secrets management."
    )
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    languages = ["python"]
    effort_minutes = 15
    cwe_ids = [798, 259]  # Hard-coded Credentials, Use of Hard-coded Password
    owasp_categories = ["A07:2021"]  # Identification and Authentication Failures
    tags = ["security", "credentials", "owasp-top10"]

    # Regex patterns to detect (any match triggers an issue)
    patterns = [
        r'password\s*=\s*["\'][^"\']+["\']',
        r'passwd\s*=\s*["\'][^"\']+["\']',
        r'secret\s*=\s*["\'][^"\']+["\']',
        r'api_key\s*=\s*["\'][^"\']+["\']',
    ]

    # Patterns that indicate false positives (exclude from detection)
    exclude_patterns = [
        r'password\s*=\s*["\']["\']',  # Empty password
        r'password\s*=\s*os\.environ',  # Environment variable
        r'password\s*=\s*getenv',       # Environment variable
        r'#.*password',                  # Comment
    ]
```

### Creating an AST Rule

AST rules analyze parsed code structure for deeper semantic analysis.

```python
# src/codescope/rules/python/smells.py

from codescope.rules.base import ASTRule
from codescope.rules.registry import RuleRegistry
from codescope.core.enums import Severity, IssueType
from codescope.core.models import Issue
from codescope.parsers.models import ParsedFile

@RuleRegistry.register
class TooManyParametersRule(ASTRule):
    """Detect functions with too many parameters."""

    id = "python:S107"
    name = "Too Many Parameters"
    description = (
        "Functions with many parameters are harder to understand and maintain. "
        "Consider using parameter objects or builder patterns."
    )
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    languages = ["python"]
    effort_minutes = 30
    tags = ["maintainability", "readability"]

    # AST node types to check
    node_types = ["function_definition"]

    # Configurable threshold
    default_params = {
        "max_parameters": 7,
    }

    def check_node(self, node, file: ParsedFile) -> list[Issue]:
        """Check a single function definition node."""
        issues = []

        # Find parameters node
        params_node = node.find_first("parameters")
        if not params_node:
            return issues

        # Count parameters (excluding self/cls)
        param_count = 0
        for child in params_node.children:
            if child.type in ("identifier", "typed_parameter", "default_parameter"):
                param_name = child.text if child.type == "identifier" else child.children[0].text
                if param_name not in ("self", "cls"):
                    param_count += 1

        max_params = self.params.get("max_parameters", 7)

        if param_count > max_params:
            # Get function name
            name_node = node.find_first("identifier")
            func_name = name_node.text if name_node else "function"

            issues.append(self.create_issue(
                file=file,
                line=node.start_line,
                end_line=node.start_line,
                message=f"Function '{func_name}' has {param_count} parameters (max: {max_params})",
                snippet=self.get_snippet(file, node.start_line),
            ))

        return issues
```

### Rule Registration

Rules are automatically registered using the `@RuleRegistry.register` decorator:

```python
from codescope.rules.registry import RuleRegistry

@RuleRegistry.register
class MyCustomRule(Rule):
    id = "python:CUSTOM001"
    # ...
```

**Manual registration** (for dynamic rules):

```python
registry = RuleRegistry()
registry.register_rule(MyCustomRule)
```

**Accessing registered rules:**

```python
from codescope.rules.registry import RuleRegistry

registry = RuleRegistry()

# Get all rules
all_rules = registry.get_all_rules()

# Filter by language
python_rules = registry.get_rules_for_language("python")

# Filter by type
vulnerabilities = registry.get_rules_by_type(IssueType.VULNERABILITY)

# Filter by tag
security_rules = registry.get_rules_by_tag("security")

# Get specific rule
rule = registry.get_rule("python:S3649")
```

### Rule Parameters

Rules can define configurable parameters with defaults:

```python
@RuleRegistry.register
class FunctionTooLongRule(ASTRule):
    id = "python:S138"
    name = "Function Too Long"
    # ...

    default_params = {
        "max_lines": 50,
        "count_comments": False,
        "count_blank_lines": False,
    }

    def check(self, file: ParsedFile) -> RuleResult:
        max_lines = self.params.get("max_lines", 50)
        # Use max_lines in detection logic
```

**Override parameters in configuration:**

```yaml
# codescope.yml
rules:
  parameters:
    "python:S138":
      max_lines: 100
      count_comments: true
```

### Testing Rules

Create test files in `tests/rules/`:

```python
# tests/rules/test_python_security.py

import pytest
from codescope.rules.python.security import HardcodedPasswordRule
from codescope.parsers.python import PythonParser

@pytest.fixture
def parser():
    return PythonParser()

@pytest.fixture
def rule():
    return HardcodedPasswordRule()

class TestHardcodedPasswordRule:
    """Tests for HardcodedPasswordRule."""

    def test_detects_hardcoded_password(self, rule, parser):
        """Should detect hardcoded password assignment."""
        code = '''
password = "secret123"
'''
        parsed = parser.parse(code, "test.py")
        result = rule.check(parsed)

        assert len(result.issues) == 1
        assert result.issues[0].rule_id == "python:S2068"
        assert result.issues[0].severity == Severity.BLOCKER

    def test_ignores_environment_variable(self, rule, parser):
        """Should not flag environment variable usage."""
        code = '''
password = os.environ.get("DB_PASSWORD")
'''
        parsed = parser.parse(code, "test.py")
        result = rule.check(parsed)

        assert len(result.issues) == 0

    def test_ignores_empty_password(self, rule, parser):
        """Should not flag empty string passwords."""
        code = '''
password = ""
'''
        parsed = parser.parse(code, "test.py")
        result = rule.check(parsed)

        assert len(result.issues) == 0

    @pytest.mark.parametrize("code,expected_count", [
        ('password = "test"', 1),
        ('passwd = "test"', 1),
        ('secret = "test"', 1),
        ('api_key = "test"', 1),
        ('username = "test"', 0),  # Not a sensitive field
    ])
    def test_various_field_names(self, rule, parser, code, expected_count):
        """Test detection across various field names."""
        parsed = parser.parse(code, "test.py")
        result = rule.check(parsed)

        assert len(result.issues) == expected_count
```

**Run rule tests:**

```bash
# Run all rule tests
pytest tests/rules/ -v

# Run specific rule test
pytest tests/rules/test_python_security.py::TestHardcodedPasswordRule -v

# With coverage
pytest tests/rules/ --cov=src/codescope/rules --cov-report=html
```

---

## YAML Custom Rules

For users who want to add rules without writing Python code, CodeScope supports YAML-defined custom rules.

### YAML Rule Format

```yaml
# custom_rules/my_rules.yaml

rules:
  - id: "custom:NO_PRINT"
    name: "No Print Statements"
    description: "Print statements should not be used in production code. Use logging instead."
    pattern: "\\bprint\\s*\\("
    message: "Replace print() with proper logging"
    severity: MINOR
    type: CODE_SMELL
    languages:
      - python
    tags:
      - logging
      - production
    enabled: true

  - id: "custom:TODO_FIXME"
    name: "TODO/FIXME Comment"
    description: "Track TODO and FIXME comments for follow-up"
    pattern: "#\\s*(TODO|FIXME|XXX|HACK):"
    message: "Found {match} comment that needs attention"
    severity: INFO
    type: CODE_SMELL
    languages: []  # All languages
    file_pattern: "*.py"
    tags:
      - technical-debt
    enabled: true

  - id: "custom:SQL_RAW"
    name: "Raw SQL Query"
    description: "Raw SQL queries may be vulnerable to injection"
    pattern: "cursor\\.execute\\s*\\([^,)]*%"
    message: "Use parameterized queries instead of string formatting"
    severity: CRITICAL
    type: VULNERABILITY
    languages:
      - python
    cwe:
      - 89
    tags:
      - security
      - sql
    enabled: true
```

### YAML Rule Properties

| Property | Type | Required | Description |
|----------|------|----------|-------------|
| `id` | string | Yes | Unique identifier (recommend: `custom:NAME`) |
| `name` | string | Yes | Human-readable name |
| `description` | string | Yes | Detailed description |
| `pattern` | string | Yes | Regex pattern to match |
| `message` | string | Yes | Issue message (supports `{match}` placeholder) |
| `severity` | string | Yes | BLOCKER, CRITICAL, MAJOR, MINOR, INFO |
| `type` | string | Yes | BUG, VULNERABILITY, CODE_SMELL, SECURITY_HOTSPOT |
| `languages` | list | No | Target languages (empty = all) |
| `file_pattern` | string | No | Glob pattern for file matching |
| `cwe` | list | No | CWE IDs |
| `tags` | list | No | Categorization tags |
| `enabled` | bool | No | Enable/disable rule (default: true) |
| `multiline` | bool | No | Enable multiline regex (default: false) |
| `negate` | bool | No | Flag if pattern does NOT match (default: false) |

### Pattern Syntax

Custom rules use Python regex syntax:

```yaml
# Literal matching
pattern: "eval\\("

# Word boundaries
pattern: "\\bexec\\b"

# Character classes
pattern: "['\"]password['\"]\\s*="

# Groups and alternation
pattern: "(TODO|FIXME|XXX):"

# Multiline patterns
pattern: "def\\s+\\w+.*:\\s*$\\s*pass"
multiline: true

# Negation (flag when pattern is NOT found)
pattern: "# Copyright"
negate: true
message: "Missing copyright header"
```

### YAML Examples

**Security-focused custom rules:**

```yaml
# custom_rules/security.yaml
rules:
  - id: "custom:WEAK_HASH"
    name: "Weak Hash Algorithm"
    description: "MD5 and SHA1 are cryptographically weak"
    pattern: "\\b(md5|sha1)\\s*\\("
    message: "Use SHA-256 or stronger hash algorithm"
    severity: MAJOR
    type: VULNERABILITY
    languages: [python]
    cwe: [328]
    tags: [security, crypto]

  - id: "custom:DEBUG_TRUE"
    name: "Debug Mode Enabled"
    description: "Debug mode should be disabled in production"
    pattern: "DEBUG\\s*=\\s*True"
    message: "Set DEBUG=False for production"
    severity: CRITICAL
    type: SECURITY_HOTSPOT
    languages: [python]
    file_pattern: "settings*.py"
    tags: [security, configuration]
```

**Code quality custom rules:**

```yaml
# custom_rules/quality.yaml
rules:
  - id: "custom:MAGIC_NUMBER"
    name: "Magic Number"
    description: "Numeric literals should be named constants"
    pattern: "[^\\d]\\d{4,}[^\\d]"
    message: "Consider extracting magic number to a named constant"
    severity: MINOR
    type: CODE_SMELL
    languages: []
    tags: [readability]

  - id: "custom:LONG_LINE"
    name: "Line Too Long"
    description: "Lines should not exceed 120 characters"
    pattern: "^.{121,}$"
    message: "Line exceeds 120 characters"
    severity: INFO
    type: CODE_SMELL
    languages: []
    tags: [formatting]
```

**Loading custom rules:**

```yaml
# codescope.yml
custom_rules:
  - custom_rules/security.yaml
  - custom_rules/quality.yaml
```

```bash
# CLI
codescope scan --custom-rules custom_rules/

# API
POST /api/v1/custom-rules
{
  "rules": [...]
}
```

---

## Parser Extensions

To add support for a new language:

```python
# src/codescope/parsers/kotlin/__init__.py

from codescope.parsers.base import BaseParser
from codescope.parsers.registry import ParserRegistry

@ParserRegistry.register("kotlin")
class KotlinParser(BaseParser):
    """Parser for Kotlin source files."""

    language = "kotlin"
    file_extensions = [".kt", ".kts"]

    def __init__(self):
        super().__init__()
        # Initialize tree-sitter for Kotlin
        import tree_sitter_kotlin
        self.parser.set_language(tree_sitter_kotlin.language())

    def extract_functions(self, tree) -> list[FunctionDef]:
        """Extract function definitions from AST."""
        functions = []
        for node in tree.root_node.find_all("function_declaration"):
            functions.append(FunctionDef(
                name=node.find_first("simple_identifier").text,
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
                # ...
            ))
        return functions

    def extract_imports(self, tree) -> list[Import]:
        """Extract import statements."""
        # Implementation
        pass
```

---

## Analyzer Development

Create domain-specific analyzers:

```python
# src/codescope/analyzers/my_analyzer/__init__.py

from codescope.analyzers.base import BaseAnalyzer

class MyCustomAnalyzer(BaseAnalyzer):
    """Custom analysis engine."""

    name = "my_analyzer"
    description = "Analyzes specific patterns"

    def analyze(self, project_path: Path) -> AnalysisResult:
        """Run analysis on project."""
        issues = []

        for file in self.get_files(project_path):
            parsed = self.parse_file(file)
            # Custom analysis logic
            issues.extend(self.check_file(parsed))

        return AnalysisResult(issues=issues)
```

---

## API Extensions

Add new API endpoints:

```python
# src/codescope/api/routes/my_feature.py

from fastapi import APIRouter, Depends
from codescope.api.auth import require_role
from codescope.auth.models import UserRole

router = APIRouter(prefix="/my-feature", tags=["my-feature"])

@router.get("/")
async def list_items(user=Depends(require_role(UserRole.VIEWER))):
    """List items."""
    return {"items": []}

@router.post("/")
async def create_item(user=Depends(require_role(UserRole.ANALYST))):
    """Create new item."""
    return {"status": "created"}
```

Register in `app.py`:

```python
from codescope.api.routes import my_feature

app.include_router(my_feature.router, prefix="/api/v1")
```

---

## Best Practices

### Rule Development

1. **Use specific rule IDs**: Follow the format `language:SXXXX` (e.g., `python:S3649`)
2. **Provide CWE/OWASP mappings**: Essential for compliance reporting
3. **Set realistic effort estimates**: Help teams plan remediation
4. **Write comprehensive tests**: Cover positive and negative cases
5. **Document edge cases**: Note known limitations in description
6. **Use appropriate severity**: Reserve BLOCKER for critical security issues

### Performance

1. **Prefer PatternRule for simple checks**: Faster than AST analysis
2. **Limit AST traversal**: Only check necessary node types
3. **Cache computed values**: Use `@lru_cache` for expensive operations
4. **Test with large codebases**: Ensure rules scale

### Security

1. **Validate all inputs**: Especially in API endpoints
2. **Sanitize regex patterns**: Prevent ReDoS attacks
3. **Limit resource usage**: Set timeouts for analysis
4. **Log security events**: Track rule triggers for audit

### Documentation

1. **Write clear descriptions**: Help users understand issues
2. **Provide fix examples**: Show correct code patterns
3. **Reference standards**: Link to CWE, OWASP, language guides
4. **Keep docs updated**: Regenerate when rules change

---

## Additional Resources

- [Rule Catalog](RULES.md) - Auto-generated documentation of all rules
- [Architecture Guide](ARCHITECTURE.md) - System design and components
- [API Reference](API_REFERENCE.md) - Complete API documentation
- [Testing Guide](TESTING.md) - Testing strategies and examples
