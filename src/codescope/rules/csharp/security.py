"""C# security rules."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class CSharpSQLInjectionRule(Rule):
    """Detect SQL injection vulnerabilities in C#."""

    id = "csharp:S3649"
    name = "SQL Injection"
    description = "SQL queries should use parameterized queries"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [89]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "sql", "injection", "owasp-top10"]
    languages = ["csharp"]

    SQL_PATTERNS = [
        r'new\s+SqlCommand\s*\(\s*["\'].*\+',
        r'\.CommandText\s*=\s*["\'].*\+',
        r'ExecuteReader\s*\(\s*["\'].*\+',
        r'ExecuteNonQuery\s*\(\s*["\'].*\+',
        r'ExecuteScalar\s*\(\s*["\'].*\+',
        r'\$"SELECT.*\{',  # String interpolation in SQL
        r'\$"INSERT.*\{',
        r'\$"UPDATE.*\{',
        r'\$"DELETE.*\{',
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
                            message="SQL query uses string concatenation - use SqlParameter for parameterized queries",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class CSharpCommandInjectionRule(Rule):
    """Detect OS command injection in C#."""

    id = "csharp:S2076"
    name = "Command Injection"
    description = "OS commands should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [78]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "command-injection", "owasp-top10"]
    languages = ["csharp"]

    DANGEROUS_PATTERNS = [
        r'Process\.Start\s*\([^)]*\+',
        r'ProcessStartInfo.*Arguments.*\+',
        r'new\s+ProcessStartInfo\s*\([^)]*\+',
        r'cmd\.exe.*\/c',
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
                            message="Potential command injection - avoid passing user input to shell commands",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class CSharpXXERule(Rule):
    """Detect XXE vulnerabilities in C# XML parsing."""

    id = "csharp:S2755"
    name = "XML External Entity (XXE)"
    description = "XML parsers should be configured to prevent XXE attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [611]
    owasp_categories = ["A05:2021"]
    effort_minutes = 30
    tags = ["security", "xxe", "xml", "owasp-top10"]
    languages = ["csharp"]

    XXE_PATTERNS = [
        r'new\s+XmlDocument\s*\(\s*\)',
        r'XmlReader\.Create\s*\(',
        r'new\s+XmlTextReader\s*\(',
    ]

    SAFE_PATTERNS = [
        r'DtdProcessing\s*=\s*DtdProcessing\.Prohibit',
        r'XmlResolver\s*=\s*null',
        r'ProhibitDtd\s*=\s*true',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for XXE vulnerabilities."""
        result = RuleResult()
        lines = file.source.split("\n")

        has_xml = False
        has_protection = False
        xml_line = 0

        for i, line in enumerate(lines, 1):
            for pattern in self.XXE_PATTERNS:
                if re.search(pattern, line):
                    has_xml = True
                    if xml_line == 0:
                        xml_line = i

            for pattern in self.SAFE_PATTERNS:
                if re.search(pattern, line):
                    has_protection = True

        if has_xml and not has_protection:
            result.issues.append(
                self.create_issue(
                    message="XML parser may be vulnerable to XXE - set DtdProcessing.Prohibit and XmlResolver = null",
                    file_path=file.path,
                    start_line=xml_line,
                    snippet=self.get_snippet(file, xml_line),
                )
            )

        return result


@RuleRegistry.register
class CSharpDeserializationRule(Rule):
    """Detect unsafe deserialization in C#."""

    id = "csharp:S5135"
    name = "Unsafe Deserialization"
    description = "Deserialization of untrusted data can lead to remote code execution"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [502]
    owasp_categories = ["A08:2021"]
    effort_minutes = 60
    tags = ["security", "deserialization", "owasp-top10"]
    languages = ["csharp"]

    DANGEROUS_PATTERNS = [
        r'BinaryFormatter.*Deserialize',
        r'ObjectStateFormatter.*Deserialize',
        r'NetDataContractSerializer.*Deserialize',
        r'SoapFormatter.*Deserialize',
        r'LosFormatter.*Deserialize',
        r'JsonConvert\.DeserializeObject<.*>\s*\([^)]*\)',
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
                            message="Unsafe deserialization detected - BinaryFormatter is vulnerable to RCE",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class CSharpHardcodedSecretRule(PatternRule):
    """Detect hardcoded secrets in C#."""

    id = "csharp:S2068"
    name = "Hardcoded Credentials"
    description = "Credentials should not be hardcoded in source code"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [798, 259]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "credentials", "secrets", "owasp-top10"]
    languages = ["csharp"]

    patterns = [
        r'(?i)(password|passwd|pwd)\s*=\s*"[^"]{3,}"',
        r'(?i)(apiKey|ApiKey)\s*=\s*"[^"]{8,}"',
        r'(?i)(secret|SecretKey)\s*=\s*"[^"]{8,}"',
        r'(?i)(connectionString).*password=[^;]+',
        r'-----BEGIN (RSA |DSA |EC )?PRIVATE KEY-----',
    ]

    exclude_patterns = [
        r'=\s*"(\*+|xxx+|<[^>]+>|changeme|example)"',
        r'Configuration\[',
        r'Environment\.GetEnvironmentVariable',
    ]
