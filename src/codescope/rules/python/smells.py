"""Python code smell detection rules."""

import ast
from typing import Any

from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class FunctionTooLongRule(Rule):
    """Detect functions that are too long."""

    id = "python:S138"
    name = "Function Too Long"
    description = "Functions should not have too many lines of code"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 20
    tags = ["maintainability", "readability"]

    default_params = {
        "max_lines": 50,
    }

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for long functions."""
        result = RuleResult()
        max_lines = self.params.get("max_lines", 50)

        for func in file.symbols.get_all_functions():
            if func.line_count > max_lines:
                result.issues.append(
                    self.create_issue(
                        message=f"Function '{func.name}' has {func.line_count} lines (max: {max_lines})",
                        file_path=file.path,
                        start_line=func.start_line,
                        end_line=func.end_line,
                        snippet=self.get_snippet(file, func.start_line),
                        effort=max(10, (func.line_count - max_lines) // 5),
                    )
                )

        return result


@RuleRegistry.register
class CyclomaticComplexityRule(Rule):
    """Detect functions with high cyclomatic complexity."""

    id = "python:S1541"
    name = "Cyclomatic Complexity"
    description = "Functions should not have too high cyclomatic complexity"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 30
    tags = ["maintainability", "complexity"]

    default_params = {
        "threshold": 10,
    }

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for complex functions."""
        result = RuleResult()
        threshold = self.params.get("threshold", 10)

        for func in file.symbols.get_all_functions():
            if func.complexity > threshold:
                result.issues.append(
                    self.create_issue(
                        message=f"Function '{func.name}' has cyclomatic complexity of {func.complexity} (max: {threshold})",
                        file_path=file.path,
                        start_line=func.start_line,
                        end_line=func.end_line,
                        snippet=self.get_snippet(file, func.start_line),
                        effort=max(15, (func.complexity - threshold) * 5),
                    )
                )

        return result


@RuleRegistry.register
class CognitiveComplexityRule(Rule):
    """Detect functions with high cognitive complexity."""

    id = "python:S3776"
    name = "Cognitive Complexity"
    description = "Cognitive complexity of functions should not be too high"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 30
    tags = ["maintainability", "complexity", "readability"]

    default_params = {
        "threshold": 15,
    }

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for cognitively complex functions."""
        result = RuleResult()
        threshold = self.params.get("threshold", 15)

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                complexity = self._calculate_cognitive_complexity(node)
                if complexity > threshold:
                    result.issues.append(
                        self.create_issue(
                            message=f"Function '{node.name}' has cognitive complexity of {complexity} (max: {threshold})",
                            file_path=file.path,
                            start_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                            effort=max(20, (complexity - threshold) * 5),
                        )
                    )

        return result

    def _calculate_cognitive_complexity(self, func_node: ast.AST) -> int:
        """Calculate cognitive complexity for a function."""
        complexity = 0
        nesting_level = 0

        def process_node(node: ast.AST, nesting: int) -> int:
            nonlocal complexity
            local_complexity = 0

            if isinstance(node, (ast.If, ast.For, ast.While, ast.AsyncFor)):
                # Nesting increment
                local_complexity += 1 + nesting
                nesting += 1

            elif isinstance(node, ast.ExceptHandler):
                local_complexity += 1 + nesting
                nesting += 1

            elif isinstance(node, ast.BoolOp):
                # Each boolean operator adds complexity
                local_complexity += len(node.values) - 1

            elif isinstance(node, ast.IfExp):
                # Ternary operator
                local_complexity += 1

            elif isinstance(node, ast.comprehension):
                # List/dict/set comprehensions
                local_complexity += 1
                if node.ifs:
                    local_complexity += len(node.ifs)

            elif isinstance(node, ast.Lambda):
                local_complexity += 1

            # Recursion adds complexity
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    if isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if node.func.id == func_node.name:
                            local_complexity += 1

            # Process children
            for child in ast.iter_child_nodes(node):
                local_complexity += process_node(child, nesting)

            return local_complexity

        for child in ast.iter_child_nodes(func_node):
            complexity += process_node(child, nesting_level)

        return complexity


@RuleRegistry.register
class DeepNestingRule(Rule):
    """Detect code with excessive nesting depth."""

    id = "python:S134"
    name = "Deep Nesting"
    description = "Control flow statements should not be nested too deeply"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 20
    tags = ["maintainability", "readability", "complexity"]

    default_params = {
        "max_depth": 4,
    }

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for deep nesting."""
        result = RuleResult()
        max_depth = self.params.get("max_depth", 4)

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        def check_nesting(node: ast.AST, depth: int, reported_lines: set) -> None:
            """Recursively check nesting depth."""
            # Nesting constructs
            nesting_types = (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith)

            new_depth = depth
            if isinstance(node, nesting_types):
                new_depth = depth + 1

                if new_depth > max_depth and node.lineno not in reported_lines:
                    reported_lines.add(node.lineno)
                    result.issues.append(
                        self.create_issue(
                            message=f"Nesting depth is {new_depth} (max: {max_depth})",
                            file_path=file.path,
                            start_line=node.lineno,
                            end_line=node.end_lineno or node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

            for child in ast.iter_child_nodes(node):
                check_nesting(child, new_depth, reported_lines)

        check_nesting(tree, 0, set())
        return result


@RuleRegistry.register
class TooManyParametersRule(Rule):
    """Detect functions with too many parameters."""

    id = "python:S107"
    name = "Too Many Parameters"
    description = "Functions should not have too many parameters"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 30
    tags = ["maintainability", "design"]

    default_params = {
        "max_params": 7,
    }

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for functions with too many parameters."""
        result = RuleResult()
        max_params = self.params.get("max_params", 7)

        for func in file.symbols.get_all_functions():
            # Count parameters (excluding self/cls for methods)
            params = func.parameters
            if func.is_method and params:
                if params[0].name in ("self", "cls"):
                    params = params[1:]

            # Don't count *args and **kwargs
            regular_params = [p for p in params if not p.is_args and not p.is_kwargs]

            if len(regular_params) > max_params:
                result.issues.append(
                    self.create_issue(
                        message=f"Function '{func.name}' has {len(regular_params)} parameters (max: {max_params})",
                        file_path=file.path,
                        start_line=func.start_line,
                        snippet=self.get_snippet(file, func.start_line),
                    )
                )

        return result


@RuleRegistry.register
class GodClassRule(Rule):
    """Detect classes that are too large (God classes)."""

    id = "python:S2230"
    name = "God Class"
    description = "Classes should not have too many methods or lines"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 60
    tags = ["maintainability", "design", "solid"]

    default_params = {
        "max_methods": 20,
        "max_lines": 500,
    }

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for god classes."""
        result = RuleResult()
        max_methods = self.params.get("max_methods", 20)
        max_lines = self.params.get("max_lines", 500)

        for cls in file.symbols.classes:
            issues_found = []

            # Check method count
            method_count = len(cls.methods)
            if method_count > max_methods:
                issues_found.append(f"{method_count} methods (max: {max_methods})")

            # Check line count
            if cls.line_count > max_lines:
                issues_found.append(f"{cls.line_count} lines (max: {max_lines})")

            if issues_found:
                result.issues.append(
                    self.create_issue(
                        message=f"Class '{cls.name}' is too large: {', '.join(issues_found)}",
                        file_path=file.path,
                        start_line=cls.start_line,
                        end_line=cls.end_line,
                        snippet=self.get_snippet(file, cls.start_line),
                        effort=60,
                    )
                )

        return result


@RuleRegistry.register
class EmptyBlockRule(Rule):
    """Detect empty code blocks."""

    id = "python:S108"
    name = "Empty Block"
    description = "Empty code blocks should be removed or have a comment explaining why they are empty"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 5
    tags = ["maintainability", "readability"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for empty blocks."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            # Check various block types
            if isinstance(node, (ast.If, ast.For, ast.While, ast.With, ast.AsyncFor, ast.AsyncWith)):
                if self._is_empty_body(node.body):
                    result.issues.append(
                        self.create_issue(
                            message=f"Empty {node.__class__.__name__.lower()} block",
                            file_path=file.path,
                            start_line=node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

            elif isinstance(node, ast.Try):
                if self._is_empty_body(node.body):
                    result.issues.append(
                        self.create_issue(
                            message="Empty try block",
                            file_path=file.path,
                            start_line=node.lineno,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

            elif isinstance(node, ast.ExceptHandler):
                if self._is_empty_body(node.body):
                    result.issues.append(
                        self.create_issue(
                            message="Empty except block - exceptions should be handled or re-raised",
                            file_path=file.path,
                            start_line=node.lineno,
                            severity=Severity.MAJOR,
                            snippet=self.get_snippet(file, node.lineno),
                        )
                    )

        return result

    def _is_empty_body(self, body: list) -> bool:
        """Check if a body is effectively empty."""
        if not body:
            return True
        # Check if only contains pass or ellipsis
        if len(body) == 1:
            stmt = body[0]
            if isinstance(stmt, ast.Pass):
                return True
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                if stmt.value.value is ...:  # Ellipsis
                    return True
        return False


@RuleRegistry.register
class UnusedVariableRule(Rule):
    """Detect unused local variables."""

    id = "python:S1481"
    name = "Unused Variable"
    description = "Local variables should not be declared and then not used"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 2
    tags = ["maintainability", "dead-code"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for unused variables."""
        result = RuleResult()

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                unused = self._find_unused_in_function(node)
                for var_name, var_line in unused:
                    # Skip variables starting with underscore (intentionally unused)
                    if var_name.startswith("_"):
                        continue
                    result.issues.append(
                        self.create_issue(
                            message=f"Variable '{var_name}' is assigned but never used",
                            file_path=file.path,
                            start_line=var_line,
                            snippet=self.get_snippet(file, var_line),
                        )
                    )

        return result

    def _find_unused_in_function(self, func_node: ast.AST) -> list[tuple[str, int]]:
        """Find unused variables in a function."""
        assigned: dict[str, int] = {}  # name -> line
        used: set[str] = set()

        # Get parameter names (these are "used")
        if isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in func_node.args.args:
                used.add(arg.arg)
            if func_node.args.vararg:
                used.add(func_node.args.vararg.arg)
            if func_node.args.kwarg:
                used.add(func_node.args.kwarg.arg)

        for node in ast.walk(func_node):
            # Track assignments
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        if target.id not in assigned:
                            assigned[target.id] = node.lineno
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    if node.target.id not in assigned:
                        assigned[node.target.id] = node.lineno

            # Track usage in expressions
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used.add(node.id)

        # Find unused
        unused = []
        for name, line in assigned.items():
            if name not in used:
                unused.append((name, line))

        return unused


@RuleRegistry.register
class DuplicateCodeRule(Rule):
    """Detect duplicate code blocks."""

    id = "python:S1192"
    name = "Duplicate String Literal"
    description = "String literals should not be duplicated"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 5
    tags = ["maintainability", "duplication"]

    default_params = {
        "min_length": 10,
        "min_occurrences": 3,
    }

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for duplicate string literals."""
        result = RuleResult()
        min_length = self.params.get("min_length", 10)
        min_occurrences = self.params.get("min_occurrences", 3)

        try:
            tree = ast.parse(file.source)
        except SyntaxError:
            return result

        # Collect string literals
        strings: dict[str, list[int]] = {}  # value -> [lines]

        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if len(value) >= min_length:
                    if value not in strings:
                        strings[value] = []
                    strings[value].append(node.lineno)

        # Report duplicates
        reported: set[str] = set()
        for value, lines in strings.items():
            if len(lines) >= min_occurrences and value not in reported:
                reported.add(value)
                # Report on first occurrence
                display_value = value[:30] + "..." if len(value) > 30 else value
                result.issues.append(
                    self.create_issue(
                        message=f"String literal \"{display_value}\" is duplicated {len(lines)} times. Consider using a constant.",
                        file_path=file.path,
                        start_line=lines[0],
                        snippet=self.get_snippet(file, lines[0]),
                    )
                )

        return result
