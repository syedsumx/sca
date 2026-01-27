"""Analysis engine module for CodeScope."""

from codescope.analyzers.orchestrator import AnalysisOrchestrator, analyze_path
from codescope.analyzers.duplication import (
    DuplicationAnalyzer,
    DuplicationResult,
    Duplication,
    CodeBlock,
    analyze_duplication,
)

__all__ = [
    "AnalysisOrchestrator",
    "analyze_path",
    "DuplicationAnalyzer",
    "DuplicationResult",
    "Duplication",
    "CodeBlock",
    "analyze_duplication",
]
