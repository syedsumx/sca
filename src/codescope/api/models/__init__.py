"""API Pydantic models for request/response validation."""

from codescope.api.models.schemas import (
    # Analysis
    AnalysisRequest,
    AnalysisResponse,
    AnalysisSummary,
    # Issues
    IssueResponse,
    IssueListResponse,
    IssueFilters,
    # Projects
    ProjectResponse,
    ProjectListResponse,
    # Duplications
    DuplicationResponse,
    DuplicateBlockResponse,
    DuplicationGroupResponse,
    # Dependencies
    DependencyResponse,
    VulnerabilityResponse,
    DependencyScanResponse,
    # Coverage
    FileCoverageResponse,
    CoverageResponse,
    # Git
    CommitResponse,
    BlameResponse,
    ChangedFileResponse,
    # Quality Gate
    QualityGateResponse,
    QualityGateConditionResponse,
    # Metrics
    MetricsResponse,
    FileMetricsResponse,
    # Rules
    RuleResponse,
    RuleListResponse,
)

__all__ = [
    "AnalysisRequest",
    "AnalysisResponse",
    "AnalysisSummary",
    "IssueResponse",
    "IssueListResponse",
    "IssueFilters",
    "ProjectResponse",
    "ProjectListResponse",
    "DuplicationResponse",
    "DuplicateBlockResponse",
    "DuplicationGroupResponse",
    "DependencyResponse",
    "VulnerabilityResponse",
    "DependencyScanResponse",
    "FileCoverageResponse",
    "CoverageResponse",
    "CommitResponse",
    "BlameResponse",
    "ChangedFileResponse",
    "QualityGateResponse",
    "QualityGateConditionResponse",
    "MetricsResponse",
    "FileMetricsResponse",
    "RuleResponse",
    "RuleListResponse",
]
