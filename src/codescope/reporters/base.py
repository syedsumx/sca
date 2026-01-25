"""Base class for reporters."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TextIO

from codescope.core.models import AnalysisResults


class Reporter(ABC):
    """Abstract base class for report generators."""

    @property
    @abstractmethod
    def format_name(self) -> str:
        """Return the format name (e.g., 'json', 'sarif')."""
        pass

    @abstractmethod
    def generate(self, results: AnalysisResults) -> str:
        """Generate report as string.

        Args:
            results: Analysis results to report.

        Returns:
            Report content as string.
        """
        pass

    def write(self, results: AnalysisResults, output: Path | TextIO) -> None:
        """Write report to file or stream.

        Args:
            results: Analysis results to report.
            output: File path or file-like object.
        """
        content = self.generate(results)

        if isinstance(output, Path):
            output.write_text(content)
        else:
            output.write(content)
