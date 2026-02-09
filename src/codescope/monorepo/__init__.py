"""Monorepo & Workspace Support for CodeScope."""

from codescope.monorepo.detector import (
    MonorepoDetector,
    WorkspaceType,
    WorkspaceInfo,
    SubProject,
)
from codescope.monorepo.scanner import MonorepoScanner, MonorepoResult

__all__ = [
    "MonorepoDetector",
    "WorkspaceType",
    "WorkspaceInfo",
    "SubProject",
    "MonorepoScanner",
    "MonorepoResult",
]
