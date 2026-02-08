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
class JavaLDAPInjectionRule(Rule):
    """Detect LDAP injection vulnerabilities in Java."""

    id = "java:S2078"
    name = "LDAP Injection"
    description = "LDAP queries should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [90]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "ldap", "injection", "owasp-top10"]
    languages = ["java"]

    LDAP_PATTERNS = [
        r'\.search\s*\([^)]*\+',
        r'new\s+SearchFilter\s*\([^)]*\+',
        r'ldapTemplate\.search\s*\([^)]*\+',
        r'\.newSearchRequest\s*\([^)]*\+',
        r'".*\(uid=".*\+',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for LDAP injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.LDAP_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential LDAP injection - use parameterized LDAP queries",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JavaLogInjectionRule(Rule):
    """Detect log injection vulnerabilities in Java."""

    id = "java:S5145"
    name = "Log Injection"
    description = "User input should be sanitized before being logged"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [117]
    owasp_categories = ["A09:2021"]
    effort_minutes = 15
    tags = ["security", "logging", "injection"]
    languages = ["java"]

    LOG_PATTERNS = [
        r'logger\.(info|debug|warn|error|trace)\s*\([^)]*\+.*request\.',
        r'log\.(info|debug|warn|error|trace)\s*\([^)]*\+.*getParameter',
        r'LOG\.(info|debug|warn|error|trace)\s*\([^)]*\+.*getHeader',
        r'System\.out\.println\s*\([^)]*request\.',
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


@RuleRegistry.register
class JavaSSRFRule(Rule):
    """Detect Server-Side Request Forgery vulnerabilities in Java."""

    id = "java:S5144"
    name = "SSRF (Server-Side Request Forgery)"
    description = "URLs for outbound requests should be validated to prevent SSRF attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [918]
    owasp_categories = ["A10:2021"]
    effort_minutes = 30
    tags = ["security", "ssrf", "owasp-top10"]
    languages = ["java"]

    SSRF_PATTERNS = [
        r'new\s+URL\s*\([^)]*\+',
        r'new\s+URL\s*\([^)]*request\.getParameter',
        r'HttpClient.*execute\s*\([^)]*\+',
        r'RestTemplate.*\.(get|post|put|delete)\s*\([^)]*\+',
        r'WebClient.*uri\s*\([^)]*\+',
        r'\.openConnection\s*\(\s*\).*request\.',
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
class JavaXPathInjectionRule(Rule):
    """Detect XPath injection vulnerabilities in Java."""

    id = "java:S2091"
    name = "XPath Injection"
    description = "XPath queries should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [643]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "xpath", "injection", "owasp-top10"]
    languages = ["java"]

    XPATH_PATTERNS = [
        r'xpath\.evaluate\s*\([^)]*\+',
        r'\.selectNodes\s*\([^)]*\+',
        r'\.selectSingleNode\s*\([^)]*\+',
        r'XPathFactory.*compile\s*\([^)]*\+',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for XPath injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.XPATH_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential XPath injection - use parameterized XPath queries",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JavaUnsafeReflectionRule(Rule):
    """Detect unsafe reflection usage in Java."""

    id = "java:S3011"
    name = "Unsafe Reflection"
    description = "Reflection should not be used to bypass access control"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [470]
    owasp_categories = ["A03:2021"]
    effort_minutes = 45
    tags = ["security", "reflection"]
    languages = ["java"]

    REFLECTION_PATTERNS = [
        r'Class\.forName\s*\([^)]*request\.',
        r'Class\.forName\s*\([^)]*\+',
        r'\.setAccessible\s*\(\s*true\s*\)',
        r'getMethod\s*\([^)]*\+.*invoke',
        r'getDeclaredMethod\s*\([^)]*\+',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for unsafe reflection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.REFLECTION_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Unsafe reflection - avoid using reflection with user-controlled input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JavaSpringELInjectionRule(Rule):
    """Detect Spring Expression Language injection vulnerabilities."""

    id = "java:S5146"
    name = "Spring EL Injection"
    description = "Spring Expression Language should not evaluate user-controlled input"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [917]
    owasp_categories = ["A03:2021"]
    effort_minutes = 45
    tags = ["security", "injection", "spring"]
    languages = ["java"]

    SPEL_PATTERNS = [
        r'SpelExpressionParser\s*\(\s*\).*parseExpression\s*\([^)]*\+',
        r'ExpressionParser.*parseExpression\s*\([^)]*request\.',
        r'@Value\s*\(\s*"[^"]*\#\{.*\+',
        r'StandardEvaluationContext.*getValue\s*\([^)]*\+',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for Spring EL injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.SPEL_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential Spring EL injection - never evaluate user input as SpEL",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JavaOpenRedirectRule(Rule):
    """Detect open redirect vulnerabilities in Java."""

    id = "java:S5146"
    name = "Open Redirect"
    description = "URLs used for redirects should be validated"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [601]
    owasp_categories = ["A01:2021"]
    effort_minutes = 20
    tags = ["security", "redirect", "phishing"]
    languages = ["java"]

    REDIRECT_PATTERNS = [
        r'response\.sendRedirect\s*\([^)]*request\.getParameter',
        r'response\.sendRedirect\s*\([^)]*\+',
        r'ModelAndView\s*\(\s*"redirect:.*\+',
        r'return\s+"redirect:.*\+',
        r'RedirectView\s*\([^)]*\+',
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
class JavaWeakCryptoRule(Rule):
    """Detect use of weak cryptographic algorithms in Java."""

    id = "java:S5547"
    name = "Weak Cryptography"
    description = "Weak cryptographic algorithms should not be used"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [327, 328]
    owasp_categories = ["A02:2021"]
    effort_minutes = 30
    tags = ["security", "cryptography"]
    languages = ["java"]

    WEAK_CRYPTO_PATTERNS = [
        (r'Cipher\.getInstance\s*\(\s*"DES"', "DES is insecure, use AES"),
        (r'Cipher\.getInstance\s*\(\s*"DESede"', "3DES is deprecated, use AES"),
        (r'Cipher\.getInstance\s*\(\s*"RC2"', "RC2 is insecure"),
        (r'Cipher\.getInstance\s*\(\s*"RC4"', "RC4 is insecure"),
        (r'Cipher\.getInstance\s*\(\s*"Blowfish"', "Blowfish has known weaknesses"),
        (r'MessageDigest\.getInstance\s*\(\s*"MD5"', "MD5 is cryptographically broken"),
        (r'MessageDigest\.getInstance\s*\(\s*"SHA-1"', "SHA-1 is deprecated for security use"),
        (r'Cipher\.getInstance\s*\(\s*"AES/ECB"', "ECB mode is insecure, use CBC or GCM"),
        (r'SecureRandom\.getInstance\s*\(\s*"SHA1PRNG"', "SHA1PRNG has known weaknesses"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for weak cryptographic algorithms."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.WEAK_CRYPTO_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
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
class JavaInsecureTLSRule(Rule):
    """Detect insecure TLS configurations in Java."""

    id = "java:S4830"
    name = "Insecure TLS Configuration"
    description = "TLS configuration should not disable certificate verification"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [295]
    owasp_categories = ["A02:2021"]
    effort_minutes = 30
    tags = ["security", "tls", "ssl"]
    languages = ["java"]

    INSECURE_TLS_PATTERNS = [
        r'setHostnameVerifier\s*\(\s*SSLSocketFactory\.ALLOW_ALL',
        r'TrustAllCerts',
        r'X509TrustManager.*checkClientTrusted.*\{\s*\}',
        r'X509TrustManager.*checkServerTrusted.*\{\s*\}',
        r'setDefaultHostnameVerifier\s*\(',
        r'SSLContext\.getInstance\s*\(\s*"SSL"\s*\)',
        r'SSLContext\.getInstance\s*\(\s*"TLSv1"\s*\)',
        r'SSLContext\.getInstance\s*\(\s*"TLSv1\.1"\s*\)',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure TLS configurations."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.INSECURE_TLS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Insecure TLS configuration - do not disable certificate verification",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JavaSensitiveDataExposureRule(Rule):
    """Detect exposure of sensitive data in Java."""

    id = "java:S5757"
    name = "Sensitive Data Exposure"
    description = "Sensitive data should not be exposed in logs or error messages"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [200, 532]
    owasp_categories = ["A02:2021"]
    effort_minutes = 15
    tags = ["security", "sensitive-data", "logging"]
    languages = ["java"]

    SENSITIVE_PATTERNS = [
        r'System\.out\.println\s*\([^)]*password',
        r'System\.out\.println\s*\([^)]*creditCard',
        r'System\.out\.println\s*\([^)]*ssn',
        r'logger\.(info|debug|error)\s*\([^)]*password',
        r'printStackTrace\s*\(\s*\)',
        r'e\.getMessage\s*\(\s*\).*response',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for sensitive data exposure."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.SENSITIVE_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message="Potential sensitive data exposure - avoid logging passwords or exposing stack traces",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


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
