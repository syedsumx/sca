"""Code coverage integration module."""

from codescope.analyzers.coverage.parser import (
    CoverageParser,
    CoverageReport,
    FileCoverage,
    CoverageSummary,
    parse_coverage,
)
from codescope.analyzers.coverage.formats import (
    CoberturaParser,
    LcovParser,
    CoveragePyParser,
    JacocoParser,
    CloverParser,
)

__all__ = [
    "CoverageParser",
    "CoverageReport",
    "FileCoverage",
    "CoverageSummary",
    "parse_coverage",
    "CoberturaParser",
    "LcovParser",
    "CoveragePyParser",
    "JacocoParser",
    "CloverParser",
]
