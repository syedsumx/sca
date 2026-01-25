"""Utility functions for CodeScope."""

import hashlib
import re
from pathlib import Path
from typing import Any


def generate_fingerprint(*args: Any) -> str:
    """Generate a unique fingerprint for issue deduplication.

    Args:
        *args: Values to include in fingerprint (rule_id, file, line, etc.)

    Returns:
        SHA-256 hash string (first 16 characters).
    """
    content = "|".join(str(arg) for arg in args)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def get_snippet(file_path: Path, line: int, context: int = 2) -> str:
    """Extract code snippet around a line.

    Args:
        file_path: Path to source file.
        line: Line number (1-indexed).
        context: Number of lines before/after to include.

    Returns:
        Code snippet string.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        start = max(0, line - context - 1)
        end = min(len(lines), line + context)

        snippet_lines = []
        for i in range(start, end):
            prefix = ">>> " if i == line - 1 else "    "
            snippet_lines.append(f"{prefix}{i + 1}: {lines[i].rstrip()}")

        return "\n".join(snippet_lines)
    except Exception:
        return ""


def count_lines(file_path: Path) -> tuple[int, int, int, int]:
    """Count lines in a file.

    Args:
        file_path: Path to source file.

    Returns:
        Tuple of (total, code, comment, blank) line counts.
    """
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return 0, 0, 0, 0

    lines = content.split("\n")
    total = len(lines)
    blank = 0
    comment = 0
    code = 0

    in_multiline_comment = False
    multiline_start = re.compile(r'^\s*("""|\'\'\')|(\/\*)')
    multiline_end = re.compile(r'("""|\'\'\'|\*\/)\s*$')

    for line in lines:
        stripped = line.strip()

        if not stripped:
            blank += 1
            continue

        # Check for multiline comment boundaries
        if in_multiline_comment:
            comment += 1
            if multiline_end.search(stripped):
                in_multiline_comment = False
            continue

        if multiline_start.search(stripped):
            comment += 1
            # Check if it ends on same line
            if not (stripped.count('"""') >= 2 or stripped.count("'''") >= 2):
                if not stripped.endswith("*/"):
                    in_multiline_comment = True
            continue

        # Single line comments
        if stripped.startswith(("#", "//", "*", "<!--")):
            comment += 1
            continue

        code += 1

    return total, code, comment, blank


def normalize_path(path: Path, root: Path) -> str:
    """Normalize path relative to project root.

    Args:
        path: Absolute or relative path.
        root: Project root path.

    Returns:
        Normalized relative path string.
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def format_duration(minutes: int) -> str:
    """Format duration in minutes to human-readable string.

    Args:
        minutes: Duration in minutes.

    Returns:
        Formatted string like "2h 30min" or "5d".
    """
    if minutes < 60:
        return f"{minutes}min"
    elif minutes < 60 * 24:
        hours = minutes // 60
        mins = minutes % 60
        if mins:
            return f"{hours}h {mins}min"
        return f"{hours}h"
    else:
        days = minutes // (60 * 24)
        remaining = minutes % (60 * 24)
        hours = remaining // 60
        if hours:
            return f"{days}d {hours}h"
        return f"{days}d"


def detect_language(file_path: Path) -> str:
    """Detect programming language from file extension.

    Args:
        file_path: Path to file.

    Returns:
        Language identifier string.
    """
    extension_map = {
        ".py": "python",
        ".pyw": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".java": "java",
        ".go": "go",
        ".c": "c",
        ".h": "c",
        ".cpp": "cpp",
        ".hpp": "cpp",
        ".cc": "cpp",
        ".cxx": "cpp",
        ".cs": "csharp",
        ".rb": "ruby",
        ".php": "php",
        ".rs": "rust",
        ".swift": "swift",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".scala": "scala",
    }
    return extension_map.get(file_path.suffix.lower(), "unknown")


def truncate_message(message: str, max_length: int = 200) -> str:
    """Truncate message to maximum length.

    Args:
        message: Message to truncate.
        max_length: Maximum length.

    Returns:
        Truncated message with ellipsis if needed.
    """
    if len(message) <= max_length:
        return message
    return message[: max_length - 3] + "..."
