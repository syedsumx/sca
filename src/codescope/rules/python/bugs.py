"""Python bug detection rules."""

import ast
from typing import Any

from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class MutableDefaultArgumentRule(Rule):
    """Detect mutable default arguments in function definitions."""

    id = "python:S5765"
    name = "Mutable Default Argument"
    description = "Function default arguments should not be mutable objects"
    severity = Severity.MAJOR
    issue_type = IssueType.BUG
    cwe_ids = [1321]
    effort_minutes = 10
    tags = ["bug", "python-gotcha"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for mutable default arguments."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for default in node.args.defaults + node.args.kw_defaults:
                    if default and self._is_mutable(default):
                        result.issues.append(
                            self.create_issue(
                                message=f"Function '{node.name}' has a mutable default argument. Use None and initialize in function body.",
                                file_path=file.path,
                                start_line=node.lineno,
                                snippet=self.get_snippet(file, node.lineno),
                            )
                        )
                        break  # One issue per function

        return result

    def _is_mutable(self, node: ast.AST) -> bool:
        """Check if node represents a mutable default value."""
        # Lists, dicts, sets are mutable
        if isinstance(node, (ast.List, ast.Dict, ast.Set)):
            return True
        # Empty list/dict/set created via call
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in ("list", "dict", "set"):
                    return True
        return False


@RuleRegistry.register
class ComparisonToNoneRule(Rule):
    """Detect comparison to None using == instead of 'is'."""

    id = "python:S2711"
    name = "Comparison to None"
    description = "Comparisons to None should use 'is' or 'is not'"
    severity = Severity.MINOR
    issue_type = IssueType.BUG
    effort_minutes = 2
    tags = ["bug", "style", "python-gotcha"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for comparison to None using ==."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for op, comparator in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.NotEq)):
                        if self._is_none(comparator) or self._is_none(node.left):
                            op_name = "==" if isinstance(op, ast.Eq) else "!="
                            suggested = "is" if isinstance(op, ast.Eq) else "is not"
                            result.issues.append(
                                self.create_issue(
                                    message=f"Use '{suggested}' instead of '{op_name}' when comparing to None",
                                    file_path=file.path,
                                    start_line=node.lineno,
                                    snippet=self.get_snippet(file, node.lineno),
                                )
                            )

        return result

    def _is_none(self, node: ast.AST) -> bool:
        """Check if node is None."""
        if isinstance(node, ast.Constant) and node.value is None:
            return True
        if isinstance(node, ast.NameConstant) and node.value is None:
            return True
        return False


@RuleRegistry.register
class ComparisonToTrueFalseRule(Rule):
    """Detect comparison to True/False using == instead of implicit boolean."""

    id = "python:S1125"
    name = "Comparison to Boolean"
    description = "Comparisons to True/False should be simplified"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 2
    tags = ["style", "readability"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for comparison to True/False."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                for op, comparator in zip(node.ops, node.comparators):
                    if isinstance(op, (ast.Eq, ast.NotEq, ast.Is, ast.IsNot)):
                        if self._is_boolean(comparator) or self._is_boolean(node.left):
                            result.issues.append(
                                self.create_issue(
                                    message="Simplify boolean comparison (e.g., use 'if x:' instead of 'if x == True:')",
                                    file_path=file.path,
                                    start_line=node.lineno,
                                    snippet=self.get_snippet(file, node.lineno),
                                )
                            )

        return result

    def _is_boolean(self, node: ast.AST) -> bool:
        """Check if node is True or False."""
        if isinstance(node, ast.Constant) and isinstance(node.value, bool):
            return True
        if isinstance(node, ast.NameConstant) and isinstance(node.value, bool):
            return True
        return False


@RuleRegistry.register
class BareExceptRule(Rule):
    """Detect bare except clauses."""

    id = "python:S5754"
    name = "Bare Except"
    description = "Bare 'except:' clauses should not be used"
    severity = Severity.MAJOR
    issue_type = IssueType.BUG
    cwe_ids = [396]
    effort_minutes = 10
    tags = ["bug", "error-handling"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for bare except clauses."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    result.issues.append(
                        self.create_issue(
                            message="Use specific exception types instead of bare 'except:' clause",
                            file_path=file.path,
                            start_line=node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

        return result


@RuleRegistry.register
class ExceptPassRule(Rule):
    """Detect except blocks that only contain pass."""

    id = "python:S2737"
    name = "Except Pass"
    description = "Except blocks should not just pass silently"
    severity = Severity.MAJOR
    issue_type = IssueType.BUG
    cwe_ids = [391]
    effort_minutes = 15
    tags = ["bug", "error-handling"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for except: pass patterns."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if self._is_only_pass(node.body):
                    result.issues.append(
                        self.create_issue(
                            message="Exception is silently ignored. Either handle it, log it, or re-raise it.",
                            file_path=file.path,
                            start_line=node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

        return result

    def _is_only_pass(self, body: list) -> bool:
        """Check if body only contains pass."""
        if len(body) == 1:
            if isinstance(body[0], ast.Pass):
                return True
            if isinstance(body[0], ast.Expr):
                if isinstance(body[0].value, ast.Constant):
                    if body[0].value.value is ...:
                        return True
        return False


@RuleRegistry.register
class AssertUsedRule(Rule):
    """Detect use of assert for data validation."""

    id = "python:S5915"
    name = "Assert for Validation"
    description = "Assert should not be used for data validation as it can be disabled with -O flag"
    severity = Severity.MAJOR
    issue_type = IssueType.BUG
    cwe_ids = [617]
    effort_minutes = 10
    tags = ["bug", "security"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for assert usage in validation context."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        # Heuristic: flag asserts that appear to validate user input
        validation_indicators = [
            "user", "input", "request", "param", "arg", "data", "form",
            "query", "body", "payload", "len(", "isinstance", "type("
        ]

        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                # Get the assertion text
                if hasattr(node, "lineno"):
                    line = file.get_line(node.lineno)
                    line_lower = line.lower()

                    # Check if it looks like input validation
                    if any(indicator in line_lower for indicator in validation_indicators):
                        result.issues.append(
                            self.create_issue(
                                message="Assert should not be used for data validation. Use explicit checks and raise appropriate exceptions.",
                                file_path=file.path,
                                start_line=node.lineno,
                                snippet=self.get_snippet(file, node.lineno),
                            )
                        )

        return result


@RuleRegistry.register
class GlobalStatementRule(Rule):
    """Detect use of global statement."""

    id = "python:S2890"
    name = "Global Statement"
    description = "The 'global' statement should not be used"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 15
    tags = ["maintainability", "design"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for global statements."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                result.issues.append(
                    self.create_issue(
                        message=f"Avoid using 'global' statement for variables: {', '.join(node.names)}",
                        file_path=file.path,
                        start_line=node.lineno,
                        snippet=self.get_snippet(file, node.lineno),
                    )
                )

        return result


@RuleRegistry.register
class PrintStatementRule(Rule):
    """Detect print statements that should probably be logging."""

    id = "python:S2189"
    name = "Print Statement"
    description = "Consider using logging instead of print statements"
    severity = Severity.INFO
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 5
    tags = ["maintainability", "logging"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for print statements."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        # Count print calls
        print_count = 0
        print_lines = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == "print":
                    print_count += 1
                    print_lines.append(node.lineno)

        # Only report if there are multiple prints (suggests it's not just debugging)
        if print_count >= 3:
            result.issues.append(
                self.create_issue(
                    message=f"Found {print_count} print() statements. Consider using the logging module instead.",
                    file_path=file.path,
                    start_line=print_lines[0],
                    snippet=self.get_snippet(file, print_lines[0]),
                )
            )

        return result


@RuleRegistry.register
class ReturnInFinallyRule(Rule):
    """Detect return statements in finally blocks."""

    id = "python:S5765"
    name = "Return in Finally"
    description = "Return statements in finally blocks can mask exceptions"
    severity = Severity.MAJOR
    issue_type = IssueType.BUG
    effort_minutes = 15
    tags = ["bug", "error-handling"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for return in finally blocks."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                if node.finalbody:
                    for stmt in ast.walk(ast.Module(body=node.finalbody, type_ignores=[])):
                        if isinstance(stmt, ast.Return):
                            result.issues.append(
                                self.create_issue(
                                    message="Return statement in finally block will mask any exception raised in the try block",
                                    file_path=file.path,
                                    start_line=stmt.lineno,
                                    snippet=self.get_snippet(file, stmt.lineno),
                                )
                            )

        return result


@RuleRegistry.register
class ReassignBuiltinRule(Rule):
    """Detect reassignment of Python built-in names."""

    id = "python:S2154"
    name = "Reassign Builtin"
    description = "Built-in names should not be reassigned"
    severity = Severity.MAJOR
    issue_type = IssueType.BUG
    effort_minutes = 5
    tags = ["bug", "python-gotcha"]

    BUILTINS = {
        "abs", "all", "any", "ascii", "bin", "bool", "breakpoint", "bytearray",
        "bytes", "callable", "chr", "classmethod", "compile", "complex",
        "delattr", "dict", "dir", "divmod", "enumerate", "eval", "exec",
        "filter", "float", "format", "frozenset", "getattr", "globals",
        "hasattr", "hash", "help", "hex", "id", "input", "int", "isinstance",
        "issubclass", "iter", "len", "list", "locals", "map", "max",
        "memoryview", "min", "next", "object", "oct", "open", "ord", "pow",
        "print", "property", "range", "repr", "reversed", "round", "set",
        "setattr", "slice", "sorted", "staticmethod", "str", "sum", "super",
        "tuple", "type", "vars", "zip", "__import__",
        # Common ones to avoid
        "file", "exit", "quit",
    }

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for built-in reassignment."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id in self.BUILTINS:
                            result.issues.append(
                                self.create_issue(
                                    message=f"Built-in name '{target.id}' is being reassigned",
                                    file_path=file.path,
                                    start_line=node.lineno,
                                    snippet=self.get_snippet(file, node.lineno),
                                )
                            )

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in self.BUILTINS:
                    result.issues.append(
                        self.create_issue(
                            message=f"Built-in name '{node.name}' is being used as function name",
                            file_path=file.path,
                            start_line=node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

        return result
