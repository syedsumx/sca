"""Git API routes."""

from pathlib import Path
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, HTTPException

from codescope.api.models import (
    CommitResponse,
    BlameResponse,
    ChangedFileResponse,
)
from codescope.git.repo import get_repo_info
from codescope.git.blame import get_blame_for_file
from codescope.git.diff import get_changed_files, get_new_code_lines
from codescope.api.routes.analysis import _analyses

router = APIRouter()


@router.get("/projects/{project_id}/git/info")
async def get_git_info(project_id: str):
    """Get git repository information."""
    # Look up project path
    if project_id in _analyses:
        path = Path(_analyses[project_id].get("path", "."))
    else:
        path = Path(project_id)

    if not path.exists():
        raise HTTPException(status_code=404, detail="Project path not found")

    info = get_repo_info(path)

    if not info:
        raise HTTPException(status_code=400, detail="Not a git repository")

    return {
        "current_branch": info.current_branch,
        "current_commit": info.current_commit,
        "remote_url": info.remote_url,
        "is_dirty": info.is_dirty,
        "untracked_files": info.untracked_files,
    }


@router.get("/projects/{project_id}/git/commits", response_model=list[CommitResponse])
async def get_commits(
    project_id: str,
    limit: int = 10,
    branch: Optional[str] = None,
):
    """Get recent commits."""
    if project_id in _analyses:
        path = Path(_analyses[project_id].get("path", "."))
    else:
        path = Path(project_id)

    if not path.exists():
        raise HTTPException(status_code=404, detail="Project path not found")

    info = get_repo_info(path)
    if not info:
        raise HTTPException(status_code=400, detail="Not a git repository")

    return [
        CommitResponse(
            hash=c.hash,
            short_hash=c.short_hash,
            author_name=c.author_name,
            author_email=c.author_email,
            date=c.date,
            message=c.message,
        )
        for c in info.recent_commits[:limit]
    ]


@router.get("/projects/{project_id}/git/blame", response_model=list[BlameResponse])
async def get_blame(
    project_id: str,
    path: str,
):
    """Get git blame for a file."""
    if project_id in _analyses:
        repo_path = Path(_analyses[project_id].get("path", "."))
    else:
        repo_path = Path(project_id)

    file_path = repo_path / path

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")

    blame_info = get_blame_for_file(repo_path, path)

    return [
        BlameResponse(
            line_number=b.line_number,
            commit_hash=b.commit_hash,
            author_name=b.author_name,
            author_email=b.author_email,
            date=b.date,
            content=b.content,
        )
        for b in blame_info
    ]


@router.get("/projects/{project_id}/git/diff", response_model=list[ChangedFileResponse])
async def get_diff(
    project_id: str,
    base: str = "HEAD~1",
    head: str = "HEAD",
):
    """Get changed files between two commits."""
    if project_id in _analyses:
        path = Path(_analyses[project_id].get("path", "."))
    else:
        path = Path(project_id)

    if not path.exists():
        raise HTTPException(status_code=404, detail="Project path not found")

    changed = get_changed_files(path, base, head)

    return [
        ChangedFileResponse(
            file_path=f.file_path,
            status=f.status,
            insertions=f.insertions,
            deletions=f.deletions,
            added_lines=f.added_lines,
        )
        for f in changed
    ]


@router.get("/projects/{project_id}/git/new-lines")
async def get_new_lines(
    project_id: str,
    base: str = "HEAD~1",
    head: str = "HEAD",
):
    """Get line numbers of new code."""
    if project_id in _analyses:
        path = Path(_analyses[project_id].get("path", "."))
    else:
        path = Path(project_id)

    if not path.exists():
        raise HTTPException(status_code=404, detail="Project path not found")

    new_lines = get_new_code_lines(path, base, head)

    return {
        "base": base,
        "head": head,
        "new_lines": {
            file_path: lines
            for file_path, lines in new_lines.items()
        },
    }
