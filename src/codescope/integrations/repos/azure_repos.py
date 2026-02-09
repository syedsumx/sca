"""Azure DevOps Repos integration for repository and pull request management."""

from __future__ import annotations

import base64
import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class PullRequestStatus(str, Enum):
    """Pull request status values."""

    ACTIVE = "active"
    ABANDONED = "abandoned"
    COMPLETED = "completed"
    ALL = "all"


class CommentThreadStatus(str, Enum):
    """Comment thread status values."""

    ACTIVE = "active"
    FIXED = "fixed"
    WONT_FIX = "wontFix"
    CLOSED = "closed"
    BY_DESIGN = "byDesign"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass
class AzureReposConfig:
    """Configuration for Azure DevOps Repos connection."""

    organization: str
    project: str
    repository: str
    api_token: str  # Personal Access Token
    base_url: str = ""  # Optional for on-premise Azure DevOps Server

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary (masks sensitive data)."""
        return {
            "organization": self.organization,
            "project": self.project,
            "repository": self.repository,
            "base_url": self.base_url,
            "api_token": "***" if self.api_token else "",
        }


@dataclass
class PullRequest:
    """Represents a pull request in Azure DevOps."""

    id: int
    title: str
    description: str = ""
    source_branch: str = ""
    target_branch: str = ""
    status: PullRequestStatus = PullRequestStatus.ACTIVE
    created_by: str = ""
    created_at: Optional[datetime] = None
    url: str = ""
    reviewers: list[str] = field(default_factory=list)
    is_draft: bool = False
    merge_status: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "source_branch": self.source_branch,
            "target_branch": self.target_branch,
            "status": self.status.value,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "url": self.url,
            "reviewers": self.reviewers,
            "is_draft": self.is_draft,
            "merge_status": self.merge_status,
        }


@dataclass
class Repository:
    """Represents a repository in Azure DevOps."""

    id: str
    name: str
    url: str
    default_branch: str = ""
    size: int = 0
    project: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "default_branch": self.default_branch,
            "size": self.size,
            "project": self.project,
        }


class AzureReposClient:
    """Azure DevOps Repos integration client.

    Provides functionality for:
    - Repository management
    - Pull request operations
    - Code review comments
    - Branch policies
    - File operations

    Configuration via environment variables:
        - CODESCOPE_AZURE_REPOS_ORGANIZATION: Azure DevOps organization
        - CODESCOPE_AZURE_REPOS_PROJECT: Project name
        - CODESCOPE_AZURE_REPOS_REPOSITORY: Repository name
        - CODESCOPE_AZURE_REPOS_TOKEN: Personal Access Token
        - CODESCOPE_AZURE_REPOS_URL: Base URL (optional, for on-premise)
    """

    def __init__(self, config: AzureReposConfig) -> None:
        self.config = config

    @property
    def _api_base(self) -> str:
        """Base URL for API calls."""
        org = self.config.organization
        if self.config.base_url:
            base = self.config.base_url.rstrip("/")
            return f"{base}/{org}"
        return f"https://dev.azure.com/{org}"

    @property
    def _git_api_base(self) -> str:
        """Base URL for Git API calls."""
        return f"{self._api_base}/{self.config.project}/_apis/git"

    @property
    def _auth_header(self) -> str:
        """Generate authorization header."""
        credentials = f":{self.config.api_token}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

    @property
    def _web_base(self) -> str:
        """Base URL for web links."""
        org = self.config.organization
        project = self.config.project
        repo = self.config.repository
        if self.config.base_url:
            return f"{self.config.base_url}/{org}/{project}/_git/{repo}"
        return f"https://dev.azure.com/{org}/{project}/_git/{repo}"

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Any] = None,
        params: Optional[dict] = None,
        api_version: str = "7.1",
        content_type: str = "application/json",
    ) -> Optional[dict]:
        """Make an HTTP request to the Azure DevOps API."""
        url = f"{self._git_api_base}{endpoint}"

        query_params = {"api-version": api_version}
        if params:
            query_params.update(params)

        query = "&".join(
            f"{k}={urllib.parse.quote(str(v))}" for k, v in query_params.items()
        )
        url = f"{url}?{query}"

        headers = {
            "Authorization": self._auth_header,
            "Content-Type": content_type,
            "Accept": "application/json",
        }

        body = None
        if data is not None:
            body = json.dumps(data).encode()

        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 204:
                    return {}
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            logger.error("Azure DevOps API error %d: %s - %s", e.code, e.reason, error_body)
            return None
        except Exception as exc:
            logger.error("Azure DevOps API request failed: %s", exc)
            return None

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to Azure DevOps repository."""
        result = self._request(
            "GET",
            f"/repositories/{urllib.parse.quote(self.config.repository)}",
        )
        if result and "id" in result:
            repo_name = result.get("name", self.config.repository)
            return True, f"Connected to repository: {repo_name}"
        return False, "Failed to connect to Azure DevOps. Check PAT and repository settings."

    # ========== Repository Operations ==========

    def get_repository(self) -> Optional[Repository]:
        """Get repository details."""
        result = self._request(
            "GET",
            f"/repositories/{urllib.parse.quote(self.config.repository)}",
        )
        if not result:
            return None
        return self._parse_repository(result)

    def list_repositories(self) -> list[Repository]:
        """List all repositories in the project."""
        result = self._request("GET", "/repositories")
        if not result or "value" not in result:
            return []
        return [self._parse_repository(repo) for repo in result["value"]]

    def get_branches(self) -> list[dict[str, Any]]:
        """Get all branches in the repository."""
        result = self._request(
            "GET",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/refs",
            params={"filter": "heads/"},
        )
        if not result or "value" not in result:
            return []

        return [
            {
                "name": ref["name"].replace("refs/heads/", ""),
                "object_id": ref.get("objectId", ""),
                "creator": ref.get("creator", {}).get("displayName", ""),
            }
            for ref in result["value"]
        ]

    # ========== Pull Request Operations ==========

    def get_pull_request(self, pr_id: int) -> Optional[PullRequest]:
        """Get pull request by ID."""
        result = self._request(
            "GET",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}",
        )
        if not result:
            return None
        return self._parse_pull_request(result)

    def list_pull_requests(
        self,
        status: PullRequestStatus = PullRequestStatus.ACTIVE,
        target_branch: Optional[str] = None,
        source_branch: Optional[str] = None,
        max_results: int = 50,
    ) -> list[PullRequest]:
        """List pull requests in the repository."""
        params: dict[str, Any] = {
            "searchCriteria.status": status.value,
            "$top": min(max_results, 100),
        }

        if target_branch:
            params["searchCriteria.targetRefName"] = f"refs/heads/{target_branch}"

        if source_branch:
            params["searchCriteria.sourceRefName"] = f"refs/heads/{source_branch}"

        result = self._request(
            "GET",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests",
            params=params,
        )

        if not result or "value" not in result:
            return []

        return [self._parse_pull_request(pr) for pr in result["value"]]

    def create_pull_request(
        self,
        title: str,
        source_branch: str,
        target_branch: str,
        description: str = "",
        reviewers: Optional[list[str]] = None,
        is_draft: bool = False,
    ) -> Optional[PullRequest]:
        """Create a new pull request."""
        payload: dict[str, Any] = {
            "title": title,
            "sourceRefName": f"refs/heads/{source_branch}",
            "targetRefName": f"refs/heads/{target_branch}",
            "description": description,
            "isDraft": is_draft,
        }

        if reviewers:
            payload["reviewers"] = [{"id": r} for r in reviewers]

        result = self._request(
            "POST",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests",
            data=payload,
        )

        if result and "pullRequestId" in result:
            return self._parse_pull_request(result)
        return None

    def update_pull_request(
        self,
        pr_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[PullRequestStatus] = None,
        target_branch: Optional[str] = None,
    ) -> Optional[PullRequest]:
        """Update an existing pull request."""
        payload: dict[str, Any] = {}

        if title:
            payload["title"] = title

        if description is not None:
            payload["description"] = description

        if status:
            payload["status"] = status.value

        if target_branch:
            payload["targetRefName"] = f"refs/heads/{target_branch}"

        if not payload:
            return self.get_pull_request(pr_id)

        result = self._request(
            "PATCH",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}",
            data=payload,
        )

        if result:
            return self._parse_pull_request(result)
        return None

    def add_reviewer(self, pr_id: int, reviewer_id: str, is_required: bool = False) -> bool:
        """Add a reviewer to a pull request."""
        vote = 0  # 0 = no vote
        result = self._request(
            "PUT",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}/reviewers/{reviewer_id}",
            data={"vote": vote, "isRequired": is_required},
        )
        return result is not None

    def set_vote(self, pr_id: int, reviewer_id: str, vote: int) -> bool:
        """Set reviewer vote on a pull request.

        Vote values:
        - 10: Approved
        - 5: Approved with suggestions
        - 0: No vote
        - -5: Waiting for author
        - -10: Rejected
        """
        result = self._request(
            "PUT",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}/reviewers/{reviewer_id}",
            data={"vote": vote},
        )
        return result is not None

    # ========== Comment Operations ==========

    def create_thread(
        self,
        pr_id: int,
        content: str,
        file_path: Optional[str] = None,
        line: Optional[int] = None,
        line_end: Optional[int] = None,
        status: CommentThreadStatus = CommentThreadStatus.ACTIVE,
    ) -> Optional[dict]:
        """Create a comment thread on a pull request.

        Args:
            pr_id: Pull request ID
            content: Comment content
            file_path: Optional file path for inline comments
            line: Optional starting line number
            line_end: Optional ending line number
            status: Thread status
        """
        payload: dict[str, Any] = {
            "comments": [{"content": content, "commentType": 1}],  # 1 = text
            "status": status.value,
        }

        if file_path:
            thread_context: dict[str, Any] = {
                "filePath": file_path,
            }
            if line:
                thread_context["rightFileStart"] = {"line": line, "offset": 1}
                thread_context["rightFileEnd"] = {
                    "line": line_end or line,
                    "offset": 1,
                }
            payload["threadContext"] = thread_context

        result = self._request(
            "POST",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}/threads",
            data=payload,
        )
        return result

    def add_comment_to_thread(
        self,
        pr_id: int,
        thread_id: int,
        content: str,
    ) -> bool:
        """Add a comment to an existing thread."""
        result = self._request(
            "POST",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}/threads/{thread_id}/comments",
            data={"content": content, "commentType": 1},
        )
        return result is not None

    def update_thread_status(
        self,
        pr_id: int,
        thread_id: int,
        status: CommentThreadStatus,
    ) -> bool:
        """Update the status of a comment thread."""
        result = self._request(
            "PATCH",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}/threads/{thread_id}",
            data={"status": status.value},
        )
        return result is not None

    def get_threads(
        self,
        pr_id: int,
        status: Optional[CommentThreadStatus] = None,
    ) -> list[dict]:
        """Get all comment threads on a pull request."""
        result = self._request(
            "GET",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}/threads",
        )

        if not result or "value" not in result:
            return []

        threads = result["value"]

        if status:
            threads = [t for t in threads if t.get("status") == status.value]

        return threads

    # ========== CodeScope Integration ==========

    def post_analysis_summary(
        self,
        pr_id: int,
        total_issues: int,
        critical_count: int,
        high_count: int,
        medium_count: int,
        low_count: int,
        quality_gate_passed: bool,
        dashboard_url: str = "",
    ) -> bool:
        """Post a summary of CodeScope analysis results to a PR.

        Creates a new comment thread with the analysis summary.
        """
        status_text = "Passed ✅" if quality_gate_passed else "Failed ❌"

        content = f"""## CodeScope Analysis Results

**Quality Gate: {status_text}**

### Issue Summary

| Severity | Count |
|----------|-------|
| 🔴 Critical | {critical_count} |
| 🟠 High | {high_count} |
| 🟡 Medium | {medium_count} |
| 🟢 Low | {low_count} |
| **Total** | **{total_issues}** |
"""

        if dashboard_url:
            content += f"\n[View full report in CodeScope Dashboard]({dashboard_url})"

        result = self.create_thread(pr_id, content)
        return result is not None

    def post_inline_comment(
        self,
        pr_id: int,
        file_path: str,
        line: int,
        rule_id: str,
        message: str,
        severity: str,
    ) -> bool:
        """Post an inline comment for a CodeScope issue."""
        content = f"""**CodeScope: {severity}** - `{rule_id}`

{message}
"""

        result = self.create_thread(
            pr_id,
            content,
            file_path=file_path,
            line=line,
            status=CommentThreadStatus.ACTIVE,
        )
        return result is not None

    def post_pr_status(
        self,
        pr_id: int,
        quality_gate_passed: bool,
        description: str = "",
    ) -> bool:
        """Set a status on the PR based on quality gate.

        Uses the PR reviewers vote mechanism to indicate pass/fail.
        Returns True if successful.
        """
        # Get the current user's ID
        # Note: This requires getting the identity from the API
        # For now, we'll just post a comment
        status_text = "passed" if quality_gate_passed else "failed"
        content = f"**CodeScope Quality Gate {status_text}**"
        if description:
            content += f"\n\n{description}"

        result = self.create_thread(pr_id, content)
        return result is not None

    # ========== File Operations ==========

    def get_file_content(
        self,
        path: str,
        version: Optional[str] = None,
    ) -> Optional[str]:
        """Get the content of a file from the repository.

        Args:
            path: File path relative to repository root
            version: Optional branch, tag, or commit SHA
        """
        params = {}
        if version:
            params["version"] = version

        result = self._request(
            "GET",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/items",
            params={"path": path, "includeContent": "true", **params},
        )

        if result and "content" in result:
            return result["content"]
        return None

    def get_diff(self, pr_id: int) -> list[dict]:
        """Get the diff for a pull request."""
        result = self._request(
            "GET",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}/iterations",
        )

        if not result or "value" not in result:
            return []

        # Get the latest iteration
        iterations = result["value"]
        if not iterations:
            return []

        latest_iteration = max(iterations, key=lambda x: x.get("id", 0))
        iteration_id = latest_iteration["id"]

        # Get changes for this iteration
        changes_result = self._request(
            "GET",
            f"/repositories/{urllib.parse.quote(self.config.repository)}/pullrequests/{pr_id}/iterations/{iteration_id}/changes",
        )

        if changes_result and "changeEntries" in changes_result:
            return changes_result["changeEntries"]
        return []

    # ========== Parsing Helpers ==========

    def _parse_repository(self, data: dict) -> Repository:
        """Parse repository response."""
        default_branch = data.get("defaultBranch", "")
        if default_branch.startswith("refs/heads/"):
            default_branch = default_branch[11:]

        return Repository(
            id=data.get("id", ""),
            name=data.get("name", ""),
            url=data.get("webUrl", ""),
            default_branch=default_branch,
            size=data.get("size", 0),
            project=data.get("project", {}).get("name", ""),
        )

    def _parse_pull_request(self, data: dict) -> PullRequest:
        """Parse pull request response."""
        source_branch = data.get("sourceRefName", "")
        if source_branch.startswith("refs/heads/"):
            source_branch = source_branch[11:]

        target_branch = data.get("targetRefName", "")
        if target_branch.startswith("refs/heads/"):
            target_branch = target_branch[11:]

        status_str = data.get("status", "active").lower()
        status = PullRequestStatus(status_str) if status_str in PullRequestStatus.__members__.values() else PullRequestStatus.ACTIVE

        created_at = None
        if data.get("creationDate"):
            try:
                created_at = datetime.fromisoformat(
                    data["creationDate"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

        created_by = ""
        if data.get("createdBy"):
            created_by = data["createdBy"].get("displayName", "")

        reviewers = []
        for reviewer in data.get("reviewers", []):
            reviewers.append(reviewer.get("displayName", ""))

        # Build PR URL
        pr_id = data.get("pullRequestId", "")
        url = f"{self._web_base}/pullrequest/{pr_id}"

        return PullRequest(
            id=pr_id,
            title=data.get("title", ""),
            description=data.get("description", ""),
            source_branch=source_branch,
            target_branch=target_branch,
            status=status,
            created_by=created_by,
            created_at=created_at,
            url=url,
            reviewers=reviewers,
            is_draft=data.get("isDraft", False),
            merge_status=data.get("mergeStatus", ""),
        )


def get_azure_repos_client() -> Optional[AzureReposClient]:
    """Get a configured Azure Repos client from environment variables.

    Environment variables:
        - CODESCOPE_AZURE_REPOS_ORGANIZATION: Azure DevOps organization
        - CODESCOPE_AZURE_REPOS_PROJECT: Project name
        - CODESCOPE_AZURE_REPOS_REPOSITORY: Repository name
        - CODESCOPE_AZURE_REPOS_TOKEN: Personal Access Token
        - CODESCOPE_AZURE_REPOS_URL: Base URL (optional, for on-premise)

    Returns:
        Configured client or None if not configured.
    """
    import os

    organization = os.environ.get("CODESCOPE_AZURE_REPOS_ORGANIZATION", "")
    project = os.environ.get("CODESCOPE_AZURE_REPOS_PROJECT", "")
    repository = os.environ.get("CODESCOPE_AZURE_REPOS_REPOSITORY", "")
    token = os.environ.get("CODESCOPE_AZURE_REPOS_TOKEN", "")
    base_url = os.environ.get("CODESCOPE_AZURE_REPOS_URL", "")

    if not all([organization, project, repository, token]):
        logger.debug("Azure Repos not configured: missing required environment variables")
        return None

    config = AzureReposConfig(
        organization=organization,
        project=project,
        repository=repository,
        api_token=token,
        base_url=base_url,
    )

    return AzureReposClient(config)
