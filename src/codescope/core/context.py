"""Analysis context for sharing state between analyzers."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codescope.core.config import Config
from codescope.core.models import AnalysisResults


@dataclass
class FileFilter:
    """Filter for selecting files to analyze."""

    include_patterns: list[str] = field(default_factory=lambda: ["**/*"])
    exclude_patterns: list[str] = field(default_factory=list)

    def matches(self, path: Path, root: Path) -> bool:
        """Check if a path matches the filter criteria."""
        import fnmatch

        rel_path = str(path.relative_to(root))

        # Check exclusions first
        for pattern in self.exclude_patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return False
            # Also check if any parent directory matches
            if "**" in pattern:
                # Handle recursive patterns
                simple_pattern = pattern.replace("**", "*")
                if fnmatch.fnmatch(rel_path, simple_pattern):
                    return False

        # Check inclusions
        for pattern in self.include_patterns:
            if fnmatch.fnmatch(rel_path, pattern):
                return True
            if "**" in pattern:
                simple_pattern = pattern.replace("**", "*")
                if fnmatch.fnmatch(rel_path, simple_pattern):
                    return True

        return False


@dataclass
class AnalysisContext:
    """Shared context for all analyzers during a scan."""

    project_root: Path
    config: Config
    file_filter: FileFilter = field(default_factory=FileFilter)
    results: AnalysisResults | None = None
    cache: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Initialize derived fields."""
        if self.results is None:
            self.results = AnalysisResults(project_path=self.project_root)

        # Set up file filter from config
        self.file_filter = FileFilter(
            include_patterns=self.config.sources.include,
            exclude_patterns=self.config.sources.exclude,
        )

    def is_rule_enabled(self, rule_id: str) -> bool:
        """Check if a rule is enabled."""
        return rule_id not in self.config.rules.disabled

    def get_rule_severity(self, rule_id: str, default: str) -> str:
        """Get configured severity for a rule."""
        return self.config.rules.severities.get(rule_id, default)

    def get_rule_params(self, rule_id: str) -> dict[str, Any]:
        """Get configured parameters for a rule."""
        return self.config.rules.parameters.get(rule_id, {})

    def get_files_to_analyze(self) -> list[Path]:
        """Get list of files to analyze based on filter."""
        files = []

        # File extensions to consider
        extensions = {
            ".py",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".java",
            ".go",
            ".c",
            ".cpp",
            ".h",
            ".hpp",
            ".cs",
            ".rb",
            ".php",
        }

        for path in self.project_root.rglob("*"):
            if path.is_file() and path.suffix in extensions:
                if self.file_filter.matches(path, self.project_root):
                    files.append(path)

        return sorted(files)
