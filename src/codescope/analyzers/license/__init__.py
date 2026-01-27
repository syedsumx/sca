"""License compliance analyzer."""

from codescope.analyzers.license.analyzer import (
    LicenseAnalyzer,
    LicenseInfo,
    LicenseComplianceResult,
    LicenseRisk,
    analyze_licenses,
)

__all__ = [
    "LicenseAnalyzer",
    "LicenseInfo",
    "LicenseComplianceResult",
    "LicenseRisk",
    "analyze_licenses",
]
