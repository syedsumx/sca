"""Base classes for language parsers."""

from abc import ABC, abstractmethod
from pathlib import Path

from codescope.parsers.models import ParsedFile


class LanguageParser(ABC):
    """Abstract base class for language-specific parsers."""

    @property
    @abstractmethod
    def language_id(self) -> str:
        """Return the language identifier (e.g., 'python', 'javascript')."""
        pass

    @property
    @abstractmethod
    def file_extensions(self) -> list[str]:
        """Return list of file extensions this parser handles."""
        pass

    @abstractmethod
    def parse(self, source: str, file_path: Path | None = None) -> ParsedFile:
        """Parse source code and return a ParsedFile.

        Args:
            source: Source code string to parse.
            file_path: Optional path for error messages.

        Returns:
            ParsedFile containing AST and symbol information.
        """
        pass

    def parse_file(self, file_path: Path) -> ParsedFile:
        """Parse a file from disk.

        Args:
            file_path: Path to the file to parse.

        Returns:
            ParsedFile containing AST and symbol information.
        """
        with open(file_path, encoding="utf-8", errors="replace") as f:
            source = f.read()
        return self.parse(source, file_path)

    def can_parse(self, file_path: Path) -> bool:
        """Check if this parser can handle the given file.

        Args:
            file_path: Path to check.

        Returns:
            True if this parser handles this file type.
        """
        return file_path.suffix.lower() in self.file_extensions
