"""Git repository utilities."""

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class CommitInfo:
    """Information about a git commit."""

    hash: str
    short_hash: str
    author_name: str
    author_email: str
    date: datetime
    message: str
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.hash,
            "short_hash": self.short_hash,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "date": self.date.isoformat(),
            "message": self.message,
            "files_changed": self.files_changed,
            "insertions": self.insertions,
            "deletions": self.deletions,
        }


@dataclass
class GitRepo:
    """Git repository information."""

    root_path: Path
    current_branch: str = ""
    remote_url: str = ""
    head_commit: CommitInfo | None = None
    is_dirty: bool = False
    total_commits: int = 0
    contributors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_path": str(self.root_path),
            "current_branch": self.current_branch,
            "remote_url": self.remote_url,
            "head_commit": self.head_commit.to_dict() if self.head_commit else None,
            "is_dirty": self.is_dirty,
            "total_commits": self.total_commits,
            "contributors": self.contributors,
        }


def is_git_repo(path: Path) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path if path.is_dir() else path.parent,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_repo_info(path: Path) -> GitRepo | None:
    """Get information about a git repository.

    Args:
        path: Path within the repository.

    Returns:
        GitRepo info or None if not a git repo.
    """
    if not is_git_repo(path):
        return None

    cwd = path if path.is_dir() else path.parent

    def run_git(*args: str) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""

    # Get root path
    root_path = Path(run_git("rev-parse", "--show-toplevel") or cwd)

    # Get current branch
    current_branch = run_git("rev-parse", "--abbrev-ref", "HEAD")

    # Get remote URL
    remote_url = run_git("config", "--get", "remote.origin.url")

    # Get HEAD commit info
    head_commit = None
    commit_info = run_git("log", "-1", "--format=%H%n%h%n%an%n%ae%n%aI%n%s")
    if commit_info:
        lines = commit_info.split("\n")
        if len(lines) >= 6:
            try:
                head_commit = CommitInfo(
                    hash=lines[0],
                    short_hash=lines[1],
                    author_name=lines[2],
                    author_email=lines[3],
                    date=datetime.fromisoformat(lines[4]),
                    message=lines[5],
                )
            except (ValueError, IndexError):
                pass

    # Check if dirty
    status = run_git("status", "--porcelain")
    is_dirty = bool(status)

    # Get commit count
    commit_count = run_git("rev-list", "--count", "HEAD")
    total_commits = int(commit_count) if commit_count.isdigit() else 0

    # Get contributors (top 20)
    contributors_output = run_git("shortlog", "-sn", "--no-merges", "HEAD")
    contributors = []
    for line in contributors_output.split("\n")[:20]:
        line = line.strip()
        if line:
            # Format: "  123\tAuthor Name"
            parts = line.split("\t", 1)
            if len(parts) == 2:
                contributors.append(parts[1])

    return GitRepo(
        root_path=root_path,
        current_branch=current_branch,
        remote_url=remote_url,
        head_commit=head_commit,
        is_dirty=is_dirty,
        total_commits=total_commits,
        contributors=contributors,
    )


def get_recent_commits(path: Path, count: int = 10) -> list[CommitInfo]:
    """Get recent commits.

    Args:
        path: Path within the repository.
        count: Number of commits to retrieve.

    Returns:
        List of CommitInfo objects.
    """
    if not is_git_repo(path):
        return []

    cwd = path if path.is_dir() else path.parent

    try:
        result = subprocess.run(
            ["git", "log", f"-{count}", "--format=%H%n%h%n%an%n%ae%n%aI%n%s%n---"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )

        if result.returncode != 0:
            return []

        commits = []
        entries = result.stdout.split("---\n")

        for entry in entries:
            lines = entry.strip().split("\n")
            if len(lines) >= 6:
                try:
                    commits.append(CommitInfo(
                        hash=lines[0],
                        short_hash=lines[1],
                        author_name=lines[2],
                        author_email=lines[3],
                        date=datetime.fromisoformat(lines[4]),
                        message=lines[5],
                    ))
                except (ValueError, IndexError):
                    continue

        return commits

    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
