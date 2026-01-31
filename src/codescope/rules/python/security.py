"""Python security vulnerability rules."""

import ast
import re
from typing import Any

from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult, PatternRule
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class SQLInjectionRule(Rule):
    """Detect SQL injection vulnerabilities."""

    id = "python:S3649"
    name = "SQL Injection"
    description = "SQL queries should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [89]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "sql", "injection", "owasp-top10"]

    # Dangerous patterns
    SQL_KEYWORDS = r"\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|EXECUTE)\b"

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for SQL injection patterns."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            # Check string formatting in SQL context
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
                # String % formatting
                if self._is_sql_string(node.left, file):
                    result.issues.append(
                        self.create_issue(
                            message="SQL query uses string formatting which may be vulnerable to injection",
                            file_path=file.path,
                            start_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

            # Check f-strings with SQL
            elif isinstance(node, ast.JoinedStr):
                if self._fstring_has_sql(node, file):
                    result.issues.append(
                        self.create_issue(
                            message="SQL query uses f-string interpolation which may be vulnerable to injection",
                            file_path=file.path,
                            start_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

            # Check .format() calls
            elif isinstance(node, ast.Call):
                if self._is_format_sql(node, file):
                    result.issues.append(
                        self.create_issue(
                            message="SQL query uses .format() which may be vulnerable to injection",
                            file_path=file.path,
                            start_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

                # Check execute() calls with string concatenation
                if self._is_execute_with_concat(node, file):
                    result.issues.append(
                        self.create_issue(
                            message="Database execute() with string concatenation is vulnerable to SQL injection",
                            file_path=file.path,
                            start_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

        return result

    def _is_sql_string(self, node: ast.AST, file: ParsedFile) -> bool:
        """Check if node is a SQL string."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return bool(re.search(self.SQL_KEYWORDS, node.value, re.IGNORECASE))
        return False

    def _fstring_has_sql(self, node: ast.JoinedStr, file: ParsedFile) -> bool:
        """Check if f-string contains SQL with variables."""
        # Get full string content
        has_sql = False
        has_variable = False

        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                if re.search(self.SQL_KEYWORDS, value.value, re.IGNORECASE):
                    has_sql = True
            elif isinstance(value, ast.FormattedValue):
                has_variable = True

        return has_sql and has_variable

    def _is_format_sql(self, node: ast.Call, file: ParsedFile) -> bool:
        """Check if call is .format() on SQL string."""
        if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            if isinstance(node.func.value, ast.Constant):
                value = node.func.value.value
                if isinstance(value, str):
                    return bool(re.search(self.SQL_KEYWORDS, value, re.IGNORECASE))
        return False

    def _is_execute_with_concat(self, node: ast.Call, file: ParsedFile) -> bool:
        """Check if execute() is called with concatenated string."""
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in ("execute", "executemany", "executescript"):
                if node.args:
                    arg = node.args[0]
                    # Check for string concatenation or formatting
                    if isinstance(arg, ast.BinOp):
                        if isinstance(arg.op, (ast.Add, ast.Mod)):
                            return True
                    elif isinstance(arg, ast.JoinedStr):
                        # f-string
                        return True
                    elif isinstance(arg, ast.Call):
                        if isinstance(arg.func, ast.Attribute):
                            if arg.func.attr == "format":
                                return True
        return False


@RuleRegistry.register
class CommandInjectionRule(Rule):
    """Detect OS command injection vulnerabilities."""

    id = "python:S2076"
    name = "Command Injection"
    description = "OS commands should not be constructed from user-controlled data"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [78]
    owasp_categories = ["A03:2021"]
    effort_minutes = 30
    tags = ["security", "command-injection", "owasp-top10"]

    DANGEROUS_FUNCTIONS = {
        "os": ["system", "popen", "popen2", "popen3", "popen4"],
        "subprocess": ["call", "run", "Popen", "check_output", "check_call", "getoutput", "getstatusoutput"],
        "commands": ["getoutput", "getstatusoutput"],
    }

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for command injection patterns."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        # Track imports
        dangerous_imports: dict[str, str] = {}  # alias -> module

        for node in ast.walk(tree):
            # Track imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in self.DANGEROUS_FUNCTIONS:
                        name = alias.asname or alias.name
                        dangerous_imports[name] = alias.name

            elif isinstance(node, ast.ImportFrom):
                if node.module in self.DANGEROUS_FUNCTIONS:
                    for alias in node.names:
                        if alias.name in self.DANGEROUS_FUNCTIONS.get(node.module, []):
                            name = alias.asname or alias.name
                            dangerous_imports[name] = f"{node.module}.{alias.name}"

            # Check function calls
            elif isinstance(node, ast.Call):
                is_dangerous, func_name = self._is_dangerous_call(node, dangerous_imports)
                if is_dangerous:
                    # Check if shell=True
                    shell_true = self._has_shell_true(node)

                    # Check if argument is dynamic
                    if node.args and self._is_dynamic_arg(node.args[0]):
                        message = f"'{func_name}' is called with dynamic arguments"
                        if shell_true:
                            message += " and shell=True, which is vulnerable to command injection"
                        result.issues.append(
                            self.create_issue(
                                message=message,
                                file_path=file.path,
                                start_line=node.lineno,
                                end_line=node.end_lineno or node.lineno,
                                snippet=self.get_snippet(file, node.lineno),
                            )
                        )
                    elif shell_true:
                        result.issues.append(
                            self.create_issue(
                                message=f"'{func_name}' is called with shell=True which can be dangerous",
                                file_path=file.path,
                                start_line=node.lineno,
                                severity=Severity.MAJOR,
                                snippet=self.get_snippet(file, node.lineno),
                            )
                        )

        return result

    def _is_dangerous_call(self, node: ast.Call, imports: dict[str, str]) -> tuple[bool, str]:
        """Check if call is to a dangerous function."""
        if isinstance(node.func, ast.Attribute):
            # module.function()
            if isinstance(node.func.value, ast.Name):
                module = node.func.value.id
                func = node.func.attr
                if module in imports or module in self.DANGEROUS_FUNCTIONS:
                    actual_module = imports.get(module, module)
                    if func in self.DANGEROUS_FUNCTIONS.get(actual_module.split(".")[0], []):
                        return True, f"{module}.{func}"

        elif isinstance(node.func, ast.Name):
            # Direct function call (imported)
            func_name = node.func.id
            if func_name in imports:
                return True, func_name

        return False, ""

    def _has_shell_true(self, node: ast.Call) -> bool:
        """Check if call has shell=True."""
        for keyword in node.keywords:
            if keyword.arg == "shell":
                if isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    return True
                if isinstance(keyword.value, ast.NameConstant) and keyword.value.value is True:
                    return True
        return False

    def _is_dynamic_arg(self, node: ast.AST) -> bool:
        """Check if argument is dynamically constructed."""
        if isinstance(node, ast.BinOp):
            return True
        if isinstance(node, ast.JoinedStr):  # f-string
            return True
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                return True
        if isinstance(node, ast.Name):
            return True  # Variable reference
        return False


@RuleRegistry.register
class HardcodedSecretRule(PatternRule):
    """Detect hardcoded passwords and secrets."""

    id = "python:S2068"
    name = "Hardcoded Credentials"
    description = "Credentials should not be hardcoded in source code"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [798, 259]
    owasp_categories = ["A07:2021"]
    effort_minutes = 15
    tags = ["security", "credentials", "secrets", "owasp-top10"]

    patterns = [
        # Password assignments
        r'(?i)(password|passwd|pwd)\s*=\s*["\'][^"\']{3,}["\']',
        # API keys
        r'(?i)(api[_-]?key|apikey)\s*=\s*["\'][^"\']{8,}["\']',
        # Secret keys
        r'(?i)(secret[_-]?key|secretkey)\s*=\s*["\'][^"\']{8,}["\']',
        # Auth tokens
        r'(?i)(auth[_-]?token|access[_-]?token|bearer)\s*=\s*["\'][^"\']{8,}["\']',
        # AWS keys
        r'(?i)aws[_-]?(secret[_-]?access[_-]?key|access[_-]?key[_-]?id)\s*=\s*["\'][^"\']+["\']',
        # Private keys
        r'-----BEGIN (RSA |DSA |EC |OPENSSH )?PRIVATE KEY-----',
        # Connection strings with passwords
        r'(?i)(mysql|postgres|mongodb|redis)://[^:]+:[^@]+@',
    ]

    exclude_patterns = [
        # Exclude empty or placeholder values
        r'=\s*["\'](\*+|xxx+|\.\.\.+|<[^>]+>|your[_-]?password|changeme|example)["\']',
        # Exclude environment variable lookups
        r'os\.environ|getenv|config\.',
    ]

    def get_message(self, line: str, pattern: str) -> str:
        """Generate specific message based on what was found."""
        line_lower = line.lower()
        if "password" in line_lower or "passwd" in line_lower:
            return "Hardcoded password detected - use environment variables or a secrets manager"
        elif "api" in line_lower and "key" in line_lower:
            return "Hardcoded API key detected - use environment variables or a secrets manager"
        elif "secret" in line_lower:
            return "Hardcoded secret detected - use environment variables or a secrets manager"
        elif "token" in line_lower:
            return "Hardcoded token detected - use environment variables or a secrets manager"
        elif "private key" in line_lower:
            return "Private key embedded in source code - store keys in secure key management"
        return "Hardcoded credential detected - use environment variables or a secrets manager"


@RuleRegistry.register
class InsecureHashRule(Rule):
    """Detect use of weak cryptographic hash functions."""

    id = "python:S4790"
    name = "Weak Cryptographic Hash"
    description = "Weak cryptographic hash functions like MD5 and SHA1 should not be used for security purposes"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [328, 327]
    owasp_categories = ["A02:2021"]
    effort_minutes = 20
    tags = ["security", "cryptography", "hash"]

    WEAK_HASHES = {"md5", "sha1", "md4", "md2"}

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for weak hash usage."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check hashlib.md5(), hashlib.sha1(), etc.
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr in self.WEAK_HASHES:
                        if isinstance(node.func.value, ast.Name):
                            if node.func.value.id == "hashlib":
                                result.issues.append(
                                    self.create_issue(
                                        message=f"'{node.func.attr}' is a weak hash function - use SHA-256 or stronger",
                                        file_path=file.path,
                                        start_line=node.lineno,
                                        snippet=self.get_snippet(file, node.lineno),
                                    )
                                )

                # Check hashlib.new('md5'), etc.
                elif isinstance(node.func, ast.Attribute) and node.func.attr == "new":
                    if isinstance(node.func.value, ast.Name) and node.func.value.id == "hashlib":
                        if node.args:
                            if isinstance(node.args[0], ast.Constant):
                                if str(node.args[0].value).lower() in self.WEAK_HASHES:
                                    result.issues.append(
                                        self.create_issue(
                                            message=f"'{node.args[0].value}' is a weak hash function - use SHA-256 or stronger",
                                            file_path=file.path,
                                            start_line=node.lineno,
                                            snippet=self.get_snippet(file, node.lineno),
                                        )
                                    )

        return result


@RuleRegistry.register
class InsecureRandomRule(Rule):
    """Detect use of insecure random number generation for security."""

    id = "python:S2245"
    name = "Insecure Random"
    description = "The 'random' module should not be used for security-sensitive operations"
    severity = Severity.CRITICAL
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [330, 338]
    owasp_categories = ["A02:2021"]
    effort_minutes = 10
    tags = ["security", "random", "cryptography"]

    # Security-sensitive contexts
    SECURITY_CONTEXTS = [
        "password", "token", "key", "secret", "auth", "session",
        "csrf", "nonce", "salt", "otp", "code", "pin"
    ]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for insecure random usage."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        # Track if random module is imported
        random_imported = False
        random_alias = "random"

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "random":
                        random_imported = True
                        random_alias = alias.asname or "random"

            elif isinstance(node, ast.ImportFrom):
                if node.module == "random":
                    random_imported = True

            # Check random module usage in security context
            elif isinstance(node, ast.Call) and random_imported:
                if self._is_random_call(node, random_alias):
                    # Check if used in security context
                    context = self._get_context(node, tree)
                    if self._is_security_context(context):
                        result.issues.append(
                            self.create_issue(
                                message="'random' module used in security-sensitive context - use 'secrets' module instead",
                                file_path=file.path,
                                start_line=node.lineno,
                                snippet=self.get_snippet(file, node.lineno),
                            )
                        )

        return result

    def _is_random_call(self, node: ast.Call, alias: str) -> bool:
        """Check if call is to random module."""
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                return node.func.value.id == alias
        return False

    def _get_context(self, node: ast.AST, tree: ast.AST) -> str:
        """Get surrounding context for analysis."""
        # Simple heuristic: get the line and surrounding variable names
        if hasattr(node, "lineno"):
            return str(node.lineno)
        return ""

    def _is_security_context(self, context: str) -> bool:
        """Check if context suggests security-sensitive usage."""
        context_lower = context.lower()
        return any(word in context_lower for word in self.SECURITY_CONTEXTS)


@RuleRegistry.register
class PathTraversalRule(Rule):
    """Detect path traversal vulnerabilities."""

    id = "python:S2083"
    name = "Path Traversal"
    description = "File paths should be validated before use to prevent path traversal attacks"
    severity = Severity.BLOCKER
    issue_type = IssueType.VULNERABILITY
    cwe_ids = [22]
    owasp_categories = ["A01:2021"]
    effort_minutes = 30
    tags = ["security", "path-traversal", "owasp-top10"]

    FILE_FUNCTIONS = ["open", "read", "write"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for path traversal patterns."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Check open() and similar calls
                func_name = self._get_func_name(node)
                if func_name in self.FILE_FUNCTIONS:
                    if node.args and self._is_user_controlled_path(node.args[0]):
                        result.issues.append(
                            self.create_issue(
                                message=f"'{func_name}()' with user-controlled path may allow path traversal attacks",
                                file_path=file.path,
                                start_line=node.lineno,
                                snippet=self.get_snippet(file, node.lineno),
                            )
                        )

                # Check os.path.join with user input
                if self._is_path_join(node):
                    if len(node.args) > 1 and self._is_user_controlled_path(node.args[-1]):
                        result.issues.append(
                            self.create_issue(
                                message="os.path.join() with user-controlled path component may allow path traversal",
                                file_path=file.path,
                                start_line=node.lineno,
                                severity=Severity.MAJOR,
                                snippet=self.get_snippet(file, node.lineno),
                            )
                        )

        return result

    def _get_func_name(self, node: ast.Call) -> str:
        """Get function name from call node."""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            return node.func.attr
        return ""

    def _is_path_join(self, node: ast.Call) -> bool:
        """Check if call is os.path.join."""
        if isinstance(node.func, ast.Attribute) and node.func.attr == "join":
            if isinstance(node.func.value, ast.Attribute):
                if node.func.value.attr == "path":
                    return True
        return False

    def _is_user_controlled_path(self, node: ast.AST) -> bool:
        """Check if path argument appears to be user-controlled."""
        # Check for string concatenation
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return True
        # Check for f-string
        if isinstance(node, ast.JoinedStr):
            return True
        # Check for format()
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr == "format":
                return True
        # Check for variable (heuristic - could be user input)
        if isinstance(node, ast.Name):
            name = node.id.lower()
            if any(x in name for x in ["user", "input", "request", "param", "query", "path", "file"]):
                return True
        return False
