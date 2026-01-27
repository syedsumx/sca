"""Go security rules."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class GoSQLInjectionRule(Rule):
    """Detect SQL injection vulnerabilities in Go."""

    id = "go:S3649"
    name = "SQL Injection"
    description = "SQL queries should use parameterized queries"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [89]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "sql", "injection", "owasp-top10"]
    languages = ["go"]

    SQL_PATTERNS = [
        r'db\.(Query|Exec|QueryRow)\s*\([^,)]*\+',
        r'fmt\.Sprintf\s*\(\s*"SELECT',
        r'fmt\.Sprintf\s*\(\s*"INSERT',
        r'fmt\.Sprintf\s*\(\s*"UPDATE',
        r'fmt\.Sprintf\s*\(\s*"DELETE',
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
                            message="SQL query uses string formatting - use parameterized queries with placeholders",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GoCommandInjectionRule(Rule):
    """Detect OS command injection in Go."""

    id = "go:S2076"
    name = "Command Injection"
    description = "OS commands should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [78]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "command-injection", "owasp-top10"]
    languages = ["go"]

    DANGEROUS_PATTERNS = [
        r'exec\.Command\s*\([^)]*\+',
        r'exec\.CommandContext\s*\([^)]*\+',
        r'os\.StartProcess\s*\([^)]*\+',
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
                            message="Potential command injection - avoid executing commands with user input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GoHardcodedSecretRule(PatternRule):
    """Detect hardcoded secrets in Go."""

    id = "go:S2068"
    name = "Hardcoded Credentials"
    description = "Credentials should not be hardcoded in source code"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [798, 259]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "credentials", "secrets", "owasp-top10"]
    languages = ["go"]

    patterns = [
        r'(?i)(password|passwd|pwd)\s*[=:]\s*"[^"]{3,}"',
        r'(?i)(apiKey|api_key)\s*[=:]\s*"[^"]{8,}"',
        r'(?i)(secret|secretKey)\s*[=:]\s*"[^"]{8,}"',
        r'(?i)(token|authToken|accessToken)\s*[=:]\s*"[^"]{8,}"',
        r'-----BEGIN (RSA |DSA |EC )?PRIVATE KEY-----',
    ]

    exclude_patterns = [
        r'[=:]\s*"(\*+|xxx+|<[^>]+>|changeme|example)"',
        r'os\.Getenv',
        r'viper\.',
    ]


@RuleRegistry.register
class GoInsecureTLSRule(Rule):
    """Detect insecure TLS configurations in Go."""

    id = "go:S4830"
    name = "Insecure TLS Configuration"
    description = "TLS configuration should not disable certificate verification"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [295]
    owasp_categories = ["A02:2021"]
    effort_minutes = 15
    tags = ["security", "tls", "ssl"]
    languages = ["go"]

    DANGEROUS_PATTERNS = [
        r'InsecureSkipVerify\s*:\s*true',
        r'MinVersion\s*:\s*tls\.VersionSSL',
        r'MinVersion\s*:\s*tls\.VersionTLS10',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure TLS configurations."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.DANGEROUS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Insecure TLS configuration - do not skip certificate verification or use weak TLS versions",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result
