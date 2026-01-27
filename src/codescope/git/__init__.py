"""Git integration module for CodeScope."""

from codescope.git.blame import (
    GitBlame,
    BlameInfo,
    BlameResult,
    get_blame_for_file,
    get_blame_for_line,
)
from codescope.git.diff import (
    GitDiff,
    DiffResult,
    ChangedFile,
    get_changed_files,
    get_new_code_lines,
)
from codescope.git.repo import (
    GitRepo,
    CommitInfo,
    get_repo_info,
    is_git_repo,
)

__all__ = [
    "GitBlame",
    "BlameInfo",
    "BlameResult",
    "get_blame_for_file",
    "get_blame_for_line",
    "GitDiff",
    "DiffResult",
    "ChangedFile",
    "get_changed_files",
    "get_new_code_lines",
    "GitRepo",
    "CommitInfo",
    "get_repo_info",
    "is_git_repo",
]
