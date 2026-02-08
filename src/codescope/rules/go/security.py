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


@RuleRegistry.register
class GoSSRFRule(Rule):
    """Detect Server-Side Request Forgery vulnerabilities in Go."""

    id = "go:S5144"
    name = "SSRF (Server-Side Request Forgery)"
    description = "URLs for outbound requests should be validated to prevent SSRF attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [918]
    owasp_categories = ["A10:2021"]
    effort_minutes = 30
    tags = ["security", "ssrf", "owasp-top10"]
    languages = ["go"]

    SSRF_PATTERNS = [
        r'http\.Get\s*\([^)]*\+',
        r'http\.Post\s*\([^)]*\+',
        r'http\.NewRequest\s*\([^)]*\+',
        r'client\.Do\s*\([^)]*r\.(URL|Form|Query)',
        r'http\.Get\s*\([^)]*r\.FormValue',
        r'http\.Get\s*\([^)]*r\.URL\.Query',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for SSRF patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.SSRF_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential SSRF - validate and allowlist URLs before making outbound requests",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GoPathTraversalRule(Rule):
    """Detect path traversal vulnerabilities in Go."""

    id = "go:S2083"
    name = "Path Traversal"
    description = "File paths should be validated before use"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [22]
    owasp_categories = ["A01:2021"]
    effort_minutes = 30
    tags = ["security", "path-traversal", "owasp-top10"]
    languages = ["go"]

    PATH_PATTERNS = [
        r'os\.Open\s*\([^)]*\+',
        r'os\.ReadFile\s*\([^)]*\+',
        r'ioutil\.ReadFile\s*\([^)]*\+',
        r'filepath\.Join\s*\([^)]*r\.(FormValue|URL\.Query)',
        r'http\.ServeFile\s*\([^)]*\+',
        r'os\.Open\s*\([^)]*r\.FormValue',
        r'os\.Open\s*\([^)]*r\.URL\.Query',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for path traversal patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.PATH_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential path traversal - validate and sanitize file paths from user input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GoXXERule(Rule):
    """Detect XXE vulnerabilities in Go XML parsing."""

    id = "go:S2755"
    name = "XML External Entity (XXE)"
    description = "XML parsers should be configured to prevent XXE attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [611]
    owasp_categories = ["A05:2021"]
    effort_minutes = 30
    tags = ["security", "xxe", "xml", "owasp-top10"]
    languages = ["go"]

    XXE_PATTERNS = [
        r'xml\.NewDecoder\s*\(',
        r'xml\.Unmarshal\s*\(',
        r'xml\.Decode\s*\(',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for potential XXE vulnerabilities."""
        result = RuleResult()
        lines = file.source.split("\n")
        source = file.source

        # Go's encoding/xml is safe by default, but flag if processing external input
        has_xml = False
        has_external_input = False

        for i, line in enumerate(lines, 1):
            for pattern in self.XXE_PATTERNS:
                if re.search(pattern, line):
                    has_xml = True
                    # Check if processing request body
                    if 'r.Body' in line or 'req.Body' in line or 'request.Body' in line:
                        has_external_input = True
                        result.issues.append(
                            self.create_issue(
                                message="XML parsing of external input - ensure DTD processing is disabled",
                                file_path=file.path,
                                start_line=i,
                                severity=Severity.MAJOR,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        break

        return result


@RuleRegistry.register
class GoRaceConditionRule(Rule):
    """Detect potential race condition vulnerabilities in Go."""

    id = "go:S5720"
    name = "Race Condition"
    description = "Shared resources should be protected with proper synchronization"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [362]
    owasp_categories = ["A04:2021"]
    effort_minutes = 45
    tags = ["security", "race-condition", "concurrency"]
    languages = ["go"]

    RACE_PATTERNS = [
        r'go\s+func\s*\(\s*\).*\{[^}]*\b\w+\b[^}]*\}',  # goroutine accessing outer variable
        r'go\s+\w+\s*\(',  # goroutine call without mutex
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for potential race conditions."""
        result = RuleResult()
        lines = file.source.split("\n")
        source = file.source

        # Check if there are goroutines and shared state
        has_goroutines = 'go func' in source or 'go ' in source
        has_mutex = 'sync.Mutex' in source or 'sync.RWMutex' in source or 'atomic.' in source

        if has_goroutines and not has_mutex:
            # Check for potential shared state access
            for i, line in enumerate(lines, 1):
                if re.search(r'go\s+func\s*\(', line) or re.search(r'go\s+\w+\s*\(', line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential race condition - consider using sync.Mutex or atomic operations for shared state",
                            file_path=file.path,
                            start_line=i,
                            severity=Severity.MINOR,
                            snippet=self.get_snippet(file, i),
                        )
                    )

        return result


@RuleRegistry.register
class GoWeakCryptoRule(Rule):
    """Detect use of weak cryptographic algorithms in Go."""

    id = "go:S5547"
    name = "Weak Cryptography"
    description = "Weak cryptographic algorithms should not be used"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [327, 328]
    owasp_categories = ["A02:2021"]
    effort_minutes = 30
    tags = ["security", "cryptography"]
    languages = ["go"]

    WEAK_CRYPTO_PATTERNS = [
        (r'crypto/md5', "MD5 is cryptographically broken"),
        (r'crypto/sha1', "SHA1 is deprecated for security use"),
        (r'crypto/des', "DES is insecure, use AES"),
        (r'crypto/rc4', "RC4 is insecure"),
        (r'md5\.New\s*\(', "MD5 is cryptographically broken"),
        (r'sha1\.New\s*\(', "SHA1 is deprecated for security use"),
        (r'des\.NewCipher\s*\(', "DES is insecure, use AES"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for weak cryptographic algorithms."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.WEAK_CRYPTO_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message=f"Weak cryptography detected - {message}",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GoTemplateInjectionRule(Rule):
    """Detect template injection vulnerabilities in Go."""

    id = "go:S5334"
    name = "Template Injection"
    description = "Template strings should not be constructed from user input"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [1336, 94]
    owasp_categories = ["A03:2021"]
    effort_minutes = 45
    tags = ["security", "injection", "template"]
    languages = ["go"]

    TEMPLATE_PATTERNS = [
        r'template\.New\s*\([^)]*\)\.Parse\s*\([^)]*\+',
        r'template\.Must\s*\([^)]*\+',
        r'\.Execute\s*\([^)]*r\.FormValue',
        r'html/template.*Parse\s*\([^)]*\+',
        r'text/template.*Parse\s*\([^)]*\+',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for template injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.TEMPLATE_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential template injection - never use user input in template strings",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GoOpenRedirectRule(Rule):
    """Detect open redirect vulnerabilities in Go."""

    id = "go:S5146"
    name = "Open Redirect"
    description = "URLs used for redirects should be validated"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [601]
    owasp_categories = ["A01:2021"]
    effort_minutes = 20
    tags = ["security", "redirect", "phishing"]
    languages = ["go"]

    REDIRECT_PATTERNS = [
        r'http\.Redirect\s*\([^)]*r\.FormValue',
        r'http\.Redirect\s*\([^)]*r\.URL\.Query',
        r'http\.Redirect\s*\([^)]*\+',
        r'w\.Header\s*\(\s*\)\.Set\s*\(\s*"Location"[^)]*\+',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for open redirect patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.REDIRECT_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential open redirect - validate redirect URLs against an allowlist",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GoInsecureRandomRule(Rule):
    """Detect use of insecure random number generation in Go."""

    id = "go:S2245"
    name = "Insecure Randomness"
    description = "math/rand should not be used for security-sensitive operations"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [338, 330]
    owasp_categories = ["A02:2021"]
    effort_minutes = 15
    tags = ["security", "random", "cryptography"]
    languages = ["go"]

    INSECURE_PATTERNS = [
        r'math/rand',
        r'rand\.Int\s*\(',
        r'rand\.Intn\s*\(',
        r'rand\.Read\s*\(',
    ]

    SECURITY_CONTEXTS = [
        'token', 'secret', 'key', 'password', 'salt', 'nonce', 'session', 'auth'
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure randomness usage."""
        result = RuleResult()
        lines = file.source.split("\n")
        source_lower = file.source.lower()

        # Check if file uses math/rand and has security context
        uses_math_rand = 'math/rand' in file.source
        has_security_context = any(ctx in source_lower for ctx in self.SECURITY_CONTEXTS)

        if uses_math_rand and has_security_context:
            for i, line in enumerate(lines, 1):
                for pattern in self.INSECURE_PATTERNS:
                    if re.search(pattern, line):
                        result.issues.append(
                            self.create_issue(
                                message="Insecure randomness - use crypto/rand for security-sensitive operations",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        break

        return result


@RuleRegistry.register
class GoLogInjectionRule(Rule):
    """Detect log injection vulnerabilities in Go."""

    id = "go:S5145"
    name = "Log Injection"
    description = "User input should be sanitized before being logged"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [117]
    owasp_categories = ["A09:2021"]
    effort_minutes = 15
    tags = ["security", "logging", "injection"]
    languages = ["go"]

    LOG_PATTERNS = [
        r'log\.(Print|Printf|Println)\s*\([^)]*r\.FormValue',
        r'log\.(Print|Printf|Println)\s*\([^)]*r\.URL\.Query',
        r'log\.(Print|Printf|Println)\s*\([^)]*r\.Header\.Get',
        r'fmt\.(Print|Printf|Println)\s*\([^)]*r\.FormValue',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for log injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.LOG_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential log injection - sanitize user input before logging",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result
