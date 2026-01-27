"""JavaScript/TypeScript security rules."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class JSXSSRule(Rule):
    """Detect potential XSS vulnerabilities in JavaScript."""

    id = "javascript:S5131"
    name = "Cross-Site Scripting (XSS)"
    description = "User input should be sanitized before being rendered to prevent XSS attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [79]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "xss", "owasp-top10"]
    languages = ["javascript", "typescript"]

    XSS_PATTERNS = [
        r'\.innerHTML\s*=',
        r'\.outerHTML\s*=',
        r'document\.write\s*\(',
        r'document\.writeln\s*\(',
        r'\.insertAdjacentHTML\s*\(',
        r'eval\s*\([^)]*\+',
        r'dangerouslySetInnerHTML',
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
                            message="Potential XSS vulnerability - user input may be rendered without sanitization",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSEvalRule(Rule):
    """Detect use of eval() and similar dangerous functions."""

    id = "javascript:S1523"
    name = "Eval Injection"
    description = "eval() and similar functions should not be used as they can lead to code injection"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [95]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "injection", "owasp-top10"]
    languages = ["javascript", "typescript"]

    DANGEROUS_FUNCTIONS = [
        r'\beval\s*\(',
        r'\bFunction\s*\(',
        r'setTimeout\s*\(\s*[\'"`]',
        r'setInterval\s*\(\s*[\'"`]',
        r'new\s+Function\s*\(',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for dangerous eval patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("/*"):
                continue

            for pattern in self.DANGEROUS_FUNCTIONS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Use of eval() or similar function can lead to code injection vulnerabilities",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSCommandInjectionRule(Rule):
    """Detect OS command injection in Node.js."""

    id = "javascript:S2076"
    name = "Command Injection"
    description = "OS commands should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [78]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "command-injection", "owasp-top10", "nodejs"]
    languages = ["javascript", "typescript"]

    DANGEROUS_PATTERNS = [
        r'child_process.*exec\s*\(',
        r'child_process.*execSync\s*\(',
        r'child_process.*spawn\s*\([^)]*shell\s*:\s*true',
        r'require\s*\(\s*[\'"]child_process[\'"]\s*\).*exec',
        r'execSync\s*\(\s*`',  # Template literal in execSync
        r'exec\s*\(\s*`',  # Template literal in exec
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
                            message="Potential command injection - avoid using shell commands with user input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSHardcodedSecretRule(PatternRule):
    """Detect hardcoded secrets in JavaScript."""

    id = "javascript:S2068"
    name = "Hardcoded Credentials"
    description = "Credentials should not be hardcoded in source code"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [798, 259]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "credentials", "secrets", "owasp-top10"]
    languages = ["javascript", "typescript"]

    patterns = [
        r'(?i)(password|passwd|pwd)\s*[=:]\s*[\'"][^\'"]{3,}[\'"]',
        r'(?i)(api[_-]?key|apikey)\s*[=:]\s*[\'"][^\'"]{8,}[\'"]',
        r'(?i)(secret[_-]?key|secretkey)\s*[=:]\s*[\'"][^\'"]{8,}[\'"]',
        r'(?i)(auth[_-]?token|access[_-]?token|bearer)\s*[=:]\s*[\'"][^\'"]{8,}[\'"]',
        r'(?i)aws[_-]?(secret[_-]?access[_-]?key|access[_-]?key[_-]?id)\s*[=:]\s*[\'"][^\'"]+[\'"]',
        r'-----BEGIN (RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----',
        r'(?i)(mysql|postgres|mongodb|redis)://[^:]+:[^@]+@',
    ]

    exclude_patterns = [
        r'[=:]\s*[\'"](\*+|xxx+|\.\.\.+|<[^>]+>|your[_-]?password|changeme|example)[\'"]',
        r'process\.env',
        r'config\.',
    ]


@RuleRegistry.register
class JSPrototypePollutionRule(Rule):
    """Detect potential prototype pollution vulnerabilities."""

    id = "javascript:S5147"
    name = "Prototype Pollution"
    description = "Object properties should be safely accessed to prevent prototype pollution"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [1321]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "prototype-pollution"]
    languages = ["javascript", "typescript"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for prototype pollution patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        # Patterns that may indicate prototype pollution
        dangerous_patterns = [
            r'\[.*\]\s*=.*\[.*\]',  # obj[key] = value[key]
            r'Object\.assign\s*\(\s*\{\}',  # Shallow copy that can be polluted
            r'__proto__',
            r'constructor\.prototype',
        ]

        for i, line in enumerate(lines, 1):
            for pattern in dangerous_patterns:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential prototype pollution - validate object keys before assignment",
                            file_path=file.path,
                            start_line=i,
                            severity=Severity.MAJOR,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSSQLInjectionRule(Rule):
    """Detect SQL injection vulnerabilities in JavaScript."""

    id = "javascript:S3649"
    name = "SQL Injection"
    description = "SQL queries should use parameterized statements"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [89]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "sql", "injection", "owasp-top10"]
    languages = ["javascript", "typescript"]

    SQL_PATTERNS = [
        r'\.query\s*\(\s*`[^`]*\$\{',  # Template literal in query
        r'\.query\s*\(\s*[\'"][^\'"]*\'\s*\+',  # String concat in query
        r'\.execute\s*\(\s*`[^`]*\$\{',
        r'SELECT.*FROM.*\+',
        r'INSERT.*INTO.*\+',
        r'UPDATE.*SET.*\+',
        r'DELETE.*FROM.*\+',
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
                            message="SQL query uses string interpolation - use parameterized queries instead",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result
