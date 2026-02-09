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


@RuleRegistry.register
class PHPDeserializationRule(Rule):
    """Detect insecure deserialization in PHP."""

    id = "php:S5135"
    name = "Insecure Deserialization"
    description = "Deserialization of untrusted data can lead to remote code execution"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [502]
    owasp_categories = ["A08:2021"]
    effort_minutes = 60
    tags = ["security", "deserialization", "owasp-top10"]
    languages = ["php"]

    DESERIALIZE_PATTERNS = [
        r'unserialize\s*\(\s*\$_(GET|POST|REQUEST|COOKIE)',
        r'unserialize\s*\(\s*\$',
        r'unserialize\s*\(\s*file_get_contents',
        r'unserialize\s*\(\s*base64_decode',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure deserialization."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.DESERIALIZE_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Insecure deserialization - never unserialize untrusted data, use JSON instead",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class PHPSSRFRule(Rule):
    """Detect Server-Side Request Forgery vulnerabilities in PHP."""

    id = "php:S5144"
    name = "SSRF (Server-Side Request Forgery)"
    description = "URLs for outbound requests should be validated to prevent SSRF attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [918]
    owasp_categories = ["A10:2021"]
    effort_minutes = 30
    tags = ["security", "ssrf", "owasp-top10"]
    languages = ["php"]

    SSRF_PATTERNS = [
        r'file_get_contents\s*\(\s*\$_(GET|POST|REQUEST)',
        r'curl_setopt\s*\([^,]+,\s*CURLOPT_URL\s*,\s*\$',
        r'curl_init\s*\(\s*\$_(GET|POST|REQUEST)',
        r'fopen\s*\(\s*\$_(GET|POST|REQUEST)',
        r'readfile\s*\(\s*\$_(GET|POST|REQUEST)',
        r'file_get_contents\s*\(\s*\$.*\.\s*\$',
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
class PHPSessionFixationRule(Rule):
    """Detect session fixation vulnerabilities in PHP."""

    id = "php:S5128"
    name = "Session Fixation"
    description = "Session ID should be regenerated after authentication"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [384]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "session", "authentication"]
    languages = ["php"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for session fixation vulnerabilities."""
        result = RuleResult()
        source = file.source
        lines = source.split("\n")

        # Check if file handles login/authentication
        has_login = any(x in source.lower() for x in ['login', 'authenticate', 'signin', 'password'])
        has_session = 'session_start' in source or '$_SESSION' in source
        has_regenerate = 'session_regenerate_id' in source

        if has_login and has_session and not has_regenerate:
            for i, line in enumerate(lines, 1):
                if 'session_start' in line or '$_SESSION' in line.lower():
                    result.issues.append(
                        self.create_issue(
                            message="Potential session fixation - call session_regenerate_id(true) after authentication",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class PHPLDAPInjectionRule(Rule):
    """Detect LDAP injection vulnerabilities in PHP."""

    id = "php:S2078"
    name = "LDAP Injection"
    description = "LDAP queries should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [90]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "ldap", "injection", "owasp-top10"]
    languages = ["php"]

    LDAP_PATTERNS = [
        r'ldap_search\s*\([^)]*\.\s*\$_(GET|POST|REQUEST)',
        r'ldap_search\s*\([^)]*\$.*\.',
        r'ldap_bind\s*\([^)]*\$_(GET|POST|REQUEST)',
        r'".*\(uid=.*"\s*\.\s*\$',
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
                            message="Potential LDAP injection - use ldap_escape() for user input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class PHPObjectInjectionRule(Rule):
    """Detect object injection vulnerabilities in PHP."""

    id = "php:S5334"
    name = "Object Injection"
    description = "User input should not be used to instantiate objects"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [915, 502]
    owasp_categories = ["A08:2021"]
    effort_minutes = 45
    tags = ["security", "injection", "object-injection"]
    languages = ["php"]

    OBJECT_PATTERNS = [
        r'new\s+\$_(GET|POST|REQUEST)',
        r'new\s+\$\w+\s*\(',
        r'\$class\s*=.*\$_(GET|POST|REQUEST)',
        r'call_user_func\s*\(\s*\$_(GET|POST|REQUEST)',
        r'call_user_func_array\s*\(\s*\$_(GET|POST|REQUEST)',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for object injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.OBJECT_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential object injection - validate class names against an allowlist",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class PHPXXERule(Rule):
    """Detect XXE vulnerabilities in PHP XML parsing."""

    id = "php:S2755"
    name = "XML External Entity (XXE)"
    description = "XML parsers should be configured to prevent XXE attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [611]
    owasp_categories = ["A05:2021"]
    effort_minutes = 30
    tags = ["security", "xxe", "xml", "owasp-top10"]
    languages = ["php"]

    XXE_PATTERNS = [
        r'simplexml_load_string\s*\(',
        r'simplexml_load_file\s*\(',
        r'DOMDocument.*loadXML\s*\(',
        r'new\s+SimpleXMLElement\s*\(',
    ]

    SAFE_PATTERNS = [
        r'libxml_disable_entity_loader\s*\(\s*true\s*\)',
        r'LIBXML_NOENT',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for XXE vulnerabilities."""
        result = RuleResult()
        source = file.source
        lines = source.split("\n")

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
                    message="XML parser may be vulnerable to XXE - call libxml_disable_entity_loader(true) before parsing",
                    file_path=file.path,
                    start_line=xml_line,
                    snippet=self.get_snippet(file, xml_line),
                )
            )

        return result


@RuleRegistry.register
class PHPWeakCryptoRule(Rule):
    """Detect use of weak cryptographic functions in PHP."""

    id = "php:S5547"
    name = "Weak Cryptography"
    description = "Weak cryptographic algorithms should not be used"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [327, 328]
    owasp_categories = ["A02:2021"]
    effort_minutes = 30
    tags = ["security", "cryptography"]
    languages = ["php"]

    WEAK_CRYPTO_PATTERNS = [
        (r'md5\s*\(', "MD5 is cryptographically broken - use password_hash() or hash('sha256')"),
        (r'sha1\s*\(', "SHA1 is deprecated - use hash('sha256') or stronger"),
        (r'mcrypt_', "mcrypt is deprecated and removed - use openssl_encrypt()"),
        (r'crypt\s*\(', "crypt() is weak - use password_hash() instead"),
        (r'rand\s*\(', "rand() is predictable - use random_int() for security"),
        (r'mt_rand\s*\(', "mt_rand() is predictable - use random_int() for security"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for weak cryptographic functions."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.WEAK_CRYPTO_PATTERNS:
                if re.search(pattern, line):
                    # Check context - md5/sha1 for password is critical
                    line_lower = line.lower()
                    if 'password' in line_lower or 'secret' in line_lower:
                        result.issues.append(
                            self.create_issue(
                                message=f"Weak cryptography - {message}",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        break

        return result


@RuleRegistry.register
class PHPOpenRedirectRule(Rule):
    """Detect open redirect vulnerabilities in PHP."""

    id = "php:S5146"
    name = "Open Redirect"
    description = "URLs used for redirects should be validated"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [601]
    owasp_categories = ["A01:2021"]
    effort_minutes = 20
    tags = ["security", "redirect", "phishing"]
    languages = ["php"]

    REDIRECT_PATTERNS = [
        r'header\s*\(\s*["\']Location:.*\$_(GET|POST|REQUEST)',
        r'header\s*\(\s*["\']Location:.*\.\s*\$',
        r'wp_redirect\s*\(\s*\$_(GET|POST|REQUEST)',
        r'redirect\s*\(\s*\$_(GET|POST|REQUEST)',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for open redirect patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.REDIRECT_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
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
class PHPInsecureCookieRule(Rule):
    """Detect insecure cookie settings in PHP."""

    id = "php:S2092"
    name = "Insecure Cookie"
    description = "Cookies should be created with security attributes"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [614, 1004]
    owasp_categories = ["A05:2021"]
    effort_minutes = 10
    tags = ["security", "cookie", "session"]
    languages = ["php"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure cookie settings."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            # Check setcookie calls
            if re.search(r'setcookie\s*\(', line):
                # Check if security parameters are set
                if 'true' not in line.lower() and 'httponly' not in line.lower():
                    result.issues.append(
                        self.create_issue(
                            message="Cookie may be insecure - set httponly and secure flags",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )

            # Check session cookie settings
            if 'session.cookie_secure' in line and 'false' in line.lower():
                result.issues.append(
                    self.create_issue(
                        message="Session cookie is not secure - set session.cookie_secure = true",
                        file_path=file.path,
                        start_line=i,
                        snippet=self.get_snippet(file, i),
                    )
                )

        return result


@RuleRegistry.register
class PHPPathTraversalRule(Rule):
    """Detect path traversal vulnerabilities in PHP."""

    id = "php:S2083"
    name = "Path Traversal"
    description = "File paths should be validated before use"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [22]
    owasp_categories = ["A01:2021"]
    effort_minutes = 30
    tags = ["security", "path-traversal", "owasp-top10"]
    languages = ["php"]

    PATH_PATTERNS = [
        r'file_get_contents\s*\(\s*\$_(GET|POST|REQUEST)',
        r'fopen\s*\(\s*\$_(GET|POST|REQUEST)',
        r'file\s*\(\s*\$_(GET|POST|REQUEST)',
        r'readfile\s*\(\s*\$_(GET|POST|REQUEST)',
        r'copy\s*\([^,]*\$_(GET|POST|REQUEST)',
        r'unlink\s*\(\s*\$_(GET|POST|REQUEST)',
        r'rmdir\s*\(\s*\$_(GET|POST|REQUEST)',
        r'move_uploaded_file\s*\([^,]*,\s*\$',
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
                            message="Potential path traversal - use realpath() and validate against base directory",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result
