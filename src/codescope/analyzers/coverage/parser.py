"""Coverage report parser and data models."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LineCoverage:
    """Coverage information for a single line."""

    line_number: int
    hits: int
    is_branch: bool = False
    branch_coverage: float | None = None  # For branch coverage


@dataclass
class FileCoverage:
    """Coverage information for a single file."""

    file_path: str
    lines: list[LineCoverage] = field(default_factory=list)

    @property
    def total_lines(self) -> int:
        """Total number of executable lines."""
        return len(self.lines)

    @property
    def covered_lines(self) -> int:
        """Number of lines that were executed."""
        return sum(1 for line in self.lines if line.hits > 0)

    @property
    def uncovered_lines(self) -> int:
        """Number of lines that were not executed."""
        return sum(1 for line in self.lines if line.hits == 0)

    @property
    def line_coverage_percent(self) -> float:
        """Line coverage percentage."""
        if self.total_lines == 0:
            return 100.0
        return (self.covered_lines / self.total_lines) * 100

    @property
    def branch_lines(self) -> list[LineCoverage]:
        """Lines with branch coverage information."""
        return [line for line in self.lines if line.is_branch]

    @property
    def branch_coverage_percent(self) -> float | None:
        """Branch coverage percentage, if available."""
        branch_lines = self.branch_lines
        if not branch_lines:
            return None
        total_branch = sum(line.branch_coverage or 0 for line in branch_lines)
        return total_branch / len(branch_lines) * 100 if branch_lines else None

    @property
    def uncovered_line_numbers(self) -> list[int]:
        """List of line numbers that were not covered."""
        return [line.line_number for line in self.lines if line.hits == 0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "total_lines": self.total_lines,
            "covered_lines": self.covered_lines,
            "uncovered_lines": self.uncovered_lines,
            "line_coverage_percent": round(self.line_coverage_percent, 2),
            "branch_coverage_percent": round(self.branch_coverage_percent, 2) if self.branch_coverage_percent else None,
            "uncovered_line_numbers": self.uncovered_line_numbers[:20],  # Limit for readability
        }


@dataclass
class CoverageSummary:
    """Summary of coverage metrics."""

    total_files: int = 0
    total_lines: int = 0
    covered_lines: int = 0
    total_branches: int = 0
    covered_branches: int = 0

    @property
    def uncovered_lines(self) -> int:
        return self.total_lines - self.covered_lines

    @property
    def line_coverage_percent(self) -> float:
        if self.total_lines == 0:
            return 100.0
        return (self.covered_lines / self.total_lines) * 100

    @property
    def branch_coverage_percent(self) -> float | None:
        if self.total_branches == 0:
            return None
        return (self.covered_branches / self.total_branches) * 100

    @property
    def coverage_rating(self) -> str:
        """Get coverage rating (A-E)."""
        pct = self.line_coverage_percent
        if pct >= 80:
            return "A"
        elif pct >= 70:
            return "B"
        elif pct >= 50:
            return "C"
        elif pct >= 30:
            return "D"
        return "E"

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_files": self.total_files,
            "total_lines": self.total_lines,
            "covered_lines": self.covered_lines,
            "uncovered_lines": self.uncovered_lines,
            "line_coverage_percent": round(self.line_coverage_percent, 2),
            "total_branches": self.total_branches,
            "covered_branches": self.covered_branches,
            "branch_coverage_percent": round(self.branch_coverage_percent, 2) if self.branch_coverage_percent else None,
            "coverage_rating": self.coverage_rating,
        }


@dataclass
class CoverageReport:
    """Complete coverage report."""

    files: list[FileCoverage] = field(default_factory=list)
    format: str = "unknown"
    source_file: str = ""

    @property
    def summary(self) -> CoverageSummary:
        """Calculate coverage summary."""
        summary = CoverageSummary(total_files=len(self.files))

        for file_cov in self.files:
            summary.total_lines += file_cov.total_lines
            summary.covered_lines += file_cov.covered_lines

            for line in file_cov.lines:
                if line.is_branch:
                    summary.total_branches += 1
                    if line.branch_coverage and line.branch_coverage > 0:
                        summary.covered_branches += 1

        return summary

    @property
    def uncovered_files(self) -> list[FileCoverage]:
        """Files with less than 100% coverage."""
        return [f for f in self.files if f.line_coverage_percent < 100]

    @property
    def low_coverage_files(self) -> list[FileCoverage]:
        """Files with less than 50% coverage."""
        return [f for f in self.files if f.line_coverage_percent < 50]

    def get_file_coverage(self, file_path: str) -> FileCoverage | None:
        """Get coverage for a specific file."""
        for file_cov in self.files:
            if file_cov.file_path == file_path or file_cov.file_path.endswith(file_path):
                return file_cov
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "source_file": self.source_file,
            "summary": self.summary.to_dict(),
            "files": [f.to_dict() for f in self.files],
        }


class CoverageParser:
    """Base class for coverage report parsers."""

    format_name: str = "unknown"

    def parse(self, file_path: Path) -> CoverageReport:
        """Parse a coverage report file.

        Args:
            file_path: Path to the coverage report.

        Returns:
            CoverageReport with parsed data.
        """
        raise NotImplementedError

    def can_parse(self, file_path: Path) -> bool:
        """Check if this parser can handle the given file."""
        return False


def parse_coverage(
    file_path: Path | str,
    format: str | None = None,
) -> CoverageReport:
    """Parse a coverage report file.

    Args:
        file_path: Path to the coverage report.
        format: Optional format hint (cobertura, lcov, coverage.py, jacoco, clover).

    Returns:
        CoverageReport with parsed data.
    """
    from codescope.analyzers.coverage.formats import ALL_PARSERS

    file_path = Path(file_path)

    # If format specified, use that parser
    if format:
        for parser in ALL_PARSERS:
            if parser.format_name == format:
                return parser.parse(file_path)

    # Auto-detect format
    for parser in ALL_PARSERS:
        if parser.can_parse(file_path):
            return parser.parse(file_path)

    raise ValueError(f"Could not determine coverage format for {file_path}")
