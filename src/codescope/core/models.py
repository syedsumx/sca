"""Core data models for CodeScope."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from codescope.core.enums import IssueType, Severity, AnalysisStatus, QualityGateStatus


@dataclass
class Location:
    """Precise location in source code."""

    file_path: Path
    start_line: int
    end_line: int
    start_column: int = 0
    end_column: int = 0
    snippet: str = ""

    def __str__(self) -> str:
        return f"{self.file_path}:{self.start_line}"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "file_path": str(self.file_path),
            "start_line": self.start_line,
            "end_line": self.end_line,
            "start_column": self.start_column,
            "end_column": self.end_column,
            "snippet": self.snippet,
        }


@dataclass
class Issue:
    """Represents a single code issue (bug, vulnerability, smell)."""

    rule_id: str
    rule_name: str
    message: str
    location: Location
    severity: Severity
    issue_type: IssueType
    id: UUID = field(default_factory=uuid4)
    effort_minutes: int = 5
    cwe_ids: list[int] = field(default_factory=list)
    owasp_categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def effort(self) -> timedelta:
        """Return effort as timedelta."""
        return timedelta(minutes=self.effort_minutes)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": str(self.id),
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "message": self.message,
            "location": self.location.to_dict(),
            "severity": self.severity.value,
            "type": self.issue_type.value,
            "effort_minutes": self.effort_minutes,
            "cwe_ids": self.cwe_ids,
            "owasp_categories": self.owasp_categories,
            "tags": self.tags,
        }


@dataclass
class FileMetrics:
    """Metrics for a single file."""

    file_path: Path
    language: str
    lines_of_code: int = 0
    logical_lines: int = 0
    comment_lines: int = 0
    blank_lines: int = 0
    cyclomatic_complexity: int = 0
    cognitive_complexity: int = 0
    maintainability_index: float = 100.0
    functions_count: int = 0
    classes_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "file_path": str(self.file_path),
            "language": self.language,
            "lines_of_code": self.lines_of_code,
            "logical_lines": self.logical_lines,
            "comment_lines": self.comment_lines,
            "blank_lines": self.blank_lines,
            "cyclomatic_complexity": self.cyclomatic_complexity,
            "cognitive_complexity": self.cognitive_complexity,
            "maintainability_index": self.maintainability_index,
            "functions_count": self.functions_count,
            "classes_count": self.classes_count,
        }


@dataclass
class FileAnalysis:
    """Analysis results for a single file."""

    file_path: Path
    language: str
    issues: list[Issue] = field(default_factory=list)
    metrics: FileMetrics | None = None

    @property
    def bugs_count(self) -> int:
        return sum(1 for i in self.issues if i.issue_type == IssueType.BUG)

    @property
    def vulnerabilities_count(self) -> int:
        return sum(1 for i in self.issues if i.issue_type == IssueType.VULNERABILITY)

    @property
    def code_smells_count(self) -> int:
        return sum(1 for i in self.issues if i.issue_type == IssueType.CODE_SMELL)

    @property
    def hotspots_count(self) -> int:
        return sum(1 for i in self.issues if i.issue_type == IssueType.SECURITY_HOTSPOT)


@dataclass
class ProjectMetrics:
    """Aggregated metrics for entire project."""

    total_files: int = 0
    total_lines_of_code: int = 0
    total_issues: int = 0
    bugs_count: int = 0
    vulnerabilities_count: int = 0
    code_smells_count: int = 0
    hotspots_count: int = 0
    average_complexity: float = 0.0
    technical_debt_minutes: int = 0
    duplications_percent: float = 0.0
    coverage_percent: float | None = None

    # Ratings (A-E)
    reliability_rating: str = "A"
    security_rating: str = "A"
    maintainability_rating: str = "A"

    @property
    def technical_debt_ratio(self) -> float:
        """Calculate technical debt ratio."""
        if self.total_lines_of_code == 0:
            return 0.0
        # Assume 30 minutes per LOC for complete rewrite
        development_cost = self.total_lines_of_code * 30
        return (self.technical_debt_minutes / development_cost) * 100

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "total_files": self.total_files,
            "total_lines_of_code": self.total_lines_of_code,
            "total_issues": self.total_issues,
            "bugs_count": self.bugs_count,
            "vulnerabilities_count": self.vulnerabilities_count,
            "code_smells_count": self.code_smells_count,
            "hotspots_count": self.hotspots_count,
            "average_complexity": self.average_complexity,
            "technical_debt_minutes": self.technical_debt_minutes,
            "technical_debt_ratio": self.technical_debt_ratio,
            "duplications_percent": self.duplications_percent,
            "coverage_percent": self.coverage_percent,
            "reliability_rating": self.reliability_rating,
            "security_rating": self.security_rating,
            "maintainability_rating": self.maintainability_rating,
        }


@dataclass
class QualityGateResult:
    """Result of quality gate evaluation."""

    status: QualityGateStatus
    conditions: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "status": self.status.value,
            "conditions": self.conditions,
            "message": self.message,
        }


@dataclass
class AnalysisResults:
    """Complete analysis results for a project."""

    project_path: Path
    started_at: datetime = field(default_factory=datetime.now)
    completed_at: datetime | None = None
    status: AnalysisStatus = AnalysisStatus.PENDING
    files: list[FileAnalysis] = field(default_factory=list)
    metrics: ProjectMetrics = field(default_factory=ProjectMetrics)
    quality_gate: QualityGateResult | None = None
    error_message: str | None = None

    @property
    def all_issues(self) -> list[Issue]:
        """Get all issues from all files."""
        issues = []
        for file_analysis in self.files:
            issues.extend(file_analysis.issues)
        return issues

    @property
    def issues_by_severity(self) -> dict[Severity, list[Issue]]:
        """Group issues by severity."""
        result: dict[Severity, list[Issue]] = {s: [] for s in Severity}
        for issue in self.all_issues:
            result[issue.severity].append(issue)
        return result

    @property
    def issues_by_type(self) -> dict[IssueType, list[Issue]]:
        """Group issues by type."""
        result: dict[IssueType, list[Issue]] = {t: [] for t in IssueType}
        for issue in self.all_issues:
            result[issue.issue_type].append(issue)
        return result

    def calculate_metrics(self) -> None:
        """Calculate aggregate metrics from file analyses."""
        self.metrics.total_files = len(self.files)
        self.metrics.total_issues = len(self.all_issues)

        total_loc = 0
        total_complexity = 0
        complexity_count = 0

        for file_analysis in self.files:
            self.metrics.bugs_count += file_analysis.bugs_count
            self.metrics.vulnerabilities_count += file_analysis.vulnerabilities_count
            self.metrics.code_smells_count += file_analysis.code_smells_count
            self.metrics.hotspots_count += file_analysis.hotspots_count

            if file_analysis.metrics:
                total_loc += file_analysis.metrics.lines_of_code
                if file_analysis.metrics.cyclomatic_complexity > 0:
                    total_complexity += file_analysis.metrics.cyclomatic_complexity
                    complexity_count += 1

        self.metrics.total_lines_of_code = total_loc
        if complexity_count > 0:
            self.metrics.average_complexity = total_complexity / complexity_count

        # Calculate technical debt (sum of effort for all issues)
        self.metrics.technical_debt_minutes = sum(
            issue.effort_minutes for issue in self.all_issues
        )

        # Calculate ratings
        self.metrics.reliability_rating = self._calculate_reliability_rating()
        self.metrics.security_rating = self._calculate_security_rating()
        self.metrics.maintainability_rating = self._calculate_maintainability_rating()

    def _calculate_reliability_rating(self) -> str:
        """Calculate reliability rating based on bugs."""
        bugs = self.metrics.bugs_count
        if bugs == 0:
            return "A"
        elif bugs <= 1:
            return "B"
        elif bugs <= 5:
            return "C"
        elif bugs <= 10:
            return "D"
        return "E"

    def _calculate_security_rating(self) -> str:
        """Calculate security rating based on vulnerabilities."""
        vulns = self.metrics.vulnerabilities_count
        if vulns == 0:
            return "A"
        elif vulns <= 1:
            return "B"
        elif vulns <= 5:
            return "C"
        elif vulns <= 10:
            return "D"
        return "E"

    def _calculate_maintainability_rating(self) -> str:
        """Calculate maintainability rating based on technical debt ratio."""
        ratio = self.metrics.technical_debt_ratio
        if ratio <= 5:
            return "A"
        elif ratio <= 10:
            return "B"
        elif ratio <= 20:
            return "C"
        elif ratio <= 50:
            return "D"
        return "E"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "project_path": str(self.project_path),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "status": self.status.value,
            "metrics": self.metrics.to_dict(),
            "quality_gate": self.quality_gate.to_dict() if self.quality_gate else None,
            "issues": [issue.to_dict() for issue in self.all_issues],
            "files_analyzed": len(self.files),
            "error_message": self.error_message,
        }
