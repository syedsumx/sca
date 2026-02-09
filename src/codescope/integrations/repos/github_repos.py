"""GitHub repository and pull request management integration."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PullRequestState(str, Enum):
    """Pull request state values."""

    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"


class MergeMethod(str, Enum):
    """Pull request merge methods."""

    MERGE = "merge"
    SQUASH = "squash"
    REBASE = "rebase"


@dataclass
class GitHubReposConfig:
    """Configuration for GitHub Repos connection."""

    owner: str
    repository: str
    api_token: str  # Personal Access Token or GitHub App token
    base_url: str = ""  # Optional for GitHub Enterprise

    @property
    def repo_path(self) -> str:
        """Full repository path (owner/repo)."""
        return f"{self.owner}/{self.repository}"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary (masks sensitive data)."""
        return {
            "owner": self.owner,
            "repository": self.repository,
            "base_url": self.base_url,
            "api_token": "***" if self.api_token else "",
        }


@dataclass
class PullRequest:
    """Represents a pull request in GitHub."""

    id: int
    number: int
    title: str
    body: str = ""
    source_branch: str = ""
    target_branch: str = ""
    state: PullRequestState = PullRequestState.OPEN
    author: str = ""
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    merged_at: Optional[datetime] = None
    url: str = ""
    reviewers: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    is_draft: bool = False
    mergeable: Optional[bool] = None
    head_sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "state": self.state.value,
            "author": self.author,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "merged_at": self.merged_at.isoformat() if self.merged_at else None,
            "url": self.url,
            "reviewers": self.reviewers,
            "labels": self.labels,
            "is_draft": self.is_draft,
            "mergeable": self.mergeable,
            "head_sha": self.head_sha,
        }


@dataclass
class Repository:
    """Represents a repository in GitHub."""

    id: int
    name: str
    full_name: str
    url: str
    default_branch: str = ""
    private: bool = False
    description: str = ""
    language: str = ""
    stars: int = 0
    forks: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "full_name": self.full_name,
            "url": self.url,
            "default_branch": self.default_branch,
            "private": self.private,
            "description": self.description,
            "language": self.language,
            "stars": self.stars,
            "forks": self.forks,
        }


@dataclass
class ReviewComment:
    """Represents a review comment in GitHub."""

    id: int
    body: str
    path: str
    line: Optional[int] = None
    commit_id: str = ""
    author: str = ""
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "body": self.body,
            "path": self.path,
            "line": self.line,
            "commit_id": self.commit_id,
            "author": self.author,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class GitHubReposClient:
    """GitHub repository and pull request integration client.

    Provides functionality for:
    - Repository management
    - Pull request operations
    - Code review comments
    - Check runs and statuses
    - Branch protection

    Configuration via environment variables:
        - CODESCOPE_GITHUB_OWNER: Repository owner
        - CODESCOPE_GITHUB_REPOSITORY: Repository name
        - CODESCOPE_GITHUB_TOKEN: Personal Access Token
        - CODESCOPE_GITHUB_URL: Base URL (optional, for GitHub Enterprise)
    """

    def __init__(self, config: GitHubReposConfig) -> None:
        self.config = config

    @property
    def _api_base(self) -> str:
        """Base URL for API calls."""
        if self.config.base_url:
            base = self.config.base_url.rstrip("/")
            if "api.github.com" not in base and not base.endswith("/api/v3"):
                return f"{base}/api/v3"
            return base
        return "https://api.github.com"

    @property
    def _web_base(self) -> str:
        """Base URL for web links."""
        if self.config.base_url:
            base = self.config.base_url.rstrip("/")
            if "/api/v3" in base:
                return base.replace("/api/v3", "")
            if "api." in base:
                return base.replace("api.", "")
            return base
        return "https://github.com"

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
        accept: str = "application/vnd.github+json",
    ) -> Optional[dict | list]:
        """Make an HTTP request to the GitHub API."""
        url = f"{self._api_base}{endpoint}"

        if params:
            query = "&".join(
                f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items()
            )
            url = f"{url}?{query}"

        headers = {
            "Authorization": f"Bearer {self.config.api_token}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
        }

        if data is not None:
            headers["Content-Type"] = "application/json"

        body = json.dumps(data).encode() if data else None

        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 204:
                    return {}
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            logger.error("GitHub API error %d: %s - %s", e.code, e.reason, error_body)
            return None
        except Exception as exc:
            logger.error("GitHub API request failed: %s", exc)
            return None

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to GitHub repository."""
        result = self._request("GET", f"/repos/{self.config.repo_path}")
        if result and isinstance(result, dict):
            repo_name = result.get("full_name", self.config.repo_path)
            private = "private" if result.get("private") else "public"
            return True, f"Connected to {private} repository: {repo_name}"
        return False, "Failed to connect to GitHub. Check token and repository path."

    # ========== Repository Operations ==========

    def get_repository(self) -> Optional[Repository]:
        """Get repository details."""
        result = self._request("GET", f"/repos/{self.config.repo_path}")
        if not result or not isinstance(result, dict):
            return None
        return self._parse_repository(result)

    def list_repositories(self, org: Optional[str] = None) -> list[Repository]:
        """List repositories for the authenticated user or organization."""
        if org:
            endpoint = f"/orgs/{org}/repos"
        else:
            endpoint = "/user/repos"

        result = self._request("GET", endpoint, params={"per_page": 100})

        if not result or not isinstance(result, list):
            return []

        return [self._parse_repository(repo) for repo in result]

    def get_branches(self) -> list[dict[str, Any]]:
        """Get all branches in the repository."""
        result = self._request(
            "GET",
            f"/repos/{self.config.repo_path}/branches",
            params={"per_page": 100},
        )

        if not result or not isinstance(result, list):
            return []

        return [
            {
                "name": branch.get("name", ""),
                "sha": branch.get("commit", {}).get("sha", ""),
                "protected": branch.get("protected", False),
            }
            for branch in result
        ]

    def get_default_branch(self) -> str:
        """Get the default branch of the repository."""
        repo = self.get_repository()
        return repo.default_branch if repo else "main"

    # ========== Pull Request Operations ==========

    def get_pull_request(self, pr_number: int) -> Optional[PullRequest]:
        """Get pull request by number."""
        result = self._request(
            "GET",
            f"/repos/{self.config.repo_path}/pulls/{pr_number}",
        )
        if not result or not isinstance(result, dict):
            return None
        return self._parse_pull_request(result)

    def list_pull_requests(
        self,
        state: PullRequestState = PullRequestState.OPEN,
        head: Optional[str] = None,
        base: Optional[str] = None,
        sort: str = "created",
        direction: str = "desc",
        max_results: int = 50,
    ) -> list[PullRequest]:
        """List pull requests in the repository."""
        params: dict[str, Any] = {
            "state": state.value,
            "sort": sort,
            "direction": direction,
            "per_page": min(max_results, 100),
        }

        if head:
            params["head"] = head

        if base:
            params["base"] = base

        result = self._request(
            "GET",
            f"/repos/{self.config.repo_path}/pulls",
            params=params,
        )

        if not result or not isinstance(result, list):
            return []

        return [self._parse_pull_request(pr) for pr in result[:max_results]]

    def create_pull_request(
        self,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
        maintainer_can_modify: bool = True,
    ) -> Optional[PullRequest]:
        """Create a new pull request."""
        payload: dict[str, Any] = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "draft": draft,
            "maintainer_can_modify": maintainer_can_modify,
        }

        result = self._request(
            "POST",
            f"/repos/{self.config.repo_path}/pulls",
            data=payload,
        )

        if result and isinstance(result, dict) and "number" in result:
            return self._parse_pull_request(result)
        return None

    def update_pull_request(
        self,
        pr_number: int,
        title: Optional[str] = None,
        body: Optional[str] = None,
        state: Optional[PullRequestState] = None,
        base: Optional[str] = None,
    ) -> Optional[PullRequest]:
        """Update an existing pull request."""
        payload: dict[str, Any] = {}

        if title:
            payload["title"] = title

        if body is not None:
            payload["body"] = body

        if state:
            payload["state"] = state.value

        if base:
            payload["base"] = base

        if not payload:
            return self.get_pull_request(pr_number)

        result = self._request(
            "PATCH",
            f"/repos/{self.config.repo_path}/pulls/{pr_number}",
            data=payload,
        )

        if result and isinstance(result, dict):
            return self._parse_pull_request(result)
        return None

    def merge_pull_request(
        self,
        pr_number: int,
        commit_title: Optional[str] = None,
        commit_message: Optional[str] = None,
        merge_method: MergeMethod = MergeMethod.MERGE,
    ) -> bool:
        """Merge a pull request."""
        payload: dict[str, Any] = {
            "merge_method": merge_method.value,
        }

        if commit_title:
            payload["commit_title"] = commit_title

        if commit_message:
            payload["commit_message"] = commit_message

        result = self._request(
            "PUT",
            f"/repos/{self.config.repo_path}/pulls/{pr_number}/merge",
            data=payload,
        )

        return result is not None and result.get("merged", False)

    def request_reviewers(
        self,
        pr_number: int,
        reviewers: list[str],
        team_reviewers: Optional[list[str]] = None,
    ) -> bool:
        """Request reviewers for a pull request."""
        payload: dict[str, Any] = {"reviewers": reviewers}

        if team_reviewers:
            payload["team_reviewers"] = team_reviewers

        result = self._request(
            "POST",
            f"/repos/{self.config.repo_path}/pulls/{pr_number}/requested_reviewers",
            data=payload,
        )

        return result is not None

    def add_labels(self, pr_number: int, labels: list[str]) -> bool:
        """Add labels to a pull request."""
        result = self._request(
            "POST",
            f"/repos/{self.config.repo_path}/issues/{pr_number}/labels",
            data={"labels": labels},
        )
        return result is not None

    # ========== Review Operations ==========

    def create_review(
        self,
        pr_number: int,
        body: str = "",
        event: str = "COMMENT",
        comments: Optional[list[dict]] = None,
        commit_id: Optional[str] = None,
    ) -> bool:
        """Create a pull request review.

        Args:
            pr_number: Pull request number
            body: Review summary
            event: APPROVE, REQUEST_CHANGES, COMMENT, or PENDING
            comments: List of review comments with path, position/line, body
            commit_id: SHA of commit to review (defaults to latest)
        """
        payload: dict[str, Any] = {
            "body": body,
            "event": event,
        }

        if comments:
            payload["comments"] = comments

        if commit_id:
            payload["commit_id"] = commit_id

        result = self._request(
            "POST",
            f"/repos/{self.config.repo_path}/pulls/{pr_number}/reviews",
            data=payload,
        )

        return result is not None

    def create_review_comment(
        self,
        pr_number: int,
        body: str,
        commit_id: str,
        path: str,
        line: int,
        side: str = "RIGHT",
    ) -> bool:
        """Create a review comment on a specific line."""
        payload: dict[str, Any] = {
            "body": body,
            "commit_id": commit_id,
            "path": path,
            "line": line,
            "side": side,
        }

        result = self._request(
            "POST",
            f"/repos/{self.config.repo_path}/pulls/{pr_number}/comments",
            data=payload,
        )

        return result is not None

    def create_issue_comment(self, pr_number: int, body: str) -> bool:
        """Create a general comment on a pull request."""
        result = self._request(
            "POST",
            f"/repos/{self.config.repo_path}/issues/{pr_number}/comments",
            data={"body": body},
        )
        return result is not None

    def get_review_comments(self, pr_number: int) -> list[ReviewComment]:
        """Get all review comments on a pull request."""
        result = self._request(
            "GET",
            f"/repos/{self.config.repo_path}/pulls/{pr_number}/comments",
            params={"per_page": 100},
        )

        if not result or not isinstance(result, list):
            return []

        return [self._parse_review_comment(comment) for comment in result]

    # ========== Check Runs and Statuses ==========

    def create_check_run(
        self,
        head_sha: str,
        name: str = "CodeScope Analysis",
        status: str = "completed",
        conclusion: str = "success",
        title: str = "Analysis Complete",
        summary: str = "",
        text: str = "",
        annotations: Optional[list[dict]] = None,
    ) -> bool:
        """Create a check run with annotations.

        Args:
            head_sha: SHA of the commit to annotate
            name: Check run name
            status: queued, in_progress, completed
            conclusion: action_required, cancelled, failure, neutral, success, skipped, stale, timed_out
            title: Check run title
            summary: Summary of the check run
            text: Detailed markdown text
            annotations: List of annotations with path, start_line, end_line, annotation_level, message
        """
        payload: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": status,
            "conclusion": conclusion,
            "output": {
                "title": title,
                "summary": summary,
                "text": text,
            },
        }

        if annotations:
            # GitHub limits annotations to 50 per request
            payload["output"]["annotations"] = annotations[:50]

        result = self._request(
            "POST",
            f"/repos/{self.config.repo_path}/check-runs",
            data=payload,
        )

        return result is not None

    def create_commit_status(
        self,
        sha: str,
        state: str,
        description: str = "",
        target_url: str = "",
        context: str = "codescope",
    ) -> bool:
        """Create a commit status.

        Args:
            sha: Commit SHA
            state: error, failure, pending, success
            description: Short description
            target_url: URL for details
            context: Status context
        """
        payload: dict[str, Any] = {
            "state": state,
            "description": description[:140],  # GitHub limit
            "context": context,
        }

        if target_url:
            payload["target_url"] = target_url

        result = self._request(
            "POST",
            f"/repos/{self.config.repo_path}/statuses/{sha}",
            data=payload,
        )

        return result is not None

    # ========== CodeScope Integration ==========

    def post_analysis_summary(
        self,
        pr_number: int,
        total_issues: int,
        critical_count: int,
        high_count: int,
        medium_count: int,
        low_count: int,
        quality_gate_passed: bool,
        dashboard_url: str = "",
    ) -> bool:
        """Post a summary of CodeScope analysis results to a PR."""
        status_emoji = ":white_check_mark:" if quality_gate_passed else ":x:"
        status_text = "Passed" if quality_gate_passed else "Failed"

        body = f"""## CodeScope Analysis Results

{status_emoji} **Quality Gate: {status_text}**

### Issue Summary

| Severity | Count |
|----------|-------|
| :red_circle: Critical | {critical_count} |
| :orange_circle: High | {high_count} |
| :yellow_circle: Medium | {medium_count} |
| :green_circle: Low | {low_count} |
| **Total** | **{total_issues}** |
"""

        if dashboard_url:
            body += f"\n[View full report in CodeScope Dashboard]({dashboard_url})"

        return self.create_issue_comment(pr_number, body)

    def post_check_run_with_annotations(
        self,
        head_sha: str,
        issues: list[dict],
        quality_gate_passed: bool,
        dashboard_url: str = "",
    ) -> bool:
        """Post CodeScope analysis results as a check run with inline annotations.

        Args:
            head_sha: Commit SHA to annotate
            issues: List of issues with file_path, line, severity, rule_id, message
            quality_gate_passed: Whether quality gate passed
            dashboard_url: URL to CodeScope dashboard
        """
        conclusion = "success" if quality_gate_passed else "failure"
        title = "Quality Gate Passed" if quality_gate_passed else "Quality Gate Failed"

        # Count issues by severity
        severity_counts = {"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for issue in issues:
            severity = issue.get("severity", "MEDIUM").upper()
            if severity in severity_counts:
                severity_counts[severity] += 1

        summary = f"""## Analysis Summary

| Severity | Count |
|----------|-------|
| Critical | {severity_counts['CRITICAL']} |
| High | {severity_counts['HIGH']} |
| Medium | {severity_counts['MEDIUM']} |
| Low | {severity_counts['LOW']} |
| **Total** | **{len(issues)}** |
"""

        if dashboard_url:
            summary += f"\n[View full report]({dashboard_url})"

        # Convert issues to annotations
        level_map = {
            "CRITICAL": "failure",
            "HIGH": "failure",
            "MEDIUM": "warning",
            "LOW": "notice",
            "INFO": "notice",
        }

        annotations = []
        for issue in issues[:50]:  # GitHub limit
            annotations.append({
                "path": issue.get("file_path", ""),
                "start_line": issue.get("line", 1),
                "end_line": issue.get("line", 1),
                "annotation_level": level_map.get(issue.get("severity", "MEDIUM").upper(), "warning"),
                "message": f"[{issue.get('rule_id', 'unknown')}] {issue.get('message', '')}",
                "title": issue.get("rule_id", "CodeScope Issue"),
            })

        return self.create_check_run(
            head_sha=head_sha,
            conclusion=conclusion,
            title=title,
            summary=summary,
            annotations=annotations if annotations else None,
        )

    # ========== File Operations ==========

    def get_file_content(
        self,
        path: str,
        ref: Optional[str] = None,
    ) -> Optional[str]:
        """Get the content of a file from the repository.

        Args:
            path: File path relative to repository root
            ref: Optional branch, tag, or commit SHA
        """
        params = {}
        if ref:
            params["ref"] = ref

        result = self._request(
            "GET",
            f"/repos/{self.config.repo_path}/contents/{path}",
            params=params if params else None,
        )

        if result and isinstance(result, dict) and "content" in result:
            import base64
            content = result["content"]
            try:
                return base64.b64decode(content).decode()
            except Exception:
                return None
        return None

    def get_diff(self, pr_number: int) -> list[dict]:
        """Get the files changed in a pull request."""
        result = self._request(
            "GET",
            f"/repos/{self.config.repo_path}/pulls/{pr_number}/files",
            params={"per_page": 100},
        )

        if not result or not isinstance(result, list):
            return []

        return [
            {
                "filename": f.get("filename", ""),
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
                "patch": f.get("patch", ""),
            }
            for f in result
        ]

    # ========== Parsing Helpers ==========

    def _parse_repository(self, data: dict) -> Repository:
        """Parse repository response."""
        return Repository(
            id=data.get("id", 0),
            name=data.get("name", ""),
            full_name=data.get("full_name", ""),
            url=data.get("html_url", ""),
            default_branch=data.get("default_branch", "main"),
            private=data.get("private", False),
            description=data.get("description", "") or "",
            language=data.get("language", "") or "",
            stars=data.get("stargazers_count", 0),
            forks=data.get("forks_count", 0),
        )

    def _parse_pull_request(self, data: dict) -> PullRequest:
        """Parse pull request response."""
        state_str = data.get("state", "open")
        state = PullRequestState(state_str) if state_str in [s.value for s in PullRequestState] else PullRequestState.OPEN

        created_at = None
        updated_at = None
        merged_at = None

        if data.get("created_at"):
            try:
                created_at = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
            except ValueError:
                pass

        if data.get("updated_at"):
            try:
                updated_at = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
            except ValueError:
                pass

        if data.get("merged_at"):
            try:
                merged_at = datetime.fromisoformat(data["merged_at"].replace("Z", "+00:00"))
            except ValueError:
                pass

        user = data.get("user", {})
        author = user.get("login", "") if user else ""

        reviewers = []
        for reviewer in data.get("requested_reviewers", []):
            reviewers.append(reviewer.get("login", ""))

        labels = [label.get("name", "") for label in data.get("labels", [])]

        head = data.get("head", {})
        base = data.get("base", {})

        return PullRequest(
            id=data.get("id", 0),
            number=data.get("number", 0),
            title=data.get("title", ""),
            body=data.get("body", "") or "",
            source_branch=head.get("ref", ""),
            target_branch=base.get("ref", ""),
            state=state,
            author=author,
            created_at=created_at,
            updated_at=updated_at,
            merged_at=merged_at,
            url=data.get("html_url", ""),
            reviewers=reviewers,
            labels=labels,
            is_draft=data.get("draft", False),
            mergeable=data.get("mergeable"),
            head_sha=head.get("sha", ""),
        )

    def _parse_review_comment(self, data: dict) -> ReviewComment:
        """Parse review comment response."""
        created_at = None
        if data.get("created_at"):
            try:
                created_at = datetime.fromisoformat(data["created_at"].replace("Z", "+00:00"))
            except ValueError:
                pass

        user = data.get("user", {})
        author = user.get("login", "") if user else ""

        return ReviewComment(
            id=data.get("id", 0),
            body=data.get("body", ""),
            path=data.get("path", ""),
            line=data.get("line"),
            commit_id=data.get("commit_id", ""),
            author=author,
            created_at=created_at,
        )


def get_github_repos_client() -> Optional[GitHubReposClient]:
    """Get a configured GitHub Repos client from environment variables.

    Environment variables:
        - CODESCOPE_GITHUB_OWNER: Repository owner
        - CODESCOPE_GITHUB_REPOSITORY: Repository name
        - CODESCOPE_GITHUB_TOKEN: Personal Access Token
        - CODESCOPE_GITHUB_URL: Base URL (optional, for GitHub Enterprise)

    Returns:
        Configured client or None if not configured.
    """
    import os

    owner = os.environ.get("CODESCOPE_GITHUB_OWNER", "")
    repository = os.environ.get("CODESCOPE_GITHUB_REPOSITORY", "")
    token = os.environ.get("CODESCOPE_GITHUB_TOKEN", "")
    base_url = os.environ.get("CODESCOPE_GITHUB_URL", "")

    if not all([owner, repository, token]):
        logger.debug("GitHub Repos not configured: missing required environment variables")
        return None

    config = GitHubReposConfig(
        owner=owner,
        repository=repository,
        api_token=token,
        base_url=base_url,
    )

    return GitHubReposClient(config)
