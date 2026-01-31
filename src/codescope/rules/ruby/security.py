"""Ruby security rules."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class RubySQLInjectionRule(Rule):
    """Detect SQL injection vulnerabilities in Ruby."""

    id = "ruby:S3649"
    name = "SQL Injection"
    description = "SQL queries should use parameterized queries or ActiveRecord safely"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [89]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "sql", "injection", "owasp-top10", "rails"]
    languages = ["ruby"]

    SQL_PATTERNS = [
        r'\.where\s*\(\s*["\'].*#\{',  # String interpolation in where
        r'\.find_by_sql\s*\(\s*["\'].*#\{',
        r'\.execute\s*\(\s*["\'].*#\{',
        r'\.select\s*\(\s*["\'].*#\{',
        r'\.order\s*\(\s*["\'].*#\{',
        r'\.group\s*\(\s*["\'].*#\{',
        r'\.having\s*\(\s*["\'].*#\{',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for SQL injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.SQL_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="SQL query uses string interpolation - use parameterized queries instead",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyCommandInjectionRule(Rule):
    """Detect OS command injection in Ruby."""

    id = "ruby:S2076"
    name = "Command Injection"
    description = "OS commands should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [78]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "command-injection", "owasp-top10"]
    languages = ["ruby"]

    DANGEROUS_PATTERNS = [
        r'`[^`]*#\{',  # Backtick with interpolation
        r'system\s*\(\s*["\'].*#\{',
        r'exec\s*\(\s*["\'].*#\{',
        r'%x\[[^\]]*#\{',
        r'IO\.popen\s*\([^)]*#\{',
        r'Open3\.',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for command injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.DANGEROUS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential command injection - use Shellwords.escape() for user input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyXSSRule(Rule):
    """Detect XSS vulnerabilities in Ruby/Rails."""

    id = "ruby:S5131"
    name = "Cross-Site Scripting (XSS)"
    description = "User input should be sanitized before rendering"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [79]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "xss", "owasp-top10", "rails"]
    languages = ["ruby"]

    XSS_PATTERNS = [
        r'\.html_safe',
        r'raw\s*\(',
        r'<%==',  # Rails unescaped output
        r'render\s+inline:',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for XSS patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.XSS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential XSS - avoid html_safe and raw unless content is trusted",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyMassAssignmentRule(Rule):
    """Detect mass assignment vulnerabilities in Rails."""

    id = "ruby:S5334"
    name = "Mass Assignment"
    description = "Models should use strong parameters to prevent mass assignment"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [915]
    owasp_categories = ["A04:2021"]
    effort_minutes = 20
    tags = ["security", "mass-assignment", "rails"]
    languages = ["ruby"]

    DANGEROUS_PATTERNS = [
        r'\.update_attributes\s*\(\s*params\s*\)',
        r'\.create\s*\(\s*params\s*\)',
        r'\.new\s*\(\s*params\s*\)',
        r'attr_accessible\s*$',  # Empty attr_accessible
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for mass assignment vulnerabilities."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.DANGEROUS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential mass assignment vulnerability - use strong parameters (params.require().permit())",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyHardcodedSecretRule(PatternRule):
    """Detect hardcoded secrets in Ruby."""

    id = "ruby:S2068"
    name = "Hardcoded Credentials"
    description = "Credentials should not be hardcoded in source code"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [798, 259]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "credentials", "secrets", "owasp-top10"]
    languages = ["ruby"]

    patterns = [
        r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']{3,}["\']',
        r'(?i)(api_key|apikey)\s*=\s*["\'][^"\']{8,}["\']',
        r'(?i)(secret|secret_key)\s*=\s*["\'][^"\']{8,}["\']',
        r'(?i)(token|auth_token|access_token)\s*=\s*["\'][^"\']{8,}["\']',
        r'-----BEGIN (RSA |DSA |EC )?PRIVATE KEY-----',
    ]

    exclude_patterns = [
        r'=\s*["\'](\*+|xxx+|<[^>]+>|changeme|example)["\']',
        r'ENV\[',
        r'Rails\.application\.credentials',
    ]
