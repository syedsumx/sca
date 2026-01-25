"""Base classes for detection rules."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codescope.core.enums import Severity, IssueType
from codescope.core.models import Issue, Location
from codescope.parsers.models import ParsedFile


@dataclass
class RuleResult:
    """Result of running a rule on a file."""

    issues: list[Issue] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Rule(ABC):
    """Abstract base class for all detection rules."""

    # Rule identification
    id: str = ""
    name: str = ""
    description: str = ""

    # Classification
    severity: Severity = Severity.MAJOR
    issue_type: IssueType = IssueType.CODE_SMELL

    # Security classification
    cwe_ids: list[int] = []
    owasp_categories: list[str] = []

    # Effort to fix (in minutes)
    effort_minutes: int = 5

    # Language(s) this rule applies to
    languages: list[str] = ["python"]

    # Tags for filtering
    tags: list[str] = []

    # Default parameters (can be overridden by config)
    default_params: dict[str, Any] = {}

    def __init__(self, params: dict[str, Any] | None = None):
        """Initialize rule with optional parameters."""
        self.params = {**self.default_params, **(params or {})}

    @abstractmethod
    def check(self, file: ParsedFile) -> RuleResult:
        """Check a file for issues.

        Args:
            file: Parsed file to analyze.

        Returns:
            RuleResult containing found issues.
        """
        pass

    def applies_to(self, file: ParsedFile) -> bool:
        """Check if this rule applies to the given file.

        Args:
            file: Parsed file to check.

        Returns:
            True if rule should be applied.
        """
        return file.language in self.languages

    def create_issue(
        self,
        message: str,
        file_path: Path,
        start_line: int,
        end_line: int | None = None,
        start_column: int = 0,
        end_column: int = 0,
        snippet: str = "",
        severity: Severity | None = None,
        effort: int | None = None,
    ) -> Issue:
        """Helper to create an Issue with this rule's metadata.

        Args:
            message: Issue description.
            file_path: Path to the file.
            start_line: Starting line number.
            end_line: Ending line number (defaults to start_line).
            start_column: Starting column.
            end_column: Ending column.
            snippet: Code snippet for context.
            severity: Override default severity.
            effort: Override default effort.

        Returns:
            Configured Issue instance.
        """
        return Issue(
            rule_id=self.id,
            rule_name=self.name,
            message=message,
            location=Location(
                file_path=file_path,
                start_line=start_line,
                end_line=end_line or start_line,
                start_column=start_column,
                end_column=end_column,
                snippet=snippet,
            ),
            severity=severity or self.severity,
            issue_type=self.issue_type,
            effort_minutes=effort or self.effort_minutes,
            cwe_ids=self.cwe_ids.copy(),
            owasp_categories=self.owasp_categories.copy(),
            tags=self.tags.copy(),
        )

    def get_snippet(self, file: ParsedFile, line: int, context: int = 2) -> str:
        """Extract code snippet from file.

        Args:
            file: Parsed file.
            line: Center line for snippet.
            context: Lines of context above/below.

        Returns:
            Formatted snippet string.
        """
        lines = file.lines
        start = max(0, line - context - 1)
        end = min(len(lines), line + context)

        snippet_lines = []
        for i in range(start, end):
            prefix = ">>> " if i == line - 1 else "    "
            snippet_lines.append(f"{prefix}{i + 1}: {lines[i]}")

        return "\n".join(snippet_lines)

    def to_dict(self) -> dict[str, Any]:
        """Convert rule metadata to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "severity": self.severity.value,
            "type": self.issue_type.value,
            "languages": self.languages,
            "cwe_ids": self.cwe_ids,
            "owasp_categories": self.owasp_categories,
            "effort_minutes": self.effort_minutes,
            "tags": self.tags,
        }


class PatternRule(Rule):
    """Rule that uses regex pattern matching."""

    # Patterns to search for
    patterns: list[str] = []

    # Optional: patterns that negate a match (false positive prevention)
    exclude_patterns: list[str] = []

    def check(self, file: ParsedFile) -> RuleResult:
        """Check file using regex patterns."""
        import re

        result = RuleResult()

        for i, line in enumerate(file.lines, 1):
            # Check if any pattern matches
            for pattern in self.patterns:
                if re.search(pattern, line):
                    # Check for exclusions
                    excluded = False
                    for exclude in self.exclude_patterns:
                        if re.search(exclude, line):
                            excluded = True
                            break

                    if not excluded:
                        result.issues.append(
                            self.create_issue(
                                message=self.get_message(line, pattern),
                                file_path=file.path,
                                start_line=i,
                                snippet=self.get_snippet(file, i),
                            )
                        )

        return result

    def get_message(self, line: str, pattern: str) -> str:
        """Generate issue message. Override for custom messages."""
        return self.description


class ASTRule(Rule):
    """Rule that analyzes AST nodes."""

    # Node types to check
    node_types: list[str] = []

    def check(self, file: ParsedFile) -> RuleResult:
        """Check file by walking AST."""
        result = RuleResult()

        if not file.ast:
            return result

        nodes = self._find_nodes(file.ast)
        for node in nodes:
            issues = self.check_node(node, file)
            result.issues.extend(issues)

        return result

    def _find_nodes(self, root: Any) -> list[Any]:
        """Find all nodes of specified types."""
        from codescope.parsers.models import ASTNode

        if not isinstance(root, ASTNode):
            return []

        nodes = []
        if root.type in self.node_types:
            nodes.append(root)

        for child in root.children:
            nodes.extend(self._find_nodes(child))

        return nodes

    @abstractmethod
    def check_node(self, node: Any, file: ParsedFile) -> list[Issue]:
        """Check a single AST node.

        Args:
            node: AST node to check.
            file: The parsed file.

        Returns:
            List of issues found.
        """
        pass
