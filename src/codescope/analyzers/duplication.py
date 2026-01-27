"""Code duplication detection analyzer."""

import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from codescope.core.utils import detect_language


@dataclass
class CodeBlock:
    """Represents a block of code that may be duplicated."""

    file_path: Path
    start_line: int
    end_line: int
    content: str
    fingerprint: str

    @property
    def line_count(self) -> int:
        return self.end_line - self.start_line + 1

    def to_dict(self) -> dict:
        return {
            "file_path": str(self.file_path),
            "start_line": self.start_line,
            "end_line": self.end_line,
            "line_count": self.line_count,
        }


@dataclass
class Duplication:
    """Represents a set of duplicated code blocks."""

    blocks: list[CodeBlock] = field(default_factory=list)
    duplicated_lines: int = 0
    tokens_count: int = 0

    @property
    def file_count(self) -> int:
        """Number of unique files containing this duplication."""
        return len(set(b.file_path for b in self.blocks))

    def to_dict(self) -> dict:
        return {
            "blocks": [b.to_dict() for b in self.blocks],
            "duplicated_lines": self.duplicated_lines,
            "tokens_count": self.tokens_count,
            "file_count": self.file_count,
        }


@dataclass
class DuplicationResult:
    """Results of duplication analysis."""

    duplications: list[Duplication] = field(default_factory=list)
    total_lines: int = 0
    duplicated_lines: int = 0
    files_analyzed: int = 0

    @property
    def duplication_percentage(self) -> float:
        """Calculate percentage of duplicated lines."""
        if self.total_lines == 0:
            return 0.0
        return (self.duplicated_lines / self.total_lines) * 100

    @property
    def duplication_count(self) -> int:
        """Total number of duplication instances."""
        return len(self.duplications)

    def to_dict(self) -> dict:
        return {
            "total_lines": self.total_lines,
            "duplicated_lines": self.duplicated_lines,
            "duplication_percentage": round(self.duplication_percentage, 2),
            "duplication_count": self.duplication_count,
            "files_analyzed": self.files_analyzed,
            "duplications": [d.to_dict() for d in self.duplications],
        }


class DuplicationAnalyzer:
    """Analyzes code for duplications using token-based fingerprinting."""

    # Minimum number of lines for a block to be considered
    MIN_BLOCK_LINES = 6

    # Minimum number of tokens for a block to be considered
    MIN_BLOCK_TOKENS = 50

    # Language-specific comment patterns
    COMMENT_PATTERNS = {
        "python": [r'#.*$', r'"""[\s\S]*?"""', r"'''[\s\S]*?'''"],
        "javascript": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "typescript": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "java": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "go": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "c": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "cpp": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "csharp": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "ruby": [r'#.*$', r'=begin[\s\S]*?=end'],
        "php": [r'//.*$', r'#.*$', r'/\*[\s\S]*?\*/'],
        "rust": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "swift": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "kotlin": [r'//.*$', r'/\*[\s\S]*?\*/'],
        "scala": [r'//.*$', r'/\*[\s\S]*?\*/'],
    }

    def __init__(
        self,
        min_lines: int = MIN_BLOCK_LINES,
        min_tokens: int = MIN_BLOCK_TOKENS,
    ):
        """Initialize the analyzer.

        Args:
            min_lines: Minimum lines for a duplicate block.
            min_tokens: Minimum tokens for a duplicate block.
        """
        self.min_lines = min_lines
        self.min_tokens = min_tokens

    def analyze(self, files: list[Path]) -> DuplicationResult:
        """Analyze files for code duplication.

        Args:
            files: List of file paths to analyze.

        Returns:
            DuplicationResult with all found duplications.
        """
        result = DuplicationResult(files_analyzed=len(files))

        # Collect all code blocks with their fingerprints
        fingerprint_map: dict[str, list[CodeBlock]] = defaultdict(list)

        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                lines = content.split("\n")
                result.total_lines += len(lines)

                language = detect_language(file_path)
                normalized = self._normalize_code(content, language)

                # Extract blocks and compute fingerprints
                for block in self._extract_blocks(file_path, normalized, lines):
                    fingerprint_map[block.fingerprint].append(block)

            except Exception:
                continue

        # Find duplicates (fingerprints with multiple blocks)
        duplicated_line_set: set[tuple[Path, int]] = set()

        for fingerprint, blocks in fingerprint_map.items():
            if len(blocks) >= 2:
                duplication = Duplication(
                    blocks=blocks,
                    duplicated_lines=blocks[0].line_count,
                    tokens_count=len(self._tokenize(blocks[0].content)),
                )
                result.duplications.append(duplication)

                # Track duplicated lines (excluding first occurrence)
                for block in blocks[1:]:
                    for line_num in range(block.start_line, block.end_line + 1):
                        duplicated_line_set.add((block.file_path, line_num))

        result.duplicated_lines = len(duplicated_line_set)

        # Sort by number of duplicated lines (most significant first)
        result.duplications.sort(key=lambda d: d.duplicated_lines, reverse=True)

        return result

    def _normalize_code(self, content: str, language: str) -> str:
        """Normalize code by removing comments and extra whitespace.

        Args:
            content: Source code content.
            language: Programming language.

        Returns:
            Normalized code string.
        """
        # Remove comments
        patterns = self.COMMENT_PATTERNS.get(language, [r'//.*$', r'/\*[\s\S]*?\*/'])
        for pattern in patterns:
            content = re.sub(pattern, '', content, flags=re.MULTILINE)

        # Normalize whitespace
        lines = []
        for line in content.split("\n"):
            # Remove leading/trailing whitespace but preserve line structure
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
            else:
                lines.append("")

        return "\n".join(lines)

    def _tokenize(self, content: str) -> list[str]:
        """Tokenize code into a list of tokens.

        Args:
            content: Code content to tokenize.

        Returns:
            List of tokens.
        """
        # Simple tokenization: split on whitespace and punctuation
        # Keep identifiers and keywords together
        token_pattern = r'[a-zA-Z_][a-zA-Z0-9_]*|[0-9]+|[^\s\w]'
        return re.findall(token_pattern, content)

    def _extract_blocks(
        self,
        file_path: Path,
        normalized: str,
        original_lines: list[str],
    ) -> Iterator[CodeBlock]:
        """Extract code blocks from normalized content.

        Args:
            file_path: Path to the file.
            normalized: Normalized code content.
            original_lines: Original source lines.

        Yields:
            CodeBlock instances.
        """
        lines = normalized.split("\n")

        # Use sliding window to extract blocks
        for start in range(len(lines) - self.min_lines + 1):
            # Find the end of a logical block
            for end in range(start + self.min_lines - 1, min(start + 50, len(lines))):
                block_content = "\n".join(lines[start:end + 1])
                tokens = self._tokenize(block_content)

                if len(tokens) >= self.min_tokens:
                    # Compute fingerprint
                    fingerprint = self._compute_fingerprint(tokens)

                    # Get original content for display
                    original_content = "\n".join(original_lines[start:end + 1])

                    yield CodeBlock(
                        file_path=file_path,
                        start_line=start + 1,
                        end_line=end + 1,
                        content=original_content,
                        fingerprint=fingerprint,
                    )

                    # Only yield the first valid block starting at this line
                    break

    def _compute_fingerprint(self, tokens: list[str]) -> str:
        """Compute a fingerprint for a token sequence.

        Args:
            tokens: List of tokens.

        Returns:
            SHA-256 fingerprint string.
        """
        # Normalize tokens (replace identifiers with placeholders)
        normalized_tokens = []
        identifier_map: dict[str, str] = {}
        counter = 0

        for token in tokens:
            if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', token):
                # It's an identifier - normalize it
                if token not in identifier_map:
                    identifier_map[token] = f"ID{counter}"
                    counter += 1
                normalized_tokens.append(identifier_map[token])
            else:
                normalized_tokens.append(token)

        content = " ".join(normalized_tokens)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


def analyze_duplication(
    path: Path,
    min_lines: int = 6,
    min_tokens: int = 50,
    extensions: list[str] | None = None,
) -> DuplicationResult:
    """Convenience function to analyze duplication in a directory.

    Args:
        path: Directory or file path to analyze.
        min_lines: Minimum lines for a duplicate block.
        min_tokens: Minimum tokens for a duplicate block.
        extensions: File extensions to include (e.g., ['.py', '.js']).

    Returns:
        DuplicationResult with analysis results.
    """
    if extensions is None:
        extensions = [
            ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go",
            ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php",
            ".rs", ".swift", ".kt", ".scala",
        ]

    # Collect files
    files: list[Path] = []
    if path.is_file():
        files = [path]
    else:
        for ext in extensions:
            files.extend(path.rglob(f"*{ext}"))

    # Filter out common exclusions
    exclusions = ["node_modules", "venv", ".venv", "__pycache__", ".git", "vendor", "dist", "build"]
    files = [
        f for f in files
        if not any(excl in str(f) for excl in exclusions)
    ]

    analyzer = DuplicationAnalyzer(min_lines=min_lines, min_tokens=min_tokens)
    return analyzer.analyze(files)
