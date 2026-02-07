"""gRPC security rules for detecting vulnerabilities and misconfigurations."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class GRPCInsecureConnectionRule(Rule):
    """Detect insecure gRPC connections without TLS."""

    id = "grpc:S6600"
    name = "gRPC Insecure Connection"
    description = (
        "gRPC connections should use TLS to encrypt data in transit and prevent "
        "man-in-the-middle attacks"
    )
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [319, 311]
    owasp_categories = ["A02:2021"]
    effort_minutes = 30
    tags = ["security", "grpc", "api", "tls", "encryption"]
    languages = ["javascript", "typescript", "python", "go", "java", "csharp"]

    INSECURE_PATTERNS = [
        # Go - grpc.Dial without credentials
        (r'grpc\.Dial\s*\([^)]*grpc\.WithInsecure\s*\(\s*\)', "grpc.WithInsecure() disables TLS"),
        (r'grpc\.WithTransportCredentials\s*\(\s*insecure\.NewCredentials\s*\(\s*\)\s*\)', "insecure.NewCredentials() disables TLS"),
        # Python - grpc.insecure_channel
        (r'grpc\.insecure_channel\s*\(', "grpc.insecure_channel() creates unencrypted connection"),
        # Node.js - grpc.credentials.createInsecure
        (r'grpc\.credentials\.createInsecure\s*\(', "createInsecure() disables TLS"),
        (r'grpc\.ChannelCredentials\.createInsecure\s*\(', "createInsecure() disables TLS"),
        (r'@grpc/grpc-js.*createInsecure', "Insecure gRPC credentials"),
        # Java - usePlaintext
        (r'\.usePlaintext\s*\(\s*\)', "usePlaintext() disables TLS"),
        (r'ManagedChannelBuilder.*\.usePlaintext\s*\(', "ManagedChannelBuilder with plaintext"),
        (r'NettyChannelBuilder.*\.usePlaintext\s*\(', "NettyChannelBuilder with plaintext"),
        # C# - ChannelCredentials.Insecure
        (r'ChannelCredentials\.Insecure', "ChannelCredentials.Insecure disables TLS"),
        (r'GrpcChannel\.ForAddress\s*\([^)]*http://', "HTTP (non-TLS) gRPC channel"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure gRPC connections."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.INSECURE_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"Insecure gRPC connection - {message}. Use TLS credentials instead.",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GRPCMissingAuthRule(Rule):
    """Detect gRPC services without authentication."""

    id = "grpc:S6601"
    name = "gRPC Missing Authentication"
    description = (
        "gRPC services should implement authentication to verify client identity "
        "and prevent unauthorized access"
    )
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [306, 287]
    owasp_categories = ["A07:2021"]
    effort_minutes = 60
    tags = ["security", "grpc", "api", "authentication"]
    languages = ["javascript", "typescript", "python", "go", "java", "csharp"]

    # Patterns for gRPC server without auth interceptors
    SERVER_PATTERNS = [
        r'grpc\.NewServer\s*\(',
        r'grpc\.server\s*\(',
        r'new\s+grpc\.Server\s*\(',
        r'ServerBuilder\s*\(\s*\)',
        r'Server\.Builder\s*\(\s*\)',
    ]

    AUTH_PATTERNS = [
        r'(auth|authenticate|authorization)',
        r'(interceptor|middleware)',
        r'UnaryInterceptor',
        r'StreamInterceptor',
        r'WithUnaryInterceptor',
        r'grpc-middleware',
        r'CallCredentials',
        r'PerRPCCredentials',
        r'grpc_auth',
        r'AuthInterceptor',
        r'TokenAuth',
        r'JwtAuth',
        r'OAuth',
        r'metadata.*token',
        r'metadata.*bearer',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing gRPC authentication."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Check if file has gRPC server setup
        has_grpc_server = any(re.search(p, source, re.IGNORECASE) for p in self.SERVER_PATTERNS)
        if not has_grpc_server:
            return result

        # Check if authentication is implemented
        has_auth = any(re.search(p, source, re.IGNORECASE) for p in self.AUTH_PATTERNS)

        if not has_auth:
            for i, line in enumerate(lines, 1):
                for pattern in self.SERVER_PATTERNS:
                    if re.search(pattern, line, re.IGNORECASE):
                        result.issues.append(
                            self.create_issue(
                                message="gRPC server lacks authentication - implement auth interceptors for secure access",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        return result

        return result


@RuleRegistry.register
class GRPCMetadataInjectionRule(Rule):
    """Detect potential gRPC metadata injection vulnerabilities."""

    id = "grpc:S6602"
    name = "gRPC Metadata Injection"
    description = (
        "gRPC metadata values should be validated and sanitized to prevent "
        "header injection attacks"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [113, 93]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "grpc", "api", "injection"]
    languages = ["javascript", "typescript", "python", "go", "java"]

    INJECTION_PATTERNS = [
        # Go - direct user input in metadata
        (r'metadata\.Pairs\s*\([^)]*req\.', "User input in metadata.Pairs"),
        (r'metadata\.New\s*\([^)]*req\.', "User input in metadata.New"),
        (r'metadata\.AppendToOutgoingContext\s*\([^)]*req\.', "User input in metadata"),
        # Python - metadata with user input
        (r'grpc\.metadata\s*\(\s*\[.*req\.', "User input in gRPC metadata"),
        (r'context\.set_code\s*\([^)]*req\.', "User input in context"),
        # Node.js - metadata injection
        (r'new\s+grpc\.Metadata\s*\(\s*\).*add\s*\([^)]*req\.', "User input in Metadata"),
        (r'metadata\.set\s*\([^)]*req\.', "User input in metadata.set"),
        # Java - metadata injection
        (r'Metadata\.Key\.of\s*\([^)]*\+', "Dynamic metadata key"),
        (r'metadata\.put\s*\([^)]*request\.', "User input in metadata.put"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for metadata injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.INJECTION_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"Potential gRPC metadata injection - {message}. Validate and sanitize input.",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GRPCMissingDeadlineRule(Rule):
    """Detect gRPC calls without deadline/timeout configuration."""

    id = "grpc:S6603"
    name = "gRPC Missing Deadline"
    description = (
        "gRPC calls should have deadlines configured to prevent resource "
        "exhaustion from hanging requests"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [400, 770]
    owasp_categories = ["A05:2021"]
    effort_minutes = 15
    tags = ["security", "grpc", "api", "dos", "reliability"]
    languages = ["javascript", "typescript", "python", "go", "java"]

    # Patterns for gRPC calls without deadline
    CALL_PATTERNS = [
        r'\.Invoke\s*\(',
        r'client\.\w+\s*\(',
        r'stub\.\w+\s*\(',
    ]

    DEADLINE_PATTERNS = [
        r'deadline',
        r'timeout',
        r'WithTimeout',
        r'WithDeadline',
        r'context\.WithTimeout',
        r'context\.WithDeadline',
        r'waitForReady',
        r'maxAttempts',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing deadlines in gRPC calls."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Only check files that have gRPC imports/usage
        if 'grpc' not in source.lower():
            return result

        # Check if deadlines are configured somewhere
        has_deadline = any(re.search(p, source, re.IGNORECASE) for p in self.DEADLINE_PATTERNS)

        if not has_deadline:
            for i, line in enumerate(lines, 1):
                # Look for gRPC client calls
                if re.search(r'(client|stub)\.\w+\s*\(', line):
                    result.issues.append(
                        self.create_issue(
                            message="gRPC call may lack deadline - configure timeout to prevent hanging requests",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    # Only report once per file to avoid noise
                    return result

        return result


@RuleRegistry.register
class GRPCLargeMessageRule(Rule):
    """Detect gRPC services without message size limits."""

    id = "grpc:S6604"
    name = "gRPC Large Message Attack"
    description = (
        "gRPC services should limit message sizes to prevent denial of service "
        "attacks through excessive memory consumption"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [400, 770]
    owasp_categories = ["A05:2021"]
    effort_minutes = 20
    tags = ["security", "grpc", "api", "dos"]
    languages = ["javascript", "typescript", "python", "go", "java", "csharp"]

    SERVER_PATTERNS = [
        r'grpc\.NewServer\s*\(',
        r'grpc\.server\s*\(',
        r'new\s+grpc\.Server\s*\(',
        r'ServerBuilder\s*\(\s*\)',
    ]

    SIZE_LIMIT_PATTERNS = [
        r'maxReceiveMessageLength',
        r'maxSendMessageLength',
        r'MaxRecvMsgSize',
        r'MaxSendMsgSize',
        r'max_receive_message_length',
        r'max_send_message_length',
        r'grpc\.max_message_length',
        r'maxInboundMessageSize',
        r'maxOutboundMessageSize',
        r'SetMaxReceiveMessageSize',
        r'SetMaxSendMessageSize',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing message size limits."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Check if file has gRPC server setup
        has_grpc_server = any(re.search(p, source, re.IGNORECASE) for p in self.SERVER_PATTERNS)
        if not has_grpc_server:
            return result

        # Check if size limits are configured
        has_size_limit = any(re.search(p, source, re.IGNORECASE) for p in self.SIZE_LIMIT_PATTERNS)

        if not has_size_limit:
            for i, line in enumerate(lines, 1):
                for pattern in self.SERVER_PATTERNS:
                    if re.search(pattern, line, re.IGNORECASE):
                        result.issues.append(
                            self.create_issue(
                                message="gRPC server lacks message size limits - configure max message size to prevent DoS",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        return result

        return result


@RuleRegistry.register
class GRPCReflectionEnabledRule(Rule):
    """Detect gRPC reflection enabled in production."""

    id = "grpc:S6605"
    name = "gRPC Reflection Enabled"
    description = (
        "gRPC server reflection should be disabled in production to prevent "
        "API enumeration and information disclosure"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [200, 213]
    owasp_categories = ["A01:2021"]
    effort_minutes = 10
    tags = ["security", "grpc", "api", "information-disclosure"]
    languages = ["javascript", "typescript", "python", "go", "java", "csharp"]

    REFLECTION_PATTERNS = [
        # Go
        (r'reflection\.Register\s*\(', "gRPC reflection registered"),
        # Python
        (r'grpc_reflection\.enable_server_reflection\s*\(', "Server reflection enabled"),
        (r'add_ServerReflectionServicer_to_server\s*\(', "Reflection servicer added"),
        # Node.js
        (r'grpc\.reflection\.v1alpha\.ServerReflection', "Server reflection service"),
        (r'addReflectionService\s*\(', "Reflection service added"),
        # Java
        (r'ProtoReflectionService\.newInstance\s*\(', "Proto reflection service enabled"),
        (r'ServerReflection\.newInstance\s*\(', "Server reflection enabled"),
        # C#
        (r'\.AddGrpcReflection\s*\(', "gRPC reflection added"),
        (r'MapGrpcReflectionService\s*\(', "Reflection service mapped"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for gRPC reflection exposure."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.REFLECTION_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"gRPC reflection may expose API structure - {message}. Disable in production.",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GRPCInputValidationRule(Rule):
    """Detect gRPC handlers without input validation."""

    id = "grpc:S6606"
    name = "gRPC Missing Input Validation"
    description = (
        "gRPC service handlers should validate incoming message fields to "
        "prevent injection attacks and data integrity issues"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [20, 1284]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "grpc", "api", "validation"]
    languages = ["javascript", "typescript", "python", "go", "java"]

    # Patterns for handler definitions
    HANDLER_PATTERNS = [
        r'func\s*\(\w+\s+\*\w+\)\s+\w+\s*\(\s*ctx\s+context\.Context',  # Go handler
        r'def\s+\w+\s*\(\s*self\s*,\s*request',  # Python handler
        r'async\s+\w+\s*\(\s*call\s*\)',  # Node.js handler
        r'public\s+.*\s+\w+\s*\(\s*\w+Request',  # Java handler
    ]

    VALIDATION_PATTERNS = [
        r'validate',
        r'Validate\s*\(\s*\)',
        r'\.Validate\s*\(',
        r'validator\.',
        r'schema\.',
        r'zod\.',
        r'joi\.',
        r'yup\.',
        r'IsValid\s*\(',
        r'check_field',
        r'assert',
        r'require\s*\(',
        r'if\s+.*==\s*[\'"]',
        r'if\s+len\s*\(',
        r'if\s+request\.',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing input validation in handlers."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Only check gRPC-related files
        if 'grpc' not in source.lower() and 'proto' not in source.lower():
            return result

        # Check if file has handler patterns but no validation
        has_handlers = any(re.search(p, source) for p in self.HANDLER_PATTERNS)
        if not has_handlers:
            return result

        has_validation = any(re.search(p, source, re.IGNORECASE) for p in self.VALIDATION_PATTERNS)

        if not has_validation:
            for i, line in enumerate(lines, 1):
                for pattern in self.HANDLER_PATTERNS:
                    if re.search(pattern, line):
                        result.issues.append(
                            self.create_issue(
                                message="gRPC handler may lack input validation - validate message fields before processing",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        return result

        return result


@RuleRegistry.register
class GRPCSSRFRule(Rule):
    """Detect potential SSRF through gRPC service calls."""

    id = "grpc:S6607"
    name = "gRPC SSRF"
    description = (
        "gRPC client connections to user-controlled addresses can lead to "
        "Server-Side Request Forgery attacks"
    )
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [918]
    owasp_categories = ["A10:2021"]
    effort_minutes = 30
    tags = ["security", "grpc", "api", "ssrf"]
    languages = ["javascript", "typescript", "python", "go", "java"]

    SSRF_PATTERNS = [
        # Go
        (r'grpc\.Dial\s*\([^)]*req\.', "User input in grpc.Dial address"),
        (r'grpc\.DialContext\s*\([^)]*req\.', "User input in grpc.DialContext"),
        # Python
        (r'grpc\.(insecure_)?channel\s*\([^)]*request\.', "User input in gRPC channel"),
        # Node.js
        (r'new\s+\w+Client\s*\([^)]*req\.', "User input in gRPC client address"),
        (r'grpc\.\w+\s*\([^)]*req\.query', "User input in gRPC connection"),
        # Java
        (r'ManagedChannelBuilder\.forAddress\s*\([^)]*request\.', "User input in channel builder"),
        # Generic
        (r'connect\s*\([^)]*\+', "Dynamic connection string"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for SSRF through gRPC connections."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.SSRF_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"Potential gRPC SSRF - {message}. Validate target addresses against allowlist.",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GRPCMissingRateLimitRule(Rule):
    """Detect gRPC services without rate limiting."""

    id = "grpc:S6608"
    name = "gRPC Missing Rate Limiting"
    description = (
        "gRPC services should implement rate limiting to prevent abuse and "
        "denial of service attacks"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [770, 799]
    owasp_categories = ["A05:2021"]
    effort_minutes = 45
    tags = ["security", "grpc", "api", "dos", "rate-limiting"]
    languages = ["javascript", "typescript", "python", "go", "java"]

    SERVER_PATTERNS = [
        r'grpc\.NewServer\s*\(',
        r'grpc\.server\s*\(',
        r'new\s+grpc\.Server\s*\(',
    ]

    RATE_LIMIT_PATTERNS = [
        r'ratelimit',
        r'rate_limit',
        r'rateLimiter',
        r'throttle',
        r'Throttle',
        r'limiter',
        r'Limiter',
        r'maxConcurrent',
        r'semaphore',
        r'token.*bucket',
        r'leaky.*bucket',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing rate limiting."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Check if file has gRPC server setup
        has_grpc_server = any(re.search(p, source, re.IGNORECASE) for p in self.SERVER_PATTERNS)
        if not has_grpc_server:
            return result

        # Check if rate limiting is implemented
        has_rate_limit = any(re.search(p, source, re.IGNORECASE) for p in self.RATE_LIMIT_PATTERNS)

        if not has_rate_limit:
            for i, line in enumerate(lines, 1):
                for pattern in self.SERVER_PATTERNS:
                    if re.search(pattern, line, re.IGNORECASE):
                        result.issues.append(
                            self.create_issue(
                                message="gRPC server lacks rate limiting - implement throttling to prevent abuse",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        return result

        return result


@RuleRegistry.register
class GRPCCertValidationRule(Rule):
    """Detect disabled certificate validation in gRPC."""

    id = "grpc:S6609"
    name = "gRPC Certificate Validation Disabled"
    description = (
        "gRPC TLS certificate validation should not be disabled as it allows "
        "man-in-the-middle attacks"
    )
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [295, 297]
    owasp_categories = ["A07:2021"]
    effort_minutes = 20
    tags = ["security", "grpc", "api", "tls", "certificates"]
    languages = ["javascript", "typescript", "python", "go", "java", "csharp"]

    UNSAFE_PATTERNS = [
        # Go
        (r'InsecureSkipVerify\s*:\s*true', "TLS verification skipped"),
        (r'tls\.Config\s*\{[^}]*InsecureSkipVerify', "Insecure TLS config"),
        # Python
        (r'grpc\.ssl_channel_credentials\s*\([^)]*verify\s*=\s*False', "SSL verification disabled"),
        (r'ssl\.create_default_context\s*\([^)]*check_hostname\s*=\s*False', "Hostname check disabled"),
        # Node.js
        (r'rejectUnauthorized\s*:\s*false', "Certificate rejection disabled"),
        (r'checkServerIdentity\s*:\s*\(\s*\)\s*=>\s*undefined', "Server identity check bypassed"),
        # Java
        (r'TrustAllCerts', "Trust all certificates pattern"),
        (r'X509TrustManager.*checkServerTrusted.*\{\s*\}', "Empty trust manager"),
        (r'HostnameVerifier.*verify.*return\s+true', "Hostname verification bypassed"),
        # C#
        (r'ServerCertificateValidationCallback\s*=.*=>\s*true', "Certificate validation bypassed"),
        (r'DangerousAcceptAnyServerCertificateValidator', "Dangerous certificate validator"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for disabled certificate validation."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.UNSAFE_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"gRPC certificate validation disabled - {message}. Enable proper TLS verification.",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GRPCHealthCheckExposureRule(Rule):
    """Detect exposed gRPC health check endpoints."""

    id = "grpc:S6610"
    name = "gRPC Health Check Exposure"
    description = (
        "gRPC health check endpoints should be protected or limited to prevent "
        "information disclosure about service status"
    )
    severity = Severity.MINOR
    issue_type = IssueType.SECURITY_HOTSPOT
    cwe_ids = [200]
    owasp_categories = ["A01:2021"]
    effort_minutes = 15
    tags = ["security", "grpc", "api", "health-check"]
    languages = ["javascript", "typescript", "python", "go", "java"]

    HEALTH_PATTERNS = [
        (r'grpc_health_v1\.RegisterHealthServer\s*\(', "Health server registered publicly"),
        (r'health\.NewServer\s*\(', "Health server created"),
        (r'HealthCheckService', "Health check service exposed"),
        (r'grpc\.health\.v1\.Health', "gRPC health service"),
        (r'add_HealthServicer_to_server\s*\(', "Health servicer added to server"),
    ]

    AUTH_PATTERNS = [
        r'auth.*health',
        r'health.*auth',
        r'interceptor',
        r'middleware',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for exposed health check endpoints."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Check if auth is applied to health checks
        has_auth_for_health = any(re.search(p, source, re.IGNORECASE) for p in self.AUTH_PATTERNS)

        if has_auth_for_health:
            return result

        for i, line in enumerate(lines, 1):
            for pattern, message in self.HEALTH_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"gRPC health check may expose service status - {message}. Consider access controls.",
                            file_path=file.path,
                            start_line=i,
                            severity=Severity.INFO,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result
