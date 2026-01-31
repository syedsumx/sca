"""Parser registry for managing language parsers."""

from pathlib import Path
from typing import Type

from codescope.parsers.base import LanguageParser


class ParserRegistry:
    """Registry for language parsers."""

    _parsers: dict[str, LanguageParser] = {}
    _extension_map: dict[str, str] = {}

    @classmethod
    def register(cls, parser_class: Type[LanguageParser]) -> Type[LanguageParser]:
        """Register a parser class.

        Can be used as a decorator:
            @ParserRegistry.register
            class MyParser(LanguageParser):
                ...
        """
        parser = parser_class()
        cls._parsers[parser.language_id] = parser

        for ext in parser.file_extensions:
            cls._extension_map[ext] = parser.language_id

        return parser_class

    @classmethod
    def get_parser(cls, language: str) -> LanguageParser | None:
        """Get parser by language identifier."""
        return cls._parsers.get(language)

    @classmethod
    def get_parser_for_file(cls, file_path: Path) -> LanguageParser | None:
        """Get parser for a file based on its extension."""
        ext = file_path.suffix.lower()
        language = cls._extension_map.get(ext)
        if language:
            return cls._parsers.get(language)
        return None

    @classmethod
    def get_all_parsers(cls) -> dict[str, LanguageParser]:
        """Get all registered parsers."""
        return cls._parsers.copy()

    @classmethod
    def get_supported_extensions(cls) -> list[str]:
        """Get all supported file extensions."""
        return list(cls._extension_map.keys())

    @classmethod
    def get_supported_languages(cls) -> list[str]:
        """Get all supported language identifiers."""
        return list(cls._parsers.keys())


def get_parser(language_or_path: str | Path) -> LanguageParser | None:
    """Convenience function to get a parser.

    Args:
        language_or_path: Either a language identifier or a file path.

    Returns:
        Appropriate parser or None if not found.
    """
    if isinstance(language_or_path, Path):
        return ParserRegistry.get_parser_for_file(language_or_path)
    return ParserRegistry.get_parser(language_or_path)


# Import parsers to trigger registration
def _register_parsers() -> None:
    """Register all built-in parsers."""
    # Import here to avoid circular imports
    from codescope.parsers import python  # noqa: F401

    # Future: javascript, typescript, java, go, etc.


_register_parsers()
