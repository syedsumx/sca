"""Enumerations used throughout CodeScope."""

from enum import Enum


class Severity(str, Enum):
    """Issue severity levels."""

    BLOCKER = "BLOCKER"
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    INFO = "INFO"

    @property
    def weight(self) -> int:
        """Return numeric weight for sorting/comparison."""
        weights = {
            Severity.BLOCKER: 5,
            Severity.CRITICAL: 4,
            Severity.MAJOR: 3,
            Severity.MINOR: 2,
            Severity.INFO: 1,
        }
        return weights[self]

    def __lt__(self, other: "Severity") -> bool:
        return self.weight < other.weight

    def __le__(self, other: "Severity") -> bool:
        return self.weight <= other.weight

    def __gt__(self, other: "Severity") -> bool:
        return self.weight > other.weight

    def __ge__(self, other: "Severity") -> bool:
        return self.weight >= other.weight


class IssueType(str, Enum):
    """Types of issues detected."""

    BUG = "BUG"
    VULNERABILITY = "VULNERABILITY"
    CODE_SMELL = "CODE_SMELL"
    SECURITY_HOTSPOT = "SECURITY_HOTSPOT"


class AnalysisStatus(str, Enum):
    """Status of an analysis run."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class QualityGateStatus(str, Enum):
    """Result of quality gate evaluation."""

    PASSED = "PASSED"
    FAILED = "FAILED"
    WARN = "WARN"
    NONE = "NONE"


class Language(str, Enum):
    """Supported programming languages."""

    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    JAVA = "java"
    GO = "go"
    UNKNOWN = "unknown"
