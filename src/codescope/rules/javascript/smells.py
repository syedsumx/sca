"""JavaScript/TypeScript code smell rules."""

import re
from codescope.core.enums import Severity, IssueType
from codescope.parsers.models import ParsedFile
from codescope.rules.base import Rule, RuleResult
from codescope.rules.registry import RuleRegistry


@RuleRegistry.register
class JSLongFunctionRule(Rule):
    """Detect functions that are too long."""

    id = "javascript:S138"
    name = "Function Too Long"
    description = "Functions should not have too many lines of code"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 30
    tags = ["maintainability", "readability"]
    languages = ["javascript", "typescript"]

    params = {"max_lines": 100}

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for long functions."""
        result = RuleResult()
        max_lines = self.params.get("max_lines", 100)

        for func in file.symbols.functions:
            body_lines = func.end_line - func.start_line
            if body_lines > max_lines:
                result.issues.append(
                    self.create_issue(
                        message=f"Function '{func.name}' has {body_lines} lines (max allowed: {max_lines})",
                        file_path=file.path,
                        start_line=func.start_line,
                        end_line=func.end_line,
                        snippet=self.get_snippet(file, func.start_line),
                    )
                )

        return result


@RuleRegistry.register
class JSDeepNestingRule(Rule):
    """Detect deeply nested code blocks."""

    id = "javascript:S134"
    name = "Deep Nesting"
    description = "Control flow statements should not be nested too deeply"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 20
    tags = ["maintainability", "complexity"]
    languages = ["javascript", "typescript"]

    params = {"max_depth": 4}

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for deep nesting."""
        result = RuleResult()
        max_depth = self.params.get("max_depth", 4)
        lines = file.source.split("\n")

        depth = 0
        max_found = 0
        max_line = 0

        for i, line in enumerate(lines, 1):
            # Count braces (simplified)
            stripped = line.strip()
            if stripped.startswith("//"):
                continue

            opens = line.count("{") - line.count("}")

            if opens > 0:
                depth += opens
                if depth > max_found:
                    max_found = depth
                    max_line = i
            elif opens < 0:
                depth += opens
                depth = max(0, depth)

        if max_found > max_depth:
            result.issues.append(
                self.create_issue(
                    message=f"Code is nested {max_found} levels deep (max allowed: {max_depth})",
                    file_path=file.path,
                    start_line=max_line,
                    snippet=self.get_snippet(file, max_line),
                )
            )

        return result


@RuleRegistry.register
class JSTooManyParametersRule(Rule):
    """Detect functions with too many parameters."""

    id = "javascript:S107"
    name = "Too Many Parameters"
    description = "Functions should not have too many parameters"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 30
    tags = ["maintainability", "design"]
    languages = ["javascript", "typescript"]

    params = {"max_params": 7}

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for functions with too many parameters."""
        result = RuleResult()
        max_params = self.params.get("max_params", 7)

        for func in file.symbols.functions:
            param_count = len(func.parameters)
            if param_count > max_params:
                result.issues.append(
                    self.create_issue(
                        message=f"Function '{func.name}' has {param_count} parameters (max allowed: {max_params})",
                        file_path=file.path,
                        start_line=func.start_line,
                        snippet=self.get_snippet(file, func.start_line),
                    )
                )

        return result


@RuleRegistry.register
class JSConsoleLogRule(Rule):
    """Detect console.log statements that should be removed."""

    id = "javascript:S2228"
    name = "Console Log"
    description = "console.log statements should be removed from production code"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 2
    tags = ["maintainability", "logging"]
    languages = ["javascript", "typescript"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for console.log statements."""
        result = RuleResult()
        lines = file.source.split("\n")

        console_count = 0
        first_line = 0

        for i, line in enumerate(lines, 1):
            if re.search(r'console\.(log|debug|info|warn|error)\s*\(', line):
                console_count += 1
                if first_line == 0:
                    first_line = i

        if console_count >= 3:
            result.issues.append(
                self.create_issue(
                    message=f"Found {console_count} console statements - consider using a proper logging library",
                    file_path=file.path,
                    start_line=first_line,
                    snippet=self.get_snippet(file, first_line),
                )
            )

        return result


@RuleRegistry.register
class JSUnusedVariableRule(Rule):
    """Detect potentially unused variables."""

    id = "javascript:S1481"
    name = "Unused Variable"
    description = "Unused variables should be removed"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 2
    tags = ["maintainability", "unused-code"]
    languages = ["javascript", "typescript"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for unused variables."""
        result = RuleResult()

        for var in file.symbols.variables:
            # Simple heuristic: count occurrences
            name = var.name
            # Skip if name starts with underscore (intentionally unused)
            if name.startswith("_"):
                continue

            occurrences = len(re.findall(rf'\b{re.escape(name)}\b', file.source))
            if occurrences == 1:  # Only the declaration
                result.issues.append(
                    self.create_issue(
                        message=f"Variable '{name}' appears to be unused",
                        file_path=file.path,
                        start_line=var.line,
                        snippet=self.get_snippet(file, var.line),
                    )
                )

        return result
