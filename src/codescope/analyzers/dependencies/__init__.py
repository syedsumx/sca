"""Dependency vulnerability scanning module."""

from codescope.analyzers.dependencies.scanner import (
    DependencyScanner,
    Dependency,
    Vulnerability,
    DependencyScanResult,
    scan_dependencies,
)
from codescope.analyzers.dependencies.parsers import (
    DependencyParser,
    RequirementsTxtParser,
    PackageJsonParser,
    GoModParser,
    PomXmlParser,
    GemfileParser,
    ComposerJsonParser,
    CargoTomlParser,
)

__all__ = [
    "DependencyScanner",
    "Dependency",
    "Vulnerability",
    "DependencyScanResult",
    "scan_dependencies",
    "DependencyParser",
    "RequirementsTxtParser",
    "PackageJsonParser",
    "GoModParser",
    "PomXmlParser",
    "GemfileParser",
    "ComposerJsonParser",
    "CargoTomlParser",
]
