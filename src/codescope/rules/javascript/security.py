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


@RuleRegistry.register
class JSNoSQLInjectionRule(Rule):
    """Detect NoSQL injection vulnerabilities."""

    id = "javascript:S5334"
    name = "NoSQL Injection"
    description = "NoSQL queries should use safe query construction to prevent injection"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [943]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "nosql", "injection", "mongodb", "owasp-top10"]
    languages = ["javascript", "typescript"]

    NOSQL_PATTERNS = [
        r'\.find\s*\(\s*\{[^}]*\$where',  # MongoDB $where operator
        r'\.findOne\s*\(\s*\{[^}]*\$where',
        r'\$where\s*:\s*[\'"`]',  # $where with string expression
        r'\.find\s*\(\s*JSON\.parse\s*\(',  # Parsing user input directly
        r'\.aggregate\s*\(\s*JSON\.parse\s*\(',
        r'\{\s*\$regex\s*:\s*[a-zA-Z_]+\s*\}',  # User-controlled regex
        r'new\s+RegExp\s*\([^)]*req\.',  # User-controlled RegExp
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for NoSQL injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.NOSQL_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential NoSQL injection - validate and sanitize user input before query construction",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSPathTraversalRule(Rule):
    """Detect path traversal vulnerabilities."""

    id = "javascript:S2083"
    name = "Path Traversal"
    description = "File paths should be validated to prevent directory traversal attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [22, 73]
    owasp_categories = ["A01:2021"]
    effort_minutes = 30
    tags = ["security", "path-traversal", "lfi", "owasp-top10"]
    languages = ["javascript", "typescript"]

    PATH_PATTERNS = [
        r'fs\.(readFile|writeFile|readdir|unlink|stat|access|mkdir|rmdir)\s*\([^)]*req\.',
        r'fs\.(readFile|writeFile|readdir|unlink|stat|access|mkdir|rmdir)Sync\s*\([^)]*req\.',
        r'path\.join\s*\([^)]*req\.',
        r'path\.resolve\s*\([^)]*req\.',
        r'require\s*\(\s*[`\'"][^`\'"]*\+',  # Dynamic require with concatenation
        r'import\s*\([^)]*\+',  # Dynamic import with concatenation
        r'sendFile\s*\([^)]*req\.',  # Express sendFile with user input
        r'res\.download\s*\([^)]*req\.',  # Express download with user input
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
class JSReDoSRule(Rule):
    """Detect Regular Expression Denial of Service vulnerabilities."""

    id = "javascript:S5852"
    name = "ReDoS (Regex DoS)"
    description = "Regular expressions should not be vulnerable to catastrophic backtracking"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [1333, 400]
    owasp_categories = ["A06:2021"]
    effort_minutes = 45
    tags = ["security", "regex", "dos", "performance"]
    languages = ["javascript", "typescript"]

    # Patterns that indicate potentially dangerous regex
    REDOS_PATTERNS = [
        r'new\s+RegExp\s*\([^)]*req\.',  # User-controlled regex
        r'/\([^)]*\+\)[^/]*\+/',  # Nested quantifiers like (a+)+
        r'/\([^)]*\*\)[^/]*\*/',  # Nested quantifiers like (a*)*
        r'/\([^)]*\?\)[^/]*\+/',  # Mixed nested quantifiers
        r'/\[[^\]]+\]\+\[[^\]]+\]\+/',  # Overlapping character classes with quantifiers
        r'/\([^|)]+\|[^|)]+\)\+/',  # Alternation with quantifier
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for ReDoS patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("//"):
                continue

            for pattern in self.REDOS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential ReDoS vulnerability - regex may cause catastrophic backtracking",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSOpenRedirectRule(Rule):
    """Detect open redirect vulnerabilities."""

    id = "javascript:S5146"
    name = "Open Redirect"
    description = "URLs used for redirects should be validated to prevent phishing attacks"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [601]
    owasp_categories = ["A01:2021"]
    effort_minutes = 20
    tags = ["security", "redirect", "phishing"]
    languages = ["javascript", "typescript"]

    REDIRECT_PATTERNS = [
        r'res\.redirect\s*\([^)]*req\.(query|params|body)',
        r'location\.href\s*=\s*[^;]*(req\.|params|query)',
        r'window\.location\s*=\s*[^;]*(req\.|params|query)',
        r'location\.replace\s*\([^)]*req\.',
        r'location\.assign\s*\([^)]*req\.',
        r'\.redirect\s*\(\s*302\s*,\s*[^)]*req\.',
        r'header\s*\(\s*[\'"]Location[\'"][^)]*req\.',
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
class JSInsecureCookieRule(Rule):
    """Detect insecure cookie settings."""

    id = "javascript:S2092"
    name = "Insecure Cookie"
    description = "Cookies should be created with security attributes (Secure, HttpOnly, SameSite)"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [614, 1004, 1275]
    owasp_categories = ["A05:2021"]
    effort_minutes = 10
    tags = ["security", "cookie", "session"]
    languages = ["javascript", "typescript"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure cookie settings."""
        result = RuleResult()
        lines = file.source.split("\n")
        source = file.source

        # Check for cookie setting patterns
        cookie_patterns = [
            (r'res\.cookie\s*\([^)]+\)', 'express'),
            (r'document\.cookie\s*=', 'browser'),
            (r'cookies\.set\s*\([^)]+\)', 'koa'),
            (r'\.setCookie\s*\([^)]+\)', 'generic'),
        ]

        for i, line in enumerate(lines, 1):
            for pattern, cookie_type in cookie_patterns:
                match = re.search(pattern, line)
                if match:
                    # Check if security options are missing
                    issues_found = []

                    # For express-style cookies, check options object
                    if 'secure' not in line.lower() and 'httponly' not in line.lower():
                        if 'httpOnly' not in line and 'secure' not in line:
                            issues_found.append("missing Secure and HttpOnly flags")

                    # For document.cookie, these flags can't be set without path
                    if cookie_type == 'browser' and 'Secure' not in line:
                        issues_found.append("browser cookie may be missing Secure flag")

                    if issues_found:
                        result.issues.append(
                            self.create_issue(
                                message=f"Cookie may be insecure - {'; '.join(issues_found)}",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                    break

        return result


@RuleRegistry.register
class JSInsecureRandomnessRule(Rule):
    """Detect use of insecure random number generation."""

    id = "javascript:S2245"
    name = "Insecure Randomness"
    description = "Math.random() should not be used for security-sensitive operations"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [338, 330]
    owasp_categories = ["A02:2021"]
    effort_minutes = 15
    tags = ["security", "cryptography", "random"]
    languages = ["javascript", "typescript"]

    # Contexts where Math.random is dangerous
    SECURITY_CONTEXT_PATTERNS = [
        r'(token|secret|key|password|salt|nonce|iv|csrf|session)',
        r'(auth|crypto|secure|encrypt|hash)',
        r'(uuid|guid|id).*random',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure randomness usage."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            if 'Math.random' in line:
                # Check if used in security-sensitive context
                line_lower = line.lower()
                is_security_context = any(
                    re.search(pattern, line_lower)
                    for pattern in self.SECURITY_CONTEXT_PATTERNS
                )

                # Also check surrounding lines for context
                context_start = max(0, i - 3)
                context_end = min(len(lines), i + 2)
                context = ' '.join(lines[context_start:context_end]).lower()

                if is_security_context or any(
                    re.search(pattern, context)
                    for pattern in self.SECURITY_CONTEXT_PATTERNS
                ):
                    result.issues.append(
                        self.create_issue(
                            message="Math.random() used in security-sensitive context - use crypto.randomBytes() instead",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )

        return result


@RuleRegistry.register
class JSSSRFRule(Rule):
    """Detect Server-Side Request Forgery vulnerabilities."""

    id = "javascript:S5144"
    name = "SSRF (Server-Side Request Forgery)"
    description = "URLs for outbound requests should be validated to prevent SSRF attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [918]
    owasp_categories = ["A10:2021"]
    effort_minutes = 30
    tags = ["security", "ssrf", "owasp-top10"]
    languages = ["javascript", "typescript"]

    SSRF_PATTERNS = [
        r'fetch\s*\([^)]*req\.(query|params|body)',
        r'axios\.(get|post|put|delete|patch)\s*\([^)]*req\.',
        r'axios\s*\(\s*\{[^}]*url[^}]*req\.',
        r'http\.get\s*\([^)]*req\.',
        r'https\.get\s*\([^)]*req\.',
        r'request\s*\([^)]*req\.',
        r'got\s*\([^)]*req\.',
        r'superagent\.(get|post)\s*\([^)]*req\.',
        r'needle\.(get|post)\s*\([^)]*req\.',
        r'urllib\.request\s*\([^)]*req\.',
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
                            message="Potential SSRF - validate and allowlist URLs from user input before making requests",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSWeakCryptoRule(Rule):
    """Detect use of weak cryptographic algorithms."""

    id = "javascript:S5547"
    name = "Weak Cryptography"
    description = "Weak cryptographic algorithms should not be used"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [327, 328]
    owasp_categories = ["A02:2021"]
    effort_minutes = 30
    tags = ["security", "cryptography"]
    languages = ["javascript", "typescript"]

    WEAK_CRYPTO_PATTERNS = [
        (r'createHash\s*\(\s*[\'"]md5[\'"]\s*\)', "MD5 is cryptographically broken"),
        (r'createHash\s*\(\s*[\'"]sha1[\'"]\s*\)', "SHA1 is deprecated for security use"),
        (r'createCipher\s*\(\s*[\'"]des[\'"', "DES is insecure, use AES instead"),
        (r'createCipher\s*\(\s*[\'"]rc4[\'"', "RC4 is insecure"),
        (r'createCipher\s*\(\s*[\'"]blowfish[\'"', "Blowfish has known weaknesses"),
        (r'CryptoJS\.MD5\s*\(', "MD5 is cryptographically broken"),
        (r'CryptoJS\.SHA1\s*\(', "SHA1 is deprecated for security use"),
        (r'CryptoJS\.DES\s*\.', "DES is insecure, use AES instead"),
        (r'CryptoJS\.RC4\s*\.', "RC4 is insecure"),
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
class JSInsecureJWTRule(Rule):
    """Detect insecure JWT configurations."""

    id = "javascript:S5659"
    name = "Insecure JWT Configuration"
    description = "JWT tokens should be properly validated with secure algorithms"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [347, 327]
    owasp_categories = ["A02:2021"]
    effort_minutes = 30
    tags = ["security", "jwt", "authentication"]
    languages = ["javascript", "typescript"]

    INSECURE_PATTERNS = [
        (r'algorithm\s*:\s*[\'"]none[\'"]', "JWT 'none' algorithm allows token forgery"),
        (r'algorithms\s*:\s*\[[^\]]*[\'"]none[\'"]', "JWT 'none' algorithm allows token forgery"),
        (r'verify\s*:\s*false', "JWT verification is disabled"),
        (r'ignoreExpiration\s*:\s*true', "JWT expiration check is disabled"),
        (r'algorithm\s*:\s*[\'"]HS256[\'"].*secret.*[\'"][^\'"{20}', "Weak JWT secret - use at least 256 bits"),
        (r'jwt\.decode\s*\([^)]*\{[^}]*complete\s*:\s*false', "JWT decoded without verification"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure JWT patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.INSECURE_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"Insecure JWT configuration - {message}",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSCORSMisconfigurationRule(Rule):
    """Detect CORS misconfigurations."""

    id = "javascript:S5122"
    name = "CORS Misconfiguration"
    description = "CORS should be configured with specific origins, not wildcards for sensitive resources"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [942, 346]
    owasp_categories = ["A05:2021"]
    effort_minutes = 20
    tags = ["security", "cors", "access-control"]
    languages = ["javascript", "typescript"]

    CORS_PATTERNS = [
        (r'Access-Control-Allow-Origin[\'"]?\s*[,:]\s*[\'"]?\*', "Wildcard CORS origin allows any site"),
        (r'origin\s*:\s*true', "Reflecting request origin in CORS header"),
        (r'Access-Control-Allow-Credentials.*true.*Access-Control-Allow-Origin.*\*',
         "CORS credentials with wildcard origin is forbidden"),
        (r'cors\s*\(\s*\)', "CORS enabled with default (permissive) settings"),
        (r'cors\s*:\s*true', "CORS enabled with default (permissive) settings"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for CORS misconfigurations."""
        result = RuleResult()
        source = file.source
        lines = source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.CORS_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"CORS misconfiguration - {message}",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSDOMClobberingRule(Rule):
    """Detect DOM clobbering vulnerabilities."""

    id = "javascript:S5765"
    name = "DOM Clobbering"
    description = "Global variable access patterns vulnerable to DOM clobbering attacks"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [79]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "dom", "xss"]
    languages = ["javascript", "typescript"]

    DOM_PATTERNS = [
        r'window\[[\'"]\w+[\'"]\]',  # window['name'] access
        r'document\.\w+\s*\|\|',  # document.x || fallback pattern
        r'window\.\w+\s*\|\|',  # window.x || fallback pattern
        r'self\.\w+\s*\|\|',  # self.x || fallback
        r'globalThis\.\w+\s*\|\|',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for DOM clobbering patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.DOM_PATTERNS:
                if re.search(pattern, line):
                    # Exclude legitimate uses
                    if 'undefined' not in line and 'typeof' not in line:
                        result.issues.append(
                            self.create_issue(
                                message="Potential DOM clobbering - use explicit property checks instead of truthy checks",
                                file_path=file.path,
                                start_line=i,
                                severity=Severity.MINOR,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        break

        return result


@RuleRegistry.register
class JSTimingAttackRule(Rule):
    """Detect timing attack vulnerabilities in string comparisons."""

    id = "javascript:S5852"
    name = "Timing Attack"
    description = "Secret comparisons should use constant-time comparison functions"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [208]
    owasp_categories = ["A02:2021"]
    effort_minutes = 15
    tags = ["security", "cryptography", "timing"]
    languages = ["javascript", "typescript"]

    TIMING_PATTERNS = [
        r'(password|secret|token|key|hash|signature)\s*===?\s*',
        r'===?\s*(password|secret|token|key|hash|signature)',
        r'\.(password|secret|token|key)\s*===?',
        r'(apiKey|api_key|apikey)\s*===?',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for timing attack vulnerabilities."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            # Skip if using safe comparison
            if 'timingSafeEqual' in line or 'constantTimeCompare' in line:
                continue

            for pattern in self.TIMING_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message="Potential timing attack - use crypto.timingSafeEqual() for secret comparisons",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSClickjackingRule(Rule):
    """Detect missing clickjacking protections."""

    id = "javascript:S5732"
    name = "Missing Clickjacking Protection"
    description = "Pages should set X-Frame-Options or CSP frame-ancestors to prevent clickjacking"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [1021]
    owasp_categories = ["A05:2021"]
    effort_minutes = 10
    tags = ["security", "clickjacking", "headers"]
    languages = ["javascript", "typescript"]

    # Patterns that indicate frame protection is being handled
    PROTECTION_PATTERNS = [
        r'X-Frame-Options',
        r'frame-ancestors',
        r'helmet\s*\(',
        r'frameguard',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing clickjacking protection."""
        result = RuleResult()
        source = file.source

        # Check if this appears to be an Express/server file
        is_server_file = any(x in source for x in ['express()', 'createServer', 'app.listen', 'app.use'])

        if is_server_file:
            has_protection = any(re.search(pattern, source, re.IGNORECASE) for pattern in self.PROTECTION_PATTERNS)

            if not has_protection:
                # Find first app.use or similar for line number
                lines = source.split("\n")
                for i, line in enumerate(lines, 1):
                    if 'app.use' in line or 'app.listen' in line:
                        result.issues.append(
                            self.create_issue(
                                message="Missing clickjacking protection - set X-Frame-Options header or use helmet middleware",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        break

        return result


@RuleRegistry.register
class JSPostMessageRule(Rule):
    """Detect insecure postMessage usage."""

    id = "javascript:S5821"
    name = "Insecure postMessage"
    description = "postMessage should validate origin and not use wildcard targetOrigin"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [346]
    owasp_categories = ["A07:2021"]
    effort_minutes = 20
    tags = ["security", "postmessage", "cross-origin"]
    languages = ["javascript", "typescript"]

    INSECURE_PATTERNS = [
        (r'\.postMessage\s*\([^)]+,\s*[\'\"]\*[\'\"]', "postMessage uses wildcard origin '*'"),
        (r'addEventListener\s*\(\s*[\'"]message[\'"][^}]*\)\s*=>\s*\{(?![^}]*origin)',
         "postMessage handler does not validate origin"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure postMessage usage."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.INSECURE_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message=f"Insecure postMessage - {message}",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSSecurityHeadersRule(Rule):
    """Detect missing security headers."""

    id = "javascript:S5728"
    name = "Missing Security Headers"
    description = "Server responses should include security headers"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [693]
    owasp_categories = ["A05:2021"]
    effort_minutes = 15
    tags = ["security", "headers"]
    languages = ["javascript", "typescript"]

    REQUIRED_HEADERS = [
        'Content-Security-Policy',
        'Strict-Transport-Security',
        'X-Content-Type-Options',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing security headers."""
        result = RuleResult()
        source = file.source

        # Check if this appears to be an Express/server file
        is_server_file = any(x in source for x in ['express()', 'createServer', 'app.listen'])

        if is_server_file:
            # Check if helmet is used (provides all security headers)
            if 'helmet' in source:
                return result

            lines = source.split("\n")
            missing_headers = []
            for header in self.REQUIRED_HEADERS:
                if header not in source:
                    missing_headers.append(header)

            if missing_headers:
                for i, line in enumerate(lines, 1):
                    if 'app.listen' in line or 'createServer' in line:
                        result.issues.append(
                            self.create_issue(
                                message=f"Missing security headers: {', '.join(missing_headers)}. Consider using helmet middleware",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        break

        return result


@RuleRegistry.register
class JSInsecureURLSchemeRule(Rule):
    """Detect javascript: URLs and other insecure schemes."""

    id = "javascript:S5334"
    name = "Insecure URL Scheme"
    description = "javascript: and data: URLs can lead to XSS vulnerabilities"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [79]
    owasp_categories = ["A03:2021"]
    effort_minutes = 20
    tags = ["security", "xss", "url"]
    languages = ["javascript", "typescript"]

    INSECURE_PATTERNS = [
        r'href\s*=\s*[\'"]javascript:',
        r'src\s*=\s*[\'"]javascript:',
        r'location\s*=\s*[\'"]javascript:',
        r'window\.open\s*\(\s*[\'"]javascript:',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure URL schemes."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.INSECURE_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message="Insecure URL scheme - javascript: URLs can lead to XSS",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class JSLogSensitiveDataRule(Rule):
    """Detect logging of sensitive data."""

    id = "javascript:S5757"
    name = "Sensitive Data Logging"
    description = "Sensitive information should not be logged"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [532]
    owasp_categories = ["A09:2021"]
    effort_minutes = 15
    tags = ["security", "logging", "sensitive-data"]
    languages = ["javascript", "typescript"]

    SENSITIVE_PATTERNS = [
        r'console\.(log|info|debug|warn|error)\s*\([^)]*password',
        r'console\.(log|info|debug|warn|error)\s*\([^)]*secret',
        r'console\.(log|info|debug|warn|error)\s*\([^)]*token',
        r'console\.(log|info|debug|warn|error)\s*\([^)]*apiKey',
        r'console\.(log|info|debug|warn|error)\s*\([^)]*creditCard',
        r'console\.(log|info|debug|warn|error)\s*\([^)]*ssn',
        r'logger\.(log|info|debug|warn|error)\s*\([^)]*password',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for sensitive data logging."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.SENSITIVE_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message="Potential sensitive data in logs - avoid logging passwords, tokens, or PII",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result
