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


@RuleRegistry.register
class JSMagicNumberRule(Rule):
    """Detect magic numbers that should be named constants."""

    id = "javascript:S109"
    name = "Magic Number"
    description = "Magic numbers should be replaced with named constants"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 5
    tags = ["maintainability", "readability"]
    languages = ["javascript", "typescript"]

    params = {"allowed_numbers": [0, 1, -1, 2, 100]}

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for magic numbers."""
        result = RuleResult()
        allowed = set(self.params.get("allowed_numbers", [0, 1, -1, 2, 100]))
        lines = file.source.split("\n")

        magic_count = 0
        first_line = 0

        for i, line in enumerate(lines, 1):
            # Skip comments and const/let/var declarations with clear names
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("/*"):
                continue

            # Find numbers in the line (not in strings)
            # Skip array indices and common patterns
            if re.search(r'const\s+\w+\s*=\s*\d+', line):
                continue
            if re.search(r'let\s+\w+\s*=\s*\d+', line):
                continue

            # Look for standalone magic numbers in operations
            numbers = re.findall(r'(?<!["\'\w])(\d+\.?\d*)(?!["\'\w])', line)
            for num_str in numbers:
                try:
                    num = float(num_str)
                    if num not in allowed and num > 2:
                        magic_count += 1
                        if first_line == 0:
                            first_line = i
                except ValueError:
                    pass

        if magic_count >= 5:
            result.issues.append(
                self.create_issue(
                    message=f"Found {magic_count} magic numbers - consider using named constants",
                    file_path=file.path,
                    start_line=first_line,
                    snippet=self.get_snippet(file, first_line),
                )
            )

        return result


@RuleRegistry.register
class JSEmptyCatchRule(Rule):
    """Detect empty catch blocks."""

    id = "javascript:S2486"
    name = "Empty Catch Block"
    description = "Catch blocks should not be empty"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    cwe_ids = [390, 391]
    effort_minutes = 15
    tags = ["error-handling", "maintainability"]
    languages = ["javascript", "typescript"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for empty catch blocks."""
        result = RuleResult()
        lines = file.source.split("\n")

        i = 0
        while i < len(lines):
            line = lines[i]

            # Look for catch blocks
            if re.search(r'catch\s*\([^)]*\)\s*\{', line):
                catch_line = i + 1

                # Find the matching closing brace
                brace_count = line.count("{") - line.count("}")
                content_lines = []
                j = i + 1

                while j < len(lines) and brace_count > 0:
                    next_line = lines[j]
                    brace_count += next_line.count("{") - next_line.count("}")
                    # Collect non-empty lines
                    stripped = next_line.strip()
                    if stripped and stripped != "}":
                        content_lines.append(stripped)
                    j += 1

                # Check if catch block is empty (only has comments or nothing)
                non_comment_content = [
                    l for l in content_lines
                    if not l.startswith("//") and not l.startswith("/*") and l != "*/"
                ]

                if not non_comment_content:
                    result.issues.append(
                        self.create_issue(
                            message="Empty catch block - exceptions should be handled or logged",
                            file_path=file.path,
                            start_line=catch_line,
                            snippet=self.get_snippet(file, catch_line),
                        )
                    )

            i += 1

        return result


@RuleRegistry.register
class JSCallbackHellRule(Rule):
    """Detect callback hell / pyramid of doom patterns."""

    id = "javascript:S5765"
    name = "Callback Hell"
    description = "Deeply nested callbacks should be refactored using async/await or Promises"
    severity = Severity.MAJOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 30
    tags = ["async", "maintainability", "readability"]
    languages = ["javascript", "typescript"]

    params = {"max_callback_depth": 3}

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for callback hell patterns."""
        result = RuleResult()
        max_depth = self.params.get("max_callback_depth", 3)
        lines = file.source.split("\n")

        # Look for nested callback patterns
        callback_pattern = r'function\s*\([^)]*\)\s*\{|=>\s*\{'

        for i, line in enumerate(lines, 1):
            # Count callback nesting by looking at context
            context_start = max(0, i - 10)
            context = '\n'.join(lines[context_start:i])

            # Count nested functions/arrows in context leading to this line
            callback_matches = len(re.findall(callback_pattern, context))

            # Estimate nesting from indentation and callbacks
            indent = len(line) - len(line.lstrip())
            if indent > 16 and callback_matches >= max_depth:
                # Check if this line has a callback
                if re.search(callback_pattern, line):
                    result.issues.append(
                        self.create_issue(
                            message=f"Callback nesting too deep ({callback_matches} levels) - consider using async/await",
                            file_path=file.path,
                            start_line=i,
                            snippet=self.get_snippet(file, i),
                        )
                    )
                    break  # One issue per file

        return result


@RuleRegistry.register
class JSDuplicatedStringsRule(Rule):
    """Detect duplicated string literals."""

    id = "javascript:S1192"
    name = "Duplicated String"
    description = "String literals should not be duplicated"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 5
    tags = ["maintainability", "dry"]
    languages = ["javascript", "typescript"]

    params = {"min_length": 5, "max_duplicates": 3}

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for duplicated strings."""
        result = RuleResult()
        min_length = self.params.get("min_length", 5)
        max_dupes = self.params.get("max_duplicates", 3)

        # Find all string literals
        string_pattern = r'["\']([^"\']{%d,})["\']' % min_length
        strings: dict[str, list[int]] = {}

        lines = file.source.split("\n")
        for i, line in enumerate(lines, 1):
            # Skip imports and requires
            if 'import ' in line or 'require(' in line:
                continue
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("//"):
                continue

            matches = re.findall(string_pattern, line)
            for match in matches:
                # Skip likely non-duplicable strings
                if match.startswith("http") or match.startswith("/"):
                    continue
                if match not in strings:
                    strings[match] = []
                strings[match].append(i)

        # Report strings that appear too many times
        for string, occurrences in strings.items():
            if len(occurrences) > max_dupes:
                result.issues.append(
                    self.create_issue(
                        message=f"String '{string[:30]}...' is duplicated {len(occurrences)} times - consider extracting to a constant",
                        file_path=file.path,
                        start_line=occurrences[0],
                        snippet=self.get_snippet(file, occurrences[0]),
                    )
                )

        return result


@RuleRegistry.register
class JSEmptyFunctionRule(Rule):
    """Detect empty functions that may indicate incomplete code."""

    id = "javascript:S1186"
    name = "Empty Function"
    description = "Empty functions should have a comment explaining why they are empty"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 5
    tags = ["maintainability", "incomplete-code"]
    languages = ["javascript", "typescript"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for empty functions."""
        result = RuleResult()

        for func in file.symbols.functions:
            # Check if function body is empty
            if func.end_line - func.start_line <= 2:
                lines = file.source.split("\n")
                if func.start_line <= len(lines) and func.end_line <= len(lines):
                    body = '\n'.join(lines[func.start_line - 1:func.end_line])

                    # Remove function signature and braces
                    body_content = re.sub(r'(function[^{]*\{|\}\s*$|=>\s*\{)', '', body)
                    body_content = body_content.strip()

                    # Skip if it has a comment explaining emptiness
                    if not body_content or (
                        not re.search(r'[a-zA-Z]', body_content) and
                        '//' not in body and '/*' not in body
                    ):
                        # Skip event handlers and intentional noop functions
                        if func.name not in ['noop', 'handleClick', 'handleChange', 'onChange', 'onClick']:
                            result.issues.append(
                                self.create_issue(
                                    message=f"Function '{func.name}' is empty - add implementation or comment",
                                    file_path=file.path,
                                    start_line=func.start_line,
                                    snippet=self.get_snippet(file, func.start_line),
                                )
                            )

        return result


@RuleRegistry.register
class JSNoVarRule(Rule):
    """Detect use of var instead of let/const."""

    id = "javascript:S3504"
    name = "Avoid var"
    description = "Variables should be declared with let or const instead of var"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 2
    tags = ["es6", "best-practice"]
    languages = ["javascript", "typescript"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for var declarations."""
        result = RuleResult()
        lines = file.source.split("\n")

        var_count = 0
        first_line = 0

        for i, line in enumerate(lines, 1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("//"):
                continue

            if re.search(r'\bvar\s+\w+', line):
                var_count += 1
                if first_line == 0:
                    first_line = i

        if var_count >= 3:
            result.issues.append(
                self.create_issue(
                    message=f"Found {var_count} 'var' declarations - use 'let' or 'const' instead",
                    file_path=file.path,
                    start_line=first_line,
                    snippet=self.get_snippet(file, first_line),
                )
            )

        return result


@RuleRegistry.register
class JSNoAsyncWithoutAwaitRule(Rule):
    """Detect async functions that don't use await."""

    id = "javascript:S4326"
    name = "Async Without Await"
    description = "Async functions should use await, otherwise they should not be async"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 5
    tags = ["async", "performance"]
    languages = ["javascript", "typescript"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for async functions without await."""
        result = RuleResult()
        source = file.source
        lines = source.split("\n")

        # Pattern to find async functions
        async_pattern = r'async\s+(function\s+\w+|[\w]+)\s*\([^)]*\)\s*\{'

        i = 0
        while i < len(lines):
            line = lines[i]
            if re.search(r'\basync\b', line):
                # Find the function body
                start_line = i
                brace_count = line.count("{") - line.count("}")
                j = i + 1
                body_lines = [line]

                while j < len(lines) and (brace_count > 0 or j == i + 1):
                    body_lines.append(lines[j])
                    brace_count += lines[j].count("{") - lines[j].count("}")
                    j += 1

                body = '\n'.join(body_lines)

                # Check if await is used in the body
                if 'await ' not in body and 'await(' not in body:
                    # Get function name if possible
                    name_match = re.search(r'async\s+(function\s+)?(\w+)', line)
                    func_name = name_match.group(2) if name_match else "anonymous"

                    result.issues.append(
                        self.create_issue(
                            message=f"Async function '{func_name}' doesn't use await - consider removing async",
                            file_path=file.path,
                            start_line=start_line + 1,
                            snippet=self.get_snippet(file, start_line + 1),
                        )
                    )

                i = j
            else:
                i += 1

        return result


@RuleRegistry.register
class JSTripleEqualsRule(Rule):
    """Detect use of == instead of ===."""

    id = "javascript:S1440"
    name = "Use Strict Equality"
    description = "Use strict equality (===) instead of loose equality (==)"
    severity = Severity.MINOR
    issue_type = IssueType.CODE_SMELL
    effort_minutes = 2
    tags = ["best-practice", "equality"]
    languages = ["javascript", "typescript"]

    def check(self, file: ParsedFile) -> RuleResult:
        """Check for loose equality comparisons."""
        result = RuleResult()
        lines = file.source.split("\n")

        loose_eq_count = 0
        first_line = 0

        for i, line in enumerate(lines, 1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("//"):
                continue

            # Find == that's not === or !==
            # We need to be careful not to match === or !==
            if re.search(r'(?<![!=])==(?!=)', line):
                loose_eq_count += 1
                if first_line == 0:
                    first_line = i

            if re.search(r'(?<![!=])!=(?!=)', line):
                loose_eq_count += 1
                if first_line == 0:
                    first_line = i

        if loose_eq_count >= 3:
            result.issues.append(
                self.create_issue(
                    message=f"Found {loose_eq_count} loose equality comparisons - use === and !== instead",
                    file_path=file.path,
                    start_line=first_line,
                    snippet=self.get_snippet(file, first_line),
                )
            )

        return result
