"""Pydantic schemas for API request/response models."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

from codescope.core.enums import Severity, IssueType, QualityGateStatus


# Base Models
class LocationSchema(BaseModel):
    """Location in source code."""
    file_path: str
    start_line: int
    end_line: int
    start_column: Optional[int] = None
    end_column: Optional[int] = None


# Analysis Models
class AnalysisRequest(BaseModel):
    """Request to trigger a new analysis."""
    path: str = Field(..., description="Path to the project to analyze")
    config_file: Optional[str] = Field(None, description="Path to config file")
    rules: Optional[list[str]] = Field(None, description="List of rule IDs to enable")
    exclude_patterns: Optional[list[str]] = Field(None, description="Glob patterns to exclude")


class AnalysisSummary(BaseModel):
    """Summary of an analysis."""
    analysis_id: str
    project_name: str
    timestamp: datetime
    duration_seconds: float
    status: str
    issues_count: int
    quality_gate_status: QualityGateStatus


class AnalysisResponse(BaseModel):
    """Full analysis result response."""
    analysis_id: str
    project_name: str
    timestamp: datetime
    duration_seconds: float
    status: str
    issues_count: int
    quality_gate: "QualityGateResponse"
    metrics: "MetricsResponse"


# Issue Models
class IssueResponse(BaseModel):
    """Single issue response."""
    id: str
    rule_id: str
    rule_name: str
    severity: Severity
    issue_type: IssueType
    message: str
    location: LocationSchema
    effort_minutes: Optional[int] = None
    tags: list[str] = []
    snippet: Optional[str] = None
    suggestion: Optional[str] = None


class IssueFilters(BaseModel):
    """Filters for issue queries."""
    severity: Optional[list[Severity]] = None
    issue_type: Optional[list[IssueType]] = None
    file_path: Optional[str] = None
    rule_id: Optional[str] = None
    tags: Optional[list[str]] = None


class IssueListResponse(BaseModel):
    """Paginated list of issues."""
    issues: list[IssueResponse]
    total: int
    page: int
    page_size: int


# Project Models
class ProjectResponse(BaseModel):
    """Project summary response."""
    project_name: str
    last_analysis: Optional[datetime] = None
    quality_gate_status: Optional[QualityGateStatus] = None
    bugs: int = 0
    vulnerabilities: int = 0
    code_smells: int = 0
    coverage: Optional[float] = None
    duplications: Optional[float] = None
    lines_of_code: int = 0
    reliability_rating: str = "A"
    security_rating: str = "A"
    maintainability_rating: str = "A"


class ProjectListResponse(BaseModel):
    """List of projects."""
    projects: list[ProjectResponse]
    total: int


# Duplication Models
class DuplicateBlockResponse(BaseModel):
    """A duplicated code block."""
    file_path: str
    start_line: int
    end_line: int
    lines: int


class DuplicationGroupResponse(BaseModel):
    """Group of duplicate blocks."""
    id: str
    fingerprint: str
    token_count: int
    line_count: int
    blocks: list[DuplicateBlockResponse]


class DuplicationResponse(BaseModel):
    """Duplication analysis result."""
    total_duplicated_lines: int
    total_duplicated_blocks: int
    duplication_percentage: float
    groups: list[DuplicationGroupResponse]


# Dependency Models
class DependencyResponse(BaseModel):
    """A project dependency."""
    name: str
    version: str
    ecosystem: str
    source_file: str
    is_dev: bool = False


class VulnerabilityResponse(BaseModel):
    """A security vulnerability."""
    id: str
    severity: Severity
    summary: str
    details: Optional[str] = None
    affected_package: str
    affected_versions: str
    fixed_version: Optional[str] = None
    references: list[str] = []
    cvss_score: Optional[float] = None


class DependencyScanResponse(BaseModel):
    """Dependency scan result."""
    total_dependencies: int
    direct_dependencies: int
    dev_dependencies: int
    vulnerable_count: int
    dependencies: list[DependencyResponse]
    vulnerabilities: list[VulnerabilityResponse]


# Coverage Models
class FileCoverageResponse(BaseModel):
    """Coverage for a single file."""
    file_path: str
    line_coverage: float
    branch_coverage: Optional[float] = None
    covered_lines: int
    total_lines: int
    uncovered_lines: list[int] = []


class CoverageResponse(BaseModel):
    """Coverage analysis result."""
    line_coverage: float
    branch_coverage: Optional[float] = None
    total_lines: int
    covered_lines: int
    uncovered_lines: int
    files: list[FileCoverageResponse]


# Git Models
class CommitResponse(BaseModel):
    """Git commit information."""
    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: datetime
    message: str


class BlameResponse(BaseModel):
    """Git blame information for a line."""
    line_number: int
    commit_hash: str
    author_name: str
    author_email: str
    date: datetime
    content: str


class ChangedFileResponse(BaseModel):
    """A changed file in a diff."""
    file_path: str
    status: str
    insertions: int
    deletions: int
    added_lines: list[int] = []


# Quality Gate Models
class QualityGateConditionResponse(BaseModel):
    """A quality gate condition."""
    metric: str
    operator: str
    threshold: float
    actual_value: float
    status: QualityGateStatus


class QualityGateResponse(BaseModel):
    """Quality gate result."""
    status: QualityGateStatus
    conditions: list[QualityGateConditionResponse]


# Metrics Models
class FileMetricsResponse(BaseModel):
    """Metrics for a single file."""
    file_path: str
    lines_of_code: int
    comment_lines: int
    blank_lines: int
    complexity: int
    functions: int
    classes: int
    issues_count: int


class MetricsResponse(BaseModel):
    """Project metrics."""
    total_files: int
    total_lines: int
    total_code_lines: int
    total_comment_lines: int
    total_blank_lines: int
    average_complexity: float
    total_functions: int
    total_classes: int
    languages: dict[str, int]


# Rules Models
class RuleResponse(BaseModel):
    """Analysis rule."""
    id: str
    name: str
    description: str
    severity: Severity
    issue_type: IssueType
    language: str
    tags: list[str] = []
    enabled: bool = True


class RuleListResponse(BaseModel):
    """List of rules."""
    rules: list[RuleResponse]
    total: int


# Update forward references
AnalysisResponse.model_rebuild()
