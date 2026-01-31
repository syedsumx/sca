"""PHP security rules."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class PHPSQLInjectionRule(Rule):
    """Detect SQL injection vulnerabilities in PHP."""

    id = "php:S3649"
    name = "SQL Injection"
    description = "SQL queries should use prepared statements"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [89]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "sql", "injection", "owasp-top10"]
    languages = ["php"]

    SQL_PATTERNS = [
        r'mysql_query\s*\(\s*["\'].*\$',
        r'mysqli_query\s*\([^,]+,\s*["\'].*\$',
        r'\$\w+->query\s*\(\s*["\'].*\$',
        r'pg_query\s*\([^,]*["\'].*\$',
        r'"SELECT.*FROM.*"\s*\.\s*\$',
        r'"INSERT.*INTO.*"\s*\.\s*\$',
        r'"UPDATE.*SET.*"\s*\.\s*\$',
        r'"DELETE.*FROM.*"\s*\.\s*\$',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for SQL injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.SQL_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message="SQL query uses string concatenation - use prepared statements with bound parameters",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class PHPCommandInjectionRule(Rule):
    """Detect OS command injection in PHP."""

    id = "php:S2076"
    name = "Command Injection"
    description = "OS commands should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [78]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "command-injection", "owasp-top10"]
    languages = ["php"]

    DANGEROUS_PATTERNS = [
        r'exec\s*\(\s*["\'].*\$',
        r'shell_exec\s*\(\s*["\'].*\$',
        r'system\s*\(\s*["\'].*\$',
        r'passthru\s*\(\s*["\'].*\$',
        r'popen\s*\(\s*["\'].*\$',
        r'proc_open\s*\(',
        r'`[^`]*\$[^`]*`',  # Backtick execution
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
                            message="Potential command injection - use escapeshellarg() or escapeshellcmd()",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class PHPXSSRule(Rule):
    """Detect XSS vulnerabilities in PHP."""

    id = "php:S5131"
    name = "Cross-Site Scripting (XSS)"
    description = "User input should be escaped before output"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [79]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "xss", "owasp-top10"]
    languages = ["php"]

    XSS_PATTERNS = [
        r'echo\s+\$_(GET|POST|REQUEST|COOKIE)',
        r'print\s+\$_(GET|POST|REQUEST|COOKIE)',
        r'<\?=\s*\$_(GET|POST|REQUEST|COOKIE)',
        r'echo\s+["\'].*\$_(GET|POST|REQUEST|COOKIE)',
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
                            message="Potential XSS - use htmlspecialchars() to escape output",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class PHPFileInclusionRule(Rule):
    """Detect file inclusion vulnerabilities in PHP."""

    id = "php:S2083"
    name = "File Inclusion"
    description = "File paths should be validated before inclusion"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [98]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "lfi", "rfi", "owasp-top10"]
    languages = ["php"]

    DANGEROUS_PATTERNS = [
        r'include\s*\(\s*\$',
        r'include_once\s*\(\s*\$',
        r'require\s*\(\s*\$',
        r'require_once\s*\(\s*\$',
        r'include\s+\$',
        r'require\s+\$',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for file inclusion vulnerabilities."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.DANGEROUS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential file inclusion vulnerability - validate and sanitize file paths",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class PHPHardcodedSecretRule(PatternRule):
    """Detect hardcoded secrets in PHP."""

    id = "php:S2068"
    name = "Hardcoded Credentials"
    description = "Credentials should not be hardcoded in source code"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [798, 259]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "credentials", "secrets", "owasp-top10"]
    languages = ["php"]

    patterns = [
        r'(?i)\$(password|passwd|pwd)\s*=\s*["\'][^"\']{3,}["\']',
        r'(?i)\$(apiKey|api_key)\s*=\s*["\'][^"\']{8,}["\']',
        r'(?i)\$(secret|secretKey)\s*=\s*["\'][^"\']{8,}["\']',
        r'(?i)define\s*\(\s*["\'].*PASSWORD.*["\']\s*,\s*["\'][^"\']{3,}["\']',
        r'-----BEGIN (RSA |DSA |EC )?PRIVATE KEY-----',
        r'(?i)(mysql|postgres|mongodb)://[^:]+:[^@]+@',
    ]

    exclude_patterns = [
        r'=\s*["\'](\*+|xxx+|<[^>]+>|changeme|example)["\']',
        r'getenv\s*\(',
        r'\$_ENV\[',
    ]
