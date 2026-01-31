"""Core module - Foundation for CodeScope."""

from codescope.core.enums import IssueType, Severity, AnalysisStatus
from codescope.core.models import Issue, Location, AnalysisResults, FileAnalysis
from codescope.core.config import Config, load_config
from codescope.core.context import AnalysisContext
from codescope.core.exceptions import (
    CodeScopeError,
    ConfigurationError,
    ParserError,
    AnalysisError,
)

__all__ = [
    "IssueType",
    "Severity",
    "AnalysisStatus",
    "Issue",
    "Location",
    "AnalysisResults",
    "FileAnalysis",
    "Config",
    "load_config",
    "AnalysisContext",
    "CodeScopeError",
    "ConfigurationError",
    "ParserError",
    "AnalysisError",
]
