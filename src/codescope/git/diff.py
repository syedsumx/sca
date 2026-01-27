"""Git diff and change detection."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ChangedFile:
    """Information about a changed file."""

    file_path: str
    status: str  # A (added), M (modified), D (deleted), R (renamed)
    insertions: int = 0
    deletions: int = 0
    added_lines: list[int] = field(default_factory=list)  # Line numbers of added lines
    deleted_lines: list[int] = field(default_factory=list)  # Line numbers of deleted lines

    @property
    def is_new(self) -> bool:
        return self.status == "A"

    @property
    def is_modified(self) -> bool:
        return self.status == "M"

    @property
    def is_deleted(self) -> bool:
        return self.status == "D"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "status": self.status,
            "insertions": self.insertions,
            "deletions": self.deletions,
            "added_lines": self.added_lines,
            "is_new": self.is_new,
            "is_modified": self.is_modified,
        }


@dataclass
class DiffResult:
    """Result of diff operation."""

    base_ref: str
    head_ref: str
    files: list[ChangedFile] = field(default_factory=list)
    error: str | None = None

    @property
    def total_files_changed(self) -> int:
        return len(self.files)

    @property
    def total_insertions(self) -> int:
        return sum(f.insertions for f in self.files)

    @property
    def total_deletions(self) -> int:
        return sum(f.deletions for f in self.files)

    @property
    def new_files(self) -> list[ChangedFile]:
        return [f for f in self.files if f.is_new]

    @property
    def modified_files(self) -> list[ChangedFile]:
        return [f for f in self.files if f.is_modified]

    def is_line_new(self, file_path: str, line_number: int) -> bool:
        """Check if a specific line is newly added."""
        for f in self.files:
            if f.file_path == file_path or file_path.endswith(f.file_path):
                return line_number in f.added_lines or f.is_new
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "total_files_changed": self.total_files_changed,
            "total_insertions": self.total_insertions,
            "total_deletions": self.total_deletions,
            "files": [f.to_dict() for f in self.files],
            "error": self.error,
        }


class GitDiff:
    """Git diff operations."""

    def __init__(self, repo_path: Path):
        """Initialize with repository path.

        Args:
            repo_path: Path to git repository root.
        """
        self.repo_path = repo_path

    def diff(self, base_ref: str = "HEAD~1", head_ref: str = "HEAD") -> DiffResult:
        """Get diff between two refs.

        Args:
            base_ref: Base reference (commit, branch, tag).
            head_ref: Head reference.

        Returns:
            DiffResult with changed files.
        """
        result = DiffResult(base_ref=base_ref, head_ref=head_ref)

        try:
            # Get list of changed files with stats
            proc = subprocess.run(
                ["git", "diff", "--numstat", "--name-status", base_ref, head_ref],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )

            if proc.returncode != 0:
                result.error = proc.stderr.strip()
                return result

            # Parse numstat output
            files_map: dict[str, ChangedFile] = {}

            for line in proc.stdout.strip().split("\n"):
                if not line:
                    continue

                parts = line.split("\t")

                # Name-status format: M\tfilename
                if len(parts) == 2 and len(parts[0]) == 1:
                    status, file_path = parts
                    if file_path not in files_map:
                        files_map[file_path] = ChangedFile(
                            file_path=file_path,
                            status=status,
                        )
                    else:
                        files_map[file_path].status = status

                # Numstat format: insertions\tdeletions\tfilename
                elif len(parts) == 3:
                    try:
                        insertions = int(parts[0]) if parts[0] != "-" else 0
                        deletions = int(parts[1]) if parts[1] != "-" else 0
                        file_path = parts[2]

                        if file_path not in files_map:
                            files_map[file_path] = ChangedFile(
                                file_path=file_path,
                                status="M",
                            )

                        files_map[file_path].insertions = insertions
                        files_map[file_path].deletions = deletions
                    except ValueError:
                        continue

            # Get line-level diff for each file
            for file_path, changed_file in files_map.items():
                if changed_file.is_deleted:
                    continue

                added_lines = self._get_added_lines(base_ref, head_ref, file_path)
                changed_file.added_lines = added_lines

            result.files = list(files_map.values())

        except subprocess.TimeoutExpired:
            result.error = "Git diff timed out"
        except FileNotFoundError:
            result.error = "Git not found"

        return result

    def _get_added_lines(self, base_ref: str, head_ref: str, file_path: str) -> list[int]:
        """Get line numbers of added lines in a file."""
        added_lines = []

        try:
            proc = subprocess.run(
                ["git", "diff", "-U0", base_ref, head_ref, "--", file_path],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )

            if proc.returncode != 0:
                return []

            current_line = 0
            for line in proc.stdout.split("\n"):
                # Hunk header: @@ -start,count +start,count @@
                if line.startswith("@@"):
                    # Parse +start,count
                    try:
                        plus_part = line.split("+")[1].split()[0]
                        if "," in plus_part:
                            current_line = int(plus_part.split(",")[0])
                        else:
                            current_line = int(plus_part)
                    except (IndexError, ValueError):
                        continue

                # Added line
                elif line.startswith("+") and not line.startswith("+++"):
                    added_lines.append(current_line)
                    current_line += 1

                # Context line (only in unified diff with context)
                elif not line.startswith("-") and not line.startswith("\\"):
                    current_line += 1

        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        return added_lines

    def diff_with_branch(self, branch: str = "main") -> DiffResult:
        """Get diff between current HEAD and a branch.

        Args:
            branch: Branch name to compare against.

        Returns:
            DiffResult with changed files.
        """
        # Find merge base
        try:
            proc = subprocess.run(
                ["git", "merge-base", branch, "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )

            if proc.returncode == 0:
                merge_base = proc.stdout.strip()
                return self.diff(merge_base, "HEAD")
            else:
                return self.diff(branch, "HEAD")

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return self.diff(branch, "HEAD")


def get_changed_files(
    repo_path: Path,
    base_ref: str = "HEAD~1",
    head_ref: str = "HEAD",
) -> DiffResult:
    """Get changed files between two refs.

    Args:
        repo_path: Path to repository.
        base_ref: Base reference.
        head_ref: Head reference.

    Returns:
        DiffResult with changes.
    """
    diff = GitDiff(repo_path)
    return diff.diff(base_ref, head_ref)


def get_new_code_lines(
    repo_path: Path,
    file_path: str,
    base_ref: str = "main",
) -> list[int]:
    """Get line numbers that are new compared to a base branch.

    Args:
        repo_path: Path to repository.
        file_path: File to check.
        base_ref: Base reference to compare against.

    Returns:
        List of new line numbers.
    """
    diff = GitDiff(repo_path)
    result = diff.diff_with_branch(base_ref)

    for f in result.files:
        if f.file_path == file_path or file_path.endswith(f.file_path):
            return f.added_lines if not f.is_new else []

    return []
