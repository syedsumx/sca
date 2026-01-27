"""Data flow analysis for taint tracking."""

from codescope.analyzers.dataflow.analyzer import (
    DataFlowAnalyzer,
    DataFlowResult,
    TaintedPath,
    TaintSource,
    TaintSink,
    analyze_data_flow,
)

__all__ = [
    "DataFlowAnalyzer",
    "DataFlowResult",
    "TaintedPath",
    "TaintSource",
    "TaintSink",
    "analyze_data_flow",
]
