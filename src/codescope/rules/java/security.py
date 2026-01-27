"""Java security rules."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class JavaSQLInjectionRule(Rule):
    """Detect SQL injection vulnerabilities in Java."""

    id = "java:S3649"
    name = "SQL Injection"
    description = "SQL queries should use PreparedStatement with parameterized queries"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [89]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "sql", "injection", "owasp-top10"]
    languages = ["java"]

    SQL_PATTERNS = [
        r'createStatement\s*\(\s*\).*execute',
        r'executeQuery\s*\(\s*["\'].*\+',
        r'executeUpdate\s*\(\s*["\'].*\+',
        r'Statement.*execute.*\+.*"',
        r'"SELECT.*FROM.*"\s*\+',
        r'"INSERT.*INTO.*"\s*\+',
        r'"UPDATE.*SET.*"\s*\+',
        r'"DELETE.*FROM.*"\s*\+',
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
                            message="SQL query uses string concatenation - use PreparedStatement with parameters",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JavaCommandInjectionRule(Rule):
    """Detect OS command injection in Java."""

    id = "java:S2076"
    name = "Command Injection"
    description = "OS commands should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [78]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "command-injection", "owasp-top10"]
    languages = ["java"]

    DANGEROUS_PATTERNS = [
        r'Runtime\.getRuntime\(\)\.exec\s*\(',
        r'ProcessBuilder\s*\([^)]*\+',
        r'new\s+ProcessBuilder\s*\(\s*["\'].*\+',
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
                            message="Potential command injection - avoid executing shell commands with user input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JavaXXERule(Rule):
    """Detect XXE vulnerabilities in Java XML parsing."""

    id = "java:S2755"
    name = "XML External Entity (XXE)"
    description = "XML parsers should be configured to prevent XXE attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [611]
    owasp_categories = ["A05:2021"]
    effort_minutes = 30
    tags = ["security", "xxe", "xml", "owasp-top10"]
    languages = ["java"]

    XXE_PATTERNS = [
        r'DocumentBuilderFactory\.newInstance\(\)',
        r'SAXParserFactory\.newInstance\(\)',
        r'XMLInputFactory\.newInstance\(\)',
        r'TransformerFactory\.newInstance\(\)',
        r'SchemaFactory\.newInstance\(',
    ]

    SAFE_PATTERNS = [
        r'setFeature\s*\(\s*"http://apache\.org/xml/features/disallow-doctype-decl"',
        r'setFeature\s*\(\s*XMLConstants\.FEATURE_SECURE_PROCESSING',
        r'setProperty\s*\(\s*XMLConstants\.ACCESS_EXTERNAL_DTD',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for XXE vulnerabilities."""
        result = RuleResult()
        lines = file.source.split("\n")

        # Check if file has XML factory usage
        has_factory = False
        has_protection = False
        factory_line = 0

        for i, line in enumerate(lines, 1):
            for pattern in self.XXE_PATTERNS:
                if re.search(pattern, line):
                    has_factory = True
                    if factory_line == 0:
                        factory_line = i

            for pattern in self.SAFE_PATTERNS:
                if re.search(pattern, line):
                    has_protection = True

        if has_factory and not has_protection:
            result.issues.append(
                self.create_issue(
                    message="XML parser may be vulnerable to XXE attacks - disable external entities",
                    file_path=file.path,
                    start_line=factory_line,
                    snippet=self.get_snippet(file, factory_line),
                )
            )

        return result


@RuleRegistry.register
class JavaDeserializationRule(Rule):
    """Detect unsafe deserialization in Java."""

    id = "java:S5135"
    name = "Unsafe Deserialization"
    description = "Deserialization of untrusted data can lead to remote code execution"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [502]
    owasp_categories = ["A08:2021"]
    effort_minutes = 60
    tags = ["security", "deserialization", "owasp-top10"]
    languages = ["java"]

    DANGEROUS_PATTERNS = [
        r'ObjectInputStream\s*\(',
        r'\.readObject\s*\(\s*\)',
        r'XMLDecoder\s*\(',
        r'XStream\s*\(\s*\)',
        r'ObjectMapper.*readValue',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for unsafe deserialization."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.DANGEROUS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Unsafe deserialization detected - validate and sanitize serialized data",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JavaHardcodedSecretRule(PatternRule):
    """Detect hardcoded secrets in Java."""

    id = "java:S2068"
    name = "Hardcoded Credentials"
    description = "Credentials should not be hardcoded in source code"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [798, 259]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "credentials", "secrets", "owasp-top10"]
    languages = ["java"]

    patterns = [
        r'(?i)(password|passwd|pwd)\s*=\s*"[^"]{3,}"',
        r'(?i)(apiKey|api_key)\s*=\s*"[^"]{8,}"',
        r'(?i)(secret|secretKey)\s*=\s*"[^"]{8,}"',
        r'(?i)(token|authToken|accessToken)\s*=\s*"[^"]{8,}"',
        r'-----BEGIN (RSA |DSA |EC )?PRIVATE KEY-----',
        r'(?i)jdbc:[^"]*password=[^"&]+',
    ]

    exclude_patterns = [
        r'=\s*"(\*+|xxx+|<[^>]+>|changeme|example)"',
        r'System\.getenv',
        r'getProperty\s*\(',
    ]


@RuleRegistry.register
class JavaPathTraversalRule(Rule):
    """Detect path traversal vulnerabilities in Java."""

    id = "java:S2083"
    name = "Path Traversal"
    description = "File paths should be validated before use"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [22]
    owasp_categories = ["A01:2021"]
    effort_minutes = 30
    tags = ["security", "path-traversal", "owasp-top10"]
    languages = ["java"]

    DANGEROUS_PATTERNS = [
        r'new\s+File\s*\([^)]*\+',
        r'new\s+FileInputStream\s*\([^)]*\+',
        r'new\s+FileOutputStream\s*\([^)]*\+',
        r'Paths\.get\s*\([^)]*\+',
        r'Files\.(read|write|copy|move)\s*\([^)]*\+',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for path traversal patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.DANGEROUS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential path traversal - validate file paths before use",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result
