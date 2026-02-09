"""GraphQL security rules for detecting vulnerabilities and misconfigurations."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class GraphQLIntrospectionEnabledRule(Rule):
    """Detect GraphQL introspection enabled in production environments."""

    id = "graphql:S6524"
    name = "GraphQL Introspection Enabled"
    description = (
        "GraphQL introspection should be disabled in production to prevent "
        "attackers from discovering the API schema and finding vulnerabilities"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [200, 213]
    owasp_categories = ["A01:2021"]
    effort_minutes = 15
    tags = ["security", "graphql", "api", "information-disclosure"]
    languages = ["javascript", "typescript", "python", "go", "java", "ruby"]

    # Patterns indicating introspection is enabled or not explicitly disabled
    INTROSPECTION_PATTERNS = [
        # Apollo Server - introspection defaults to true
        (r'new\s+ApolloServer\s*\(\s*\{(?!.*introspection\s*:\s*false)', "Apollo Server has introspection enabled by default"),
        # Express GraphQL
        (r'graphqlHTTP\s*\(\s*\{(?!.*graphiql\s*:\s*false)', "express-graphql may expose GraphiQL interface"),
        # Yoga/Envelop
        (r'createYoga\s*\(\s*\{(?!.*maskedErrors)', "GraphQL Yoga may expose detailed errors"),
        # Python Graphene - Django
        (r'GRAPHENE\s*=\s*\{(?!.*\'GRAPHIQL\'\s*:\s*False)', "Django Graphene may have GraphiQL enabled"),
        # Python Strawberry
        (r'GraphQLRouter\s*\([^)]*graphiql\s*=\s*True', "Strawberry GraphQL has GraphiQL enabled"),
        # Java - graphql-java
        (r'GraphQL\.newGraphQL\s*\((?!.*noIntrospection)', "graphql-java allows introspection by default"),
        # Go - gqlgen
        (r'handler\.NewDefaultServer\s*\(', "gqlgen server should configure introspection settings"),
        # Ruby - graphql-ruby
        (r'class.*GraphQL.*Schema(?!.*disable_introspection)', "graphql-ruby schema may allow introspection"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for GraphQL introspection exposure."""
        result = RuleResult()
        lines = file.source.split("\n")
        source = file.source

        for i, line in enumerate(lines, 1):
            for pattern, message in self.INTROSPECTION_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"GraphQL introspection may be exposed - {message}",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GraphQLDepthLimitRule(Rule):
    """Detect missing query depth limiting in GraphQL."""

    id = "graphql:S6525"
    name = "GraphQL Missing Depth Limit"
    description = (
        "GraphQL queries should have depth limits to prevent deeply nested "
        "queries that can cause denial of service through resource exhaustion"
    )
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [400, 770]
    owasp_categories = ["A05:2021"]
    effort_minutes = 30
    tags = ["security", "graphql", "api", "dos", "performance"]
    languages = ["javascript", "typescript", "python", "go", "java", "ruby"]

    # Patterns for GraphQL server setup WITHOUT depth limiting
    SERVER_PATTERNS = [
        r'new\s+ApolloServer\s*\(\s*\{',
        r'graphqlHTTP\s*\(\s*\{',
        r'createYoga\s*\(\s*\{',
        r'GraphQLRouter\s*\(',
        r'GraphQL\.newGraphQL\s*\(',
        r'handler\.NewDefaultServer\s*\(',
    ]

    # Patterns indicating depth limiting is implemented
    DEPTH_LIMIT_PATTERNS = [
        r'depthLimit',
        r'maxDepth',
        r'queryDepth',
        r'MaxDepthRule',
        r'depth_limit',
        r'max_query_depth',
        r'DepthLimitRule',
        r'maxQueryDepth',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing depth limiting."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Check if file has GraphQL server setup
        has_graphql_server = any(re.search(p, source) for p in self.SERVER_PATTERNS)
        if not has_graphql_server:
            return result

        # Check if depth limiting is implemented anywhere in the file
        has_depth_limit = any(re.search(p, source, re.IGNORECASE) for p in self.DEPTH_LIMIT_PATTERNS)

        if not has_depth_limit:
            # Find the GraphQL server instantiation line
            for i, line in enumerate(lines, 1):
                for pattern in self.SERVER_PATTERNS:
                    if re.search(pattern, line):
                        result.issues.append(
                            self.create_issue(
                                message="GraphQL server lacks query depth limiting - implement depthLimit to prevent DoS attacks",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        return result  # Only report once per file

        return result


@RuleRegistry.register
class GraphQLComplexityLimitRule(Rule):
    """Detect missing query complexity limiting in GraphQL."""

    id = "graphql:S6526"
    name = "GraphQL Missing Complexity Limit"
    description = (
        "GraphQL queries should have complexity limits to prevent expensive "
        "queries that can cause denial of service through resource exhaustion"
    )
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [400, 770]
    owasp_categories = ["A05:2021"]
    effort_minutes = 45
    tags = ["security", "graphql", "api", "dos", "performance"]
    languages = ["javascript", "typescript", "python", "go", "java", "ruby"]

    # Patterns for GraphQL server setup
    SERVER_PATTERNS = [
        r'new\s+ApolloServer\s*\(\s*\{',
        r'graphqlHTTP\s*\(\s*\{',
        r'createYoga\s*\(\s*\{',
        r'GraphQLRouter\s*\(',
    ]

    # Patterns indicating complexity limiting is implemented
    COMPLEXITY_PATTERNS = [
        r'complexityLimit',
        r'queryComplexity',
        r'costAnalysis',
        r'costLimit',
        r'maxComplexity',
        r'complexity_limit',
        r'max_complexity',
        r'QueryComplexity',
        r'CostAnalysis',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing complexity limiting."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Check if file has GraphQL server setup
        has_graphql_server = any(re.search(p, source) for p in self.SERVER_PATTERNS)
        if not has_graphql_server:
            return result

        # Check if complexity limiting is implemented
        has_complexity_limit = any(re.search(p, source, re.IGNORECASE) for p in self.COMPLEXITY_PATTERNS)

        if not has_complexity_limit:
            for i, line in enumerate(lines, 1):
                for pattern in self.SERVER_PATTERNS:
                    if re.search(pattern, line):
                        result.issues.append(
                            self.create_issue(
                                message="GraphQL server lacks query complexity limiting - implement cost analysis to prevent expensive queries",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        return result

        return result


@RuleRegistry.register
class GraphQLBatchingAttackRule(Rule):
    """Detect vulnerability to GraphQL batching attacks."""

    id = "graphql:S6527"
    name = "GraphQL Batching Attack"
    description = (
        "GraphQL batch queries should be limited to prevent attackers from "
        "sending multiple operations in a single request to bypass rate limiting"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [770, 799]
    owasp_categories = ["A05:2021"]
    effort_minutes = 30
    tags = ["security", "graphql", "api", "dos", "rate-limiting"]
    languages = ["javascript", "typescript", "python", "go"]

    # Patterns for batch-enabled GraphQL configurations
    BATCH_PATTERNS = [
        (r'allowBatchedHttpRequests\s*:\s*true', "Batched HTTP requests explicitly enabled"),
        (r'batching\s*:\s*true', "Query batching is enabled"),
        (r'batch\s*:\s*true', "Batch mode is enabled"),
        (r'enableBatching', "Batching feature is enabled"),
    ]

    # Patterns indicating batching protection
    BATCH_LIMIT_PATTERNS = [
        r'batchLimit',
        r'maxBatchSize',
        r'batch.*limit',
        r'BatchLimit',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for unprotected batching."""
        result = RuleResult()
        lines = file.source.split("\n")
        source = file.source

        # Check if batching is enabled
        for i, line in enumerate(lines, 1):
            for pattern, message in self.BATCH_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    # Check if batch limiting exists in file
                    has_batch_limit = any(
                        re.search(p, source, re.IGNORECASE)
                        for p in self.BATCH_LIMIT_PATTERNS
                    )

                    if not has_batch_limit:
                        result.issues.append(
                            self.create_issue(
                                message=f"GraphQL batching enabled without limits - {message}. Implement batch size limits.",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                    break

        return result


@RuleRegistry.register
class GraphQLInjectionRule(Rule):
    """Detect GraphQL query injection vulnerabilities."""

    id = "graphql:S6528"
    name = "GraphQL Injection"
    description = (
        "GraphQL queries should use variables instead of string interpolation "
        "to prevent injection attacks"
    )
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [943, 89]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "graphql", "api", "injection", "owasp-top10"]
    languages = ["javascript", "typescript", "python", "go", "java", "ruby"]

    INJECTION_PATTERNS = [
        # Template literal injection in queries
        (r'gql\s*`[^`]*\$\{[^}]*req\.', "User input interpolated in GraphQL query"),
        (r'gql\s*`[^`]*\$\{[^}]*(params|query|body)', "Request data interpolated in GraphQL query"),
        # String concatenation in queries
        (r'query\s*[:=]\s*[\'"`].*\'\s*\+\s*', "String concatenation in GraphQL query"),
        (r'mutation\s*[:=]\s*[\'"`].*\'\s*\+\s*', "String concatenation in GraphQL mutation"),
        # Format string injection
        (r'f[\'"].*query\s*\{.*\{[^}]+\}', "Python f-string in GraphQL query"),
        (r'\.format\s*\([^)]*\).*query', "format() used with GraphQL query"),
        # Go fmt.Sprintf in queries
        (r'fmt\.Sprintf\s*\([^)]*query', "fmt.Sprintf in GraphQL query"),
        # Direct variable insertion without sanitization
        (r'query\s*\([^)]*\)\s*\{[^}]*%s', "Format specifier in GraphQL query"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for GraphQL injection patterns."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.INJECTION_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"Potential GraphQL injection - {message}. Use query variables instead.",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GraphQLErrorDisclosureRule(Rule):
    """Detect GraphQL error message information disclosure."""

    id = "graphql:S6529"
    name = "GraphQL Error Information Disclosure"
    description = (
        "GraphQL error messages should be masked in production to prevent "
        "disclosure of internal system information"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [209, 200]
    owasp_categories = ["A01:2021"]
    effort_minutes = 20
    tags = ["security", "graphql", "api", "information-disclosure"]
    languages = ["javascript", "typescript", "python", "go"]

    # Patterns for unsafe error handling
    UNSAFE_ERROR_PATTERNS = [
        (r'formatError\s*:\s*\(\s*err\s*\)\s*=>\s*err', "Raw errors exposed in formatError"),
        (r'debug\s*:\s*true', "Debug mode enabled - may expose detailed errors"),
        (r'includeStacktraceInErrorResponses\s*:\s*true', "Stack traces included in error responses"),
        (r'exposeErrorDetails\s*:\s*true', "Error details exposed"),
        (r'maskedErrors\s*:\s*false', "Masked errors explicitly disabled"),
        (r'hideErrors\s*:\s*false', "Error hiding disabled"),
    ]

    # Safe patterns
    SAFE_PATTERNS = [
        r'maskedErrors\s*:\s*true',
        r'formatError\s*:.*sanitize',
        r'formatError\s*:.*mask',
        r'hideErrors\s*:\s*true',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for error information disclosure."""
        result = RuleResult()
        lines = file.source.split("\n")
        source = file.source

        # Skip if safe error handling is detected
        if any(re.search(p, source, re.IGNORECASE) for p in self.SAFE_PATTERNS):
            return result

        for i, line in enumerate(lines, 1):
            for pattern, message in self.UNSAFE_ERROR_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"GraphQL error disclosure risk - {message}",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GraphQLFieldSuggestionRule(Rule):
    """Detect GraphQL field suggestion disclosure."""

    id = "graphql:S6530"
    name = "GraphQL Field Suggestion Disclosure"
    description = (
        "GraphQL field suggestions should be disabled in production to prevent "
        "schema enumeration through typo-based probing"
    )
    severity = Severity.MINOR
    issue_type = IssueType.SECURITY_HOTSPOT
    cwe_ids = [200]
    owasp_categories = ["A01:2021"]
    effort_minutes = 10
    tags = ["security", "graphql", "api", "information-disclosure"]
    languages = ["javascript", "typescript", "go"]

    SUGGESTION_PATTERNS = [
        (r'hideSchemaFromClients\s*:\s*false', "Schema exposed to clients"),
        (r'fieldSuggestions\s*:\s*true', "Field suggestions enabled"),
        (r'DisableFieldSuggestions\s*:\s*false', "Field suggestions not disabled"),
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for field suggestion disclosure."""
        result = RuleResult()
        lines = file.source.split("\n")

        for i, line in enumerate(lines, 1):
            for pattern, message in self.SUGGESTION_PATTERNS:
                if re.search(pattern, line, re.IGNORECASE):
                    result.issues.append(
                        self.create_issue(
                            message=f"GraphQL field suggestion may expose schema - {message}",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break

        return result


@RuleRegistry.register
class GraphQLAliasDoSRule(Rule):
    """Detect vulnerability to GraphQL alias-based DoS attacks."""

    id = "graphql:S6531"
    name = "GraphQL Alias DoS"
    description = (
        "GraphQL alias abuse should be prevented by limiting the number of "
        "aliases per query to prevent denial of service attacks"
    )
    severity = Severity.MAJOR
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [400, 770]
    owasp_categories = ["A05:2021"]
    effort_minutes = 30
    tags = ["security", "graphql", "api", "dos"]
    languages = ["javascript", "typescript", "python", "go"]

    # Patterns indicating alias limiting is NOT implemented
    SERVER_PATTERNS = [
        r'new\s+ApolloServer\s*\(',
        r'createYoga\s*\(',
        r'graphqlHTTP\s*\(',
    ]

    ALIAS_LIMIT_PATTERNS = [
        r'aliasLimit',
        r'maxAliases',
        r'alias.*limit',
        r'NoAliasRule',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for alias DoS vulnerability."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Check if file has GraphQL server setup
        has_graphql_server = any(re.search(p, source) for p in self.SERVER_PATTERNS)
        if not has_graphql_server:
            return result

        # Check if alias limiting is implemented
        has_alias_limit = any(re.search(p, source, re.IGNORECASE) for p in self.ALIAS_LIMIT_PATTERNS)

        if not has_alias_limit:
            for i, line in enumerate(lines, 1):
                for pattern in self.SERVER_PATTERNS:
                    if re.search(pattern, line):
                        result.issues.append(
                            self.create_issue(
                                message="GraphQL server may be vulnerable to alias-based DoS - consider implementing alias limits",
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        return result

        return result


@RuleRegistry.register
class GraphQLMissingAuthRule(Rule):
    """Detect GraphQL resolvers without authentication checks."""

    id = "graphql:S6532"
    name = "GraphQL Missing Authentication"
    description = (
        "GraphQL resolvers handling sensitive data should implement "
        "authentication and authorization checks"
    )
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [306, 862]
    owasp_categories = ["A01:2021", "A07:2021"]
    effort_minutes = 45
    tags = ["security", "graphql", "api", "authentication", "authorization"]
    languages = ["javascript", "typescript", "python"]

    # Patterns for sensitive resolvers without auth
    SENSITIVE_PATTERNS = [
        r'(user|account|profile|payment|order|admin|setting|config)',
    ]

    AUTH_PATTERNS = [
        r'isAuthenticated',
        r'isAuthorized',
        r'checkAuth',
        r'requireAuth',
        r'@auth',
        r'@authenticated',
        r'@authorized',
        r'context\.user',
        r'ctx\.user',
        r'authRequired',
        r'authenticate',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for missing authentication in resolvers."""
        result = RuleResult()
        lines = file.source.split("\n")
        source = file.source

        # Skip if file has no resolvers
        if 'resolver' not in source.lower() and 'Query' not in source:
            return result

        # Check if auth patterns exist in file
        has_auth = any(re.search(p, source, re.IGNORECASE) for p in self.AUTH_PATTERNS)

        if not has_auth:
            # Look for resolver definitions with sensitive data access
            for i, line in enumerate(lines, 1):
                line_lower = line.lower()
                if ('resolver' in line_lower or 'query:' in line_lower or 'mutation:' in line_lower):
                    for pattern in self.SENSITIVE_PATTERNS:
                        if re.search(pattern, line, re.IGNORECASE):
                            result.issues.append(
                                self.create_issue(
                                    message="GraphQL resolver may lack authentication - implement auth checks for sensitive data",
                                    file_path=file.path,
                                    start_line=i,
                                    snippet=self.get_snippet(file, i),
                                )
                            )
                            break

        return result


@RuleRegistry.register
class GraphQLPersistedQueriesRule(Rule):
    """Detect GraphQL configuration without persisted queries."""

    id = "graphql:S6533"
    name = "GraphQL Persisted Queries Not Enforced"
    description = (
        "Consider enforcing persisted/allowlisted queries in production to "
        "prevent arbitrary query execution and reduce attack surface"
    )
    severity = Severity.MINOR
    issue_type = IssueType.SECURITY_HOTSPOT
    cwe_ids = [284]
    owasp_categories = ["A01:2021"]
    effort_minutes = 60
    tags = ["security", "graphql", "api", "hardening"]
    languages = ["javascript", "typescript", "go"]

    PERSISTED_QUERY_PATTERNS = [
        r'persistedQueries',
        r'allowlistedQueries',
        r'whitelistedQueries',
        r'queryAllowlist',
        r'APQ',  # Automatic Persisted Queries
        r'persistedQueryLink',
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for persisted queries configuration."""
        result = RuleResult()
        source = file.source
        lines = file.source.split("\n")

        # Only check files that appear to be GraphQL server configuration
        server_patterns = [r'ApolloServer', r'createYoga', r'GraphQLServer']
        is_server_config = any(re.search(p, source) for p in server_patterns)

        if not is_server_config:
            return result

        # Check if persisted queries are configured
        has_persisted = any(re.search(p, source, re.IGNORECASE) for p in self.PERSISTED_QUERY_PATTERNS)

        if not has_persisted:
            for i, line in enumerate(lines, 1):
                for pattern in server_patterns:
                    if re.search(pattern, line):
                        result.issues.append(
                            self.create_issue(
                                message="Consider implementing persisted/allowlisted queries for production GraphQL security",
                                file_path=file.path,
                                start_line=i,
                                severity=Severity.INFO,
                                snippet=self.get_snippet(file, i),
                            )
                        )
                        return result

        return result
