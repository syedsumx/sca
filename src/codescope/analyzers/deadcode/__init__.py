"""Dead code detection analyzer."""

from codescope.analyzers.deadcode.analyzer import (
    DeadCodeAnalyzer,
    DeadCodeResult,
    DeadCodeItem,
    analyze_dead_code,
)

__all__ = [
    "DeadCodeAnalyzer",
    "DeadCodeResult",
    "DeadCodeItem",
    "analyze_dead_code",
]
