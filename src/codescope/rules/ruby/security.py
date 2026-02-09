"""Ruby security rules."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class RubySQLInjectionRule(Rule):
    """Detect SQL injection vulnerabilities in Ruby."""

    id = "ruby:S3649"
    name = "SQL Injection"
    description = "SQL queries should use parameterized queries or ActiveRecord safely"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [89]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "sql", "injection", "owasp-top10", "rails"]
    languages = ["ruby"]

    SQL_PATTERNS = [
        r'\.where\s*\(\s*["\'].*#\{',  # String interpolation in where
        r'\.find_by_sql\s*\(\s*["\'].*#\{',
        r'\.execute\s*\(\s*["\'].*#\{',
        r'\.select\s*\(\s*["\'].*#\{',
        r'\.order\s*\(\s*["\'].*#\{',
        r'\.group\s*\(\s*["\'].*#\{',
        r'\.having\s*\(\s*["\'].*#\{',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for SQL injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.SQL_PATTERNS:
                if re.search(pattern, line):
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
class RubyCommandInjectionRule(Rule):
    """Detect OS command injection in Ruby."""

    id = "ruby:S2076"
    name = "Command Injection"
    description = "OS commands should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [78]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "command-injection", "owasp-top10"]
    languages = ["ruby"]

    DANGEROUS_PATTERNS = [
        r'`[^`]*#\{',  # Backtick with interpolation
        r'system\s*\(\s*["\'].*#\{',
        r'exec\s*\(\s*["\'].*#\{',
        r'%x\[[^\]]*#\{',
        r'IO\.popen\s*\([^)]*#\{',
        r'Open3\.',
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
                            message="Potential command injection - use Shellwords.escape() for user input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyXSSRule(Rule):
    """Detect XSS vulnerabilities in Ruby/Rails."""

    id = "ruby:S5131"
    name = "Cross-Site Scripting (XSS)"
    description = "User input should be sanitized before rendering"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [79]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "xss", "owasp-top10", "rails"]
    languages = ["ruby"]

    XSS_PATTERNS = [
        r'\.html_safe',
        r'raw\s*\(',
        r'<%==',  # Rails unescaped output
        r'render\s+inline:',
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
                            message="Potential XSS - avoid html_safe and raw unless content is trusted",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyMassAssignmentRule(Rule):
    """Detect mass assignment vulnerabilities in Rails."""

    id = "ruby:S5334"
    name = "Mass Assignment"
    description = "Models should use strong parameters to prevent mass assignment"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [915]
    owasp_categories = ["A04:2021"]
    effort_minutes = 20
    tags = ["security", "mass-assignment", "rails"]
    languages = ["ruby"]

    DANGEROUS_PATTERNS = [
        r'\.update_attributes\s*\(\s*params\s*\)',
        r'\.create\s*\(\s*params\s*\)',
        r'\.new\s*\(\s*params\s*\)',
        r'attr_accessible\s*$',  # Empty attr_accessible
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for mass assignment vulnerabilities."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.DANGEROUS_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential mass assignment vulnerability - use strong parameters (params.require().permit())",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyHardcodedSecretRule(PatternRule):
    """Detect hardcoded secrets in Ruby."""

    id = "ruby:S2068"
    name = "Hardcoded Credentials"
    description = "Credentials should not be hardcoded in source code"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [798, 259]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "credentials", "secrets", "owasp-top10"]
    languages = ["ruby"]

    patterns = [
        r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']{3,}["\']',
        r'(?i)(api_key|apikey)\s*=\s*["\'][^"\']{8,}["\']',
        r'(?i)(secret|secret_key)\s*=\s*["\'][^"\']{8,}["\']',
        r'(?i)(token|auth_token|access_token)\s*=\s*["\'][^"\']{8,}["\']',
        r'-----BEGIN (RSA |DSA |EC )?PRIVATE KEY-----',
    ]

    exclude_patterns = [
        r'=\s*["\'](\*+|xxx+|<[^>]+>|changeme|example)["\']',
        r'ENV\[',
        r'Rails\.application\.credentials',
    ]


@RuleRegistry.register
class RubySSRFRule(Rule):
    """Detect Server-Side Request Forgery vulnerabilities in Ruby."""

    id = "ruby:S5144"
    name = "SSRF (Server-Side Request Forgery)"
    description = "URLs for outbound requests should be validated to prevent SSRF attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [918]
    owasp_categories = ["A10:2021"]
    effort_minutes = 30
    tags = ["security", "ssrf", "owasp-top10", "rails"]
    languages = ["ruby"]

    SSRF_PATTERNS = [
        r'Net::HTTP\.(get|post)\s*\([^)]*params\[',
        r'open\s*\(\s*params\[',
        r'URI\.parse\s*\(\s*params\[',
        r'HTTParty\.(get|post)\s*\([^)]*params\[',
        r'Faraday\.(get|post)\s*\([^)]*params\[',
        r'RestClient\.(get|post)\s*\([^)]*params\[',
        r'Excon\.(get|post)\s*\([^)]*params\[',
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
class RubyYAMLDeserializationRule(Rule):
    """Detect unsafe YAML deserialization in Ruby."""

    id = "ruby:S5135"
    name = "Unsafe YAML Deserialization"
    description = "YAML.load can execute arbitrary code - use YAML.safe_load instead"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [502]
    owasp_categories = ["A08:2021"]
    effort_minutes = 15
    tags = ["security", "deserialization", "yaml", "owasp-top10"]
    languages = ["ruby"]

    YAML_PATTERNS = [
        r'YAML\.load\s*\(',
        r'Psych\.load\s*\(',
        r'YAML\.load_file\s*\(',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for unsafe YAML loading."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.YAML_PATTERNS:
                if re.search(pattern, line):
                    # Check if it's safe_load
                    if 'safe_load' not in line:
                        result.issues.append(
                            self.create_issue(
                                message="Unsafe YAML.load - use YAML.safe_load() to prevent code execution",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                    break

        return result


@RuleRegistry.register
class RubyOpenRedirectRule(Rule):
    """Detect open redirect vulnerabilities in Ruby/Rails."""

    id = "ruby:S5146"
    name = "Open Redirect"
    description = "URLs used for redirects should be validated"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [601]
    owasp_categories = ["A01:2021"]
    effort_minutes = 20
    tags = ["security", "redirect", "phishing", "rails"]
    languages = ["ruby"]

    REDIRECT_PATTERNS = [
        r'redirect_to\s+params\[',
        r'redirect_to\s+request\.',
        r'redirect_to\s+.*#\{params',
        r'redirect_to\s+url_for\s*\(\s*params',
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
class RubySessionFixationRule(Rule):
    """Detect session fixation vulnerabilities in Ruby/Rails."""

    id = "ruby:S5128"
    name = "Session Fixation"
    description = "Session ID should be regenerated after authentication"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [384]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "session", "authentication", "rails"]
    languages = ["ruby"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for session fixation vulnerabilities."""
        result = RuleResult()
        source = file.source
        lines = source.split("\n")

        # Check if file handles authentication
        has_auth = any(x in source.lower() for x in ['authenticate', 'login', 'sign_in', 'current_user'])
        has_session = 'session[' in source or 'session.merge' in source
        has_reset = 'reset_session' in source

        if has_auth and has_session and not has_reset:
            for i, line in enumerate(lines, 1):
                if 'session[' in line:
                    result.issues.append(
                        self.create_issue(
                            message="Potential session fixation - call reset_session before setting session data after login",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyPathTraversalRule(Rule):
    """Detect path traversal vulnerabilities in Ruby."""

    id = "ruby:S2083"
    name = "Path Traversal"
    description = "File paths should be validated before use"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [22]
    owasp_categories = ["A01:2021"]
    effort_minutes = 30
    tags = ["security", "path-traversal", "owasp-top10"]
    languages = ["ruby"]

    PATH_PATTERNS = [
        r'File\.read\s*\(\s*params\[',
        r'File\.open\s*\(\s*params\[',
        r'IO\.read\s*\(\s*params\[',
        r'send_file\s+params\[',
        r'send_file\s+.*#\{params',
        r'File\.read\s*\(\s*".*#\{params',
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
                            message="Potential path traversal - use File.expand_path and validate against base directory",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyEvalRule(Rule):
    """Detect dangerous eval usage in Ruby."""

    id = "ruby:S1523"
    name = "Code Injection (eval)"
    description = "eval() should not be used with user-controlled input"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [95]
    owasp_categories = ["A03:2021"]
    effort_minutes = 45
    tags = ["security", "injection", "eval"]
    languages = ["ruby"]

    EVAL_PATTERNS = [
        r'eval\s*\(\s*params\[',
        r'eval\s*\(\s*.*#\{params',
        r'instance_eval\s*\(\s*params\[',
        r'class_eval\s*\(\s*params\[',
        r'module_eval\s*\(\s*params\[',
        r'send\s*\(\s*params\[',
        r'__send__\s*\(\s*params\[',
        r'public_send\s*\(\s*params\[',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for dangerous eval patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern in self.EVAL_PATTERNS:
                if re.search(pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message="Potential code injection - never use eval with user input",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyWeakCryptoRule(Rule):
    """Detect use of weak cryptographic algorithms in Ruby."""

    id = "ruby:S5547"
    name = "Weak Cryptography"
    description = "Weak cryptographic algorithms should not be used"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [327, 328]
    owasp_categories = ["A02:2021"]
    effort_minutes = 30
    tags = ["security", "cryptography"]
    languages = ["ruby"]

    WEAK_CRYPTO_PATTERNS = [
        (r'Digest::MD5', "MD5 is cryptographically broken"),
        (r'Digest::SHA1', "SHA1 is deprecated for security use"),
        (r'OpenSSL::Cipher\.new\s*\(\s*["\']des', "DES is insecure, use AES"),
        (r'OpenSSL::Cipher\.new\s*\(\s*["\']rc4', "RC4 is insecure"),
        (r'rand\s*\(', "rand() is predictable - use SecureRandom for security"),
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
class RubyInsecureCookieRule(Rule):
    """Detect insecure cookie settings in Ruby/Rails."""

    id = "ruby:S2092"
    name = "Insecure Cookie"
    description = "Cookies should be created with security attributes"
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [614, 1004]
    owasp_categories = ["A05:2021"]
    effort_minutes = 10
    tags = ["security", "cookie", "session", "rails"]
    languages = ["ruby"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure cookie settings."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            # Check cookies.permanent or cookies.signed without secure
            if 'cookies[' in line or 'cookies.permanent' in line or 'cookies.signed' in line:
                if 'secure:' not in line and 'httponly:' not in line:
                    result.issues.append(
                        self.create_issue(
                            message="Cookie may be insecure - set secure: true and httponly: true",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )

            # Check session store configuration
            if 'config.session_store' in line and 'secure:' not in line:
                result.issues.append(
                    self.create_issue(
                        message="Session cookie may be insecure - configure with secure: true",
                        file_path=file.path,
                        start_line=i,
                        severity=Severity.MAJOR,
                        snippet=self.get_snippet(file, i),
                    )
                )

        return result


@RuleRegistry.register
class RubyRegexDoSRule(Rule):
    """Detect Regular Expression Denial of Service vulnerabilities in Ruby."""

    id = "ruby:S5852"
    name = "ReDoS (Regex DoS)"
    description = "Regular expressions should not be vulnerable to catastrophic backtracking"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [1333, 400]
    owasp_categories = ["A06:2021"]
    effort_minutes = 45
    tags = ["security", "regex", "dos"]
    languages = ["ruby"]

    REDOS_PATTERNS = [
        r'Regexp\.new\s*\(\s*params\[',
        r'/.*#\{params.*/',
        r'=~\s*/.*#\{params',
        r'\.match\s*\(\s*/.*#\{params',
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
                            message="Potential ReDoS - never use user input in regex patterns without validation",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class RubyCSRFRule(Rule):
    """Detect missing CSRF protection in Ruby/Rails."""

    id = "ruby:S5131"
    name = "Missing CSRF Protection"
    description = "Controllers should have CSRF protection enabled"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [352]
    owasp_categories = ["A05:2021"]
    effort_minutes = 15
    tags = ["security", "csrf", "rails"]
    languages = ["ruby"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing CSRF protection."""
        result = RuleResult()
        source = file.source
        lines = source.split("\n")

        # Check if this is a controller
        is_controller = 'Controller' in source and 'class' in source

        if is_controller:
            # Check for skip_forgery_protection without restriction
            has_skip = 'skip_forgery_protection' in source or 'skip_before_action :verify_authenticity_token' in source
            is_api = 'API' in source or 'Api' in source or '::API' in source

            if has_skip and not is_api:
                for i, line in enumerate(lines, 1):
                    if 'skip_forgery_protection' in line or 'skip_before_action :verify_authenticity_token' in line:
                        result.issues.append(
                            self.create_issue(
                                message="CSRF protection is disabled - only skip for API-only controllers",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        break

        return result
