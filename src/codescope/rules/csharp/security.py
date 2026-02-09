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


@RuleRegistry.register
class CSharpPathTraversalRule(Rule):
    """Detect path traversal vulnerabilities in C#."""

    id = "csharp:S2083"
    name = "Path Traversal"
    description = "File paths should be validated before use"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [22]
    owasp_categories = ["A01:2021"]
    effort_minutes = 30
    tags = ["security", "path-traversal", "owasp-top10"]
    languages = ["csharp"]

    PATH_PATTERNS = [
        r'File\.(Read|Write|Open|Delete|Move|Copy).*Request\.',
        r'FileStream.*Request\.',
        r'Path\.Combine\s*\([^)]*Request\.',
        r'Directory\.(Get|Create|Delete).*Request\.',
        r'new\s+FileInfo\s*\([^)]*Request\.',
        r'Server\.MapPath\s*\([^)]*Request\.',
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
class CSharpSSRFRule(Rule):
    """Detect Server-Side Request Forgery vulnerabilities in C#."""

    id = "csharp:S5144"
    name = "SSRF (Server-Side Request Forgery)"
    description = "URLs for outbound requests should be validated to prevent SSRF attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [918]
    owasp_categories = ["A10:2021"]
    effort_minutes = 30
    tags = ["security", "ssrf", "owasp-top10"]
    languages = ["csharp"]

    SSRF_PATTERNS = [
        r'HttpClient.*GetAsync\s*\([^)]*Request\.',
        r'HttpClient.*PostAsync\s*\([^)]*Request\.',
        r'WebClient.*Download.*\([^)]*Request\.',
        r'WebRequest\.Create\s*\([^)]*Request\.',
        r'new\s+Uri\s*\([^)]*Request\.',
        r'HttpClient.*SendAsync.*Request\.',
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
class CSharpLDAPInjectionRule(Rule):
    """Detect LDAP injection vulnerabilities in C#."""

    id = "csharp:S2078"
    name = "LDAP Injection"
    description = "LDAP queries should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [90]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "ldap", "injection", "owasp-top10"]
    languages = ["csharp"]

    LDAP_PATTERNS = [
        r'DirectorySearcher.*Filter.*\+',
        r'DirectorySearcher.*Filter.*Request\.',
        r'".*\(uid=".*\+',
        r'new\s+DirectoryEntry\s*\([^)]*\+',
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
                            message="Potential LDAP injection - sanitize user input before LDAP queries",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class CSharpLogInjectionRule(Rule):
    """Detect log injection vulnerabilities in C#."""

    id = "csharp:S5145"
    name = "Log Injection"
    description = "User input should be sanitized before being logged"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [117]
    owasp_categories = ["A09:2021"]
    effort_minutes = 15
    tags = ["security", "logging", "injection"]
    languages = ["csharp"]

    LOG_PATTERNS = [
        r'_logger\.(Log|Info|Debug|Warn|Error).*Request\.',
        r'Log\.(Information|Debug|Warning|Error).*Request\.',
        r'Console\.Write.*Request\.',
        r'Trace\.Write.*Request\.',
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
class CSharpOpenRedirectRule(Rule):
    """Detect open redirect vulnerabilities in C#."""

    id = "csharp:S5146"
    name = "Open Redirect"
    description = "URLs used for redirects should be validated"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [601]
    owasp_categories = ["A01:2021"]
    effort_minutes = 20
    tags = ["security", "redirect", "phishing"]
    languages = ["csharp"]

    REDIRECT_PATTERNS = [
        r'Redirect\s*\(\s*Request\.',
        r'RedirectToAction\s*\([^)]*Request\.',
        r'Response\.Redirect\s*\([^)]*Request\.',
        r'Redirect\s*\(\s*returnUrl',
        r'LocalRedirect\s*\(\s*Request\.',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for open redirect patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.REDIRECT_PATTERNS:
                if re.search(pattern, line):
                    # Check if Url.IsLocalUrl is used
                    if 'IsLocalUrl' not in line:
                        result.issues.append(
                            self.create_issue(
                                message="Potential open redirect - use Url.IsLocalUrl() to validate redirect URLs",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                    break

        return result


@RuleRegistry.register
class CSharpWeakCryptoRule(Rule):
    """Detect use of weak cryptographic algorithms in C#."""

    id = "csharp:S5547"
    name = "Weak Cryptography"
    description = "Weak cryptographic algorithms should not be used"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [327, 328]
    owasp_categories = ["A02:2021"]
    effort_minutes = 30
    tags = ["security", "cryptography"]
    languages = ["csharp"]

    WEAK_CRYPTO_PATTERNS = [
        (r'MD5\.Create\s*\(', "MD5 is cryptographically broken"),
        (r'SHA1\.Create\s*\(', "SHA1 is deprecated for security use"),
        (r'new\s+MD5CryptoServiceProvider', "MD5 is cryptographically broken"),
        (r'new\s+SHA1CryptoServiceProvider', "SHA1 is deprecated"),
        (r'new\s+DESCryptoServiceProvider', "DES is insecure, use AES"),
        (r'new\s+TripleDESCryptoServiceProvider', "3DES is deprecated, use AES"),
        (r'new\s+RC2CryptoServiceProvider', "RC2 is insecure"),
        (r'CipherMode\.ECB', "ECB mode is insecure, use CBC or GCM"),
        (r'new\s+Random\s*\(', "System.Random is predictable - use RandomNumberGenerator"),
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
class CSharpInsecureCookieRule(Rule):
    """Detect insecure cookie settings in C#."""

    id = "csharp:S2092"
    name = "Insecure Cookie"
    description = "Cookies should be created with security attributes"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [614, 1004]
    owasp_categories = ["A05:2021"]
    effort_minutes = 10
    tags = ["security", "cookie", "session"]
    languages = ["csharp"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure cookie settings."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            # Check cookie creation
            if 'new Cookie' in line or 'CookieOptions' in line:
                # Check for missing security flags
                context_end = min(i + 5, len(lines))
                context = '\n'.join(lines[i-1:context_end])

                if 'Secure = true' not in context and 'HttpOnly = true' not in context:
                    result.issues.append(
                        self.create_issue(
                            message="Cookie may be insecure - set Secure = true and HttpOnly = true",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )

        return result


@RuleRegistry.register
class CSharpCSRFRule(Rule):
    """Detect missing CSRF protection in C# ASP.NET."""

    id = "csharp:S5131"
    name = "Missing CSRF Protection"
    description = "POST/PUT/DELETE endpoints should have CSRF protection"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [352]
    owasp_categories = ["A05:2021"]
    effort_minutes = 15
    tags = ["security", "csrf", "aspnet"]
    languages = ["csharp"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing CSRF protection."""
        result = RuleResult()
        lines = file.source.split("\n")
        source = file.source

        # Check for POST methods without AntiForgery
        has_post = '[HttpPost]' in source or '[HttpPut]' in source or '[HttpDelete]' in source
        has_antiforgery = 'ValidateAntiForgeryToken' in source or 'AutoValidateAntiforgeryToken' in source
        has_ignore = 'IgnoreAntiforgeryToken' in source

        if has_post and not has_antiforgery:
            for i, line in enumerate(lines, 1):
                if '[HttpPost]' in line or '[HttpPut]' in line or '[HttpDelete]' in line:
                    # Check if next few lines have ValidateAntiForgeryToken
                    context_end = min(i + 3, len(lines))
                    context = '\n'.join(lines[i-1:context_end])
                    if 'ValidateAntiForgeryToken' not in context and 'IgnoreAntiforgeryToken' not in context:
                        result.issues.append(
                            self.create_issue(
                                message="Missing CSRF protection - add [ValidateAntiForgeryToken] attribute",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )

        return result


@RuleRegistry.register
class CSharpXPathInjectionRule(Rule):
    """Detect XPath injection vulnerabilities in C#."""

    id = "csharp:S2091"
    name = "XPath Injection"
    description = "XPath queries should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [643]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "xpath", "injection", "owasp-top10"]
    languages = ["csharp"]

    XPATH_PATTERNS = [
        r'SelectNodes\s*\([^)]*\+',
        r'SelectSingleNode\s*\([^)]*\+',
        r'XPathNavigator.*Select\s*\([^)]*\+',
        r'Evaluate\s*\([^)]*\+.*XPath',
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
                            message="Potential XPath injection - use parameterized XPath or validate input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class CSharpRegexDoSRule(Rule):
    """Detect Regular Expression Denial of Service vulnerabilities in C#."""

    id = "csharp:S5852"
    name = "ReDoS (Regex DoS)"
    description = "Regular expressions should not be vulnerable to catastrophic backtracking"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [1333, 400]
    owasp_categories = ["A06:2021"]
    effort_minutes = 45
    tags = ["security", "regex", "dos"]
    languages = ["csharp"]

    REDOS_PATTERNS = [
        r'new\s+Regex\s*\([^)]*Request\.',
        r'Regex\.Match\s*\([^)]*Request\.',
        r'Regex\.(IsMatch|Replace)\s*\([^)]*,\s*Request\.',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for ReDoS patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.REDOS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential ReDoS - use Regex with timeout or avoid user input in patterns",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class CSharpSensitiveDataExposureRule(Rule):
    """Detect exposure of sensitive data in C#."""

    id = "csharp:S5757"
    name = "Sensitive Data Exposure"
    description = "Sensitive data should not be exposed in logs or responses"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [200, 532]
    owasp_categories = ["A02:2021"]
    effort_minutes = 15
    tags = ["security", "sensitive-data", "logging"]
    languages = ["csharp"]

    SENSITIVE_PATTERNS = [
        r'Console\.Write.*password',
        r'_logger\.(Log|Info|Debug|Error).*password',
        r'return.*password',
        r'return.*connectionString',
        r'\.StackTrace\s*\}',
        r'ex\.ToString\s*\(\s*\)',
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
                            message="Potential sensitive data exposure - avoid logging passwords or returning stack traces",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result
