"""Git blame functionality."""

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class BlameInfo:
    """Blame information for a single line."""

    line_number: int
    commit_hash: str
    author_name: str
    author_email: str
    date: datetime
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "line_number": self.line_number,
            "commit_hash": self.commit_hash,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "date": self.date.isoformat(),
            "content": self.content,
        }


@dataclass
class BlameResult:
    """Result of git blame operation."""

    file_path: str
    lines: list[BlameInfo] = field(default_factory=list)
    error: str | None = None

    def get_line(self, line_number: int) -> BlameInfo | None:
        """Get blame info for a specific line."""
        for line in self.lines:
            if line.line_number == line_number:
                return line
        return None

    def get_author_for_lines(self, line_numbers: list[int]) -> dict[str, list[int]]:
        """Group line numbers by author."""
        author_lines: dict[str, list[int]] = {}
        for line in self.lines:
            if line.line_number in line_numbers:
                if line.author_name not in author_lines:
                    author_lines[line.author_name] = []
                author_lines[line.author_name].append(line.line_number)
        return author_lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "lines": [l.to_dict() for l in self.lines],
            "error": self.error,
        }


class GitBlame:
    """Git blame operations."""

    def __init__(self, repo_path: Path):
        """Initialize with repository path.

        Args:
            repo_path: Path to git repository root.
        """
        self.repo_path = repo_path

    def blame(self, file_path: Path, start_line: int = 1, end_line: int | None = None) -> BlameResult:
        """Get blame information for a file.

        Args:
            file_path: Path to file (relative or absolute).
            start_line: Starting line number (1-indexed).
            end_line: Ending line number (optional).

        Returns:
            BlameResult with line-by-line blame info.
        """
        result = BlameResult(file_path=str(file_path))

        try:
            # Build command — uses list-form (no shell injection risk)
            args = ["git", "blame", "--line-porcelain"]
            if end_line:
                args.extend(["-L", str(start_line) + "," + str(end_line)])
            args.append(str(file_path))

            proc = subprocess.run(  # safe: list-form args, no shell=True
                args,
                cwd=str(self.repo_path),
                capture_output=True,
                text=True,
                timeout=30,
            )

            if proc.returncode != 0:
                result.error = proc.stderr.strip()
                return result

            # Parse porcelain output
            result.lines = self._parse_porcelain(proc.stdout)

        except subprocess.TimeoutExpired:
            result.error = "Git blame timed out"
        except FileNotFoundError:
            result.error = "Git not found"

        return result

    def _parse_porcelain(self, output: str) -> list[BlameInfo]:
        """Parse git blame porcelain output."""
        lines = []
        current: dict[str, Any] = {}

        for line in output.split("\n"):
            if not line:
                continue

            # Commit line (40 char hash + line info)
            if len(line) >= 40 and line[0:40].replace(" ", "").isalnum():
                parts = line.split()
                if len(parts) >= 3:
                    current = {
                        "commit_hash": parts[0][:8],
                        "line_number": int(parts[2]),
                    }

            # Author
            elif line.startswith("author "):
                current["author_name"] = line[7:]
            elif line.startswith("author-mail "):
                email = line[12:].strip("<>")
                current["author_email"] = email
            elif line.startswith("author-time "):
                try:
                    timestamp = int(line[12:])
                    current["date"] = datetime.fromtimestamp(timestamp)
                except ValueError:
                    current["date"] = datetime.now()

            # Content line (starts with tab)
            elif line.startswith("\t"):
                current["content"] = line[1:]
                if all(k in current for k in ["line_number", "commit_hash", "author_name"]):
                    lines.append(BlameInfo(
                        line_number=current.get("line_number", 0),
                        commit_hash=current.get("commit_hash", ""),
                        author_name=current.get("author_name", "Unknown"),
                        author_email=current.get("author_email", ""),
                        date=current.get("date", datetime.now()),
                        content=current.get("content", ""),
                    ))
                current = {}

        return lines


def get_blame_for_file(file_path: Path, repo_path: Path | None = None) -> BlameResult:
    """Get blame information for a file.

    Args:
        file_path: Path to file.
        repo_path: Optional repository root path.

    Returns:
        BlameResult with blame information.
    """
    if repo_path is None:
        repo_path = file_path.parent

    blame = GitBlame(repo_path)
    return blame.blame(file_path)


def get_blame_for_line(file_path: Path, line_number: int, repo_path: Path | None = None) -> BlameInfo | None:
    """Get blame information for a specific line.

    Args:
        file_path: Path to file.
        line_number: Line number (1-indexed).
        repo_path: Optional repository root path.

    Returns:
        BlameInfo or None if not found.
    """
    result = get_blame_for_file(file_path, repo_path)
    return result.get_line(line_number)
