"""GitHub Issues integration."""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Optional

from codescope.integrations.issue_trackers.base import (
    CreateIssueRequest,
    IssuePriority,
    IssueStatus,
    IssueTrackerProvider,
    TrackerConfig,
    TrackerIssue,
    UpdateIssueRequest,
)

logger = logging.getLogger(__name__)


class GitHubIssuesClient(IssueTrackerProvider):
    """GitHub Issues integration client.

    Supports both GitHub.com and GitHub Enterprise Server.

    Configuration:
        - base_url: GitHub API URL (default: https://api.github.com)
        - project_key: Repository in format "owner/repo"
        - api_token: Personal Access Token or GitHub App token
    """

    # Map CodeScope priority to GitHub labels
    _PRIORITY_LABELS = {
        IssuePriority.HIGHEST: "priority:critical",
        IssuePriority.HIGH: "priority:high",
        IssuePriority.MEDIUM: "priority:medium",
        IssuePriority.LOW: "priority:low",
        IssuePriority.LOWEST: "priority:lowest",
    }

    # Map GitHub issue state to IssueStatus
    _STATUS_MAP = {
        "open": IssueStatus.OPEN,
        "closed": IssueStatus.CLOSED,
    }

    @property
    def name(self) -> str:
        return "github"

    @property
    def display_name(self) -> str:
        return "GitHub Issues"

    @property
    def _api_base(self) -> str:
        """Base URL for API calls."""
        if self.config.base_url:
            base = self.config.base_url.rstrip("/")
            # GitHub Enterprise uses /api/v3 suffix
            if "api.github.com" not in base and not base.endswith("/api/v3"):
                return f"{base}/api/v3"
            return base
        return "https://api.github.com"

    @property
    def _repo(self) -> str:
        """Get repository path (owner/repo)."""
        return self.config.project_key

    @property
    def _web_base(self) -> str:
        """Base URL for web links."""
        if self.config.base_url:
            # GitHub Enterprise
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
            "Accept": "application/vnd.github+json",
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
        result = self._request("GET", f"/repos/{self._repo}")
        if result and isinstance(result, dict):
            repo_name = result.get("full_name", self._repo)
            private = "private" if result.get("private") else "public"
            return True, f"Connected to {private} repository: {repo_name}"
        return False, "Failed to connect to GitHub. Check token and repository path."

    def create_issue(self, request: CreateIssueRequest) -> Optional[TrackerIssue]:
        """Create a new GitHub issue."""
        # Build labels list
        labels = list(request.labels) + self.config.default_labels + ["codescope"]

        # Add priority label
        priority_label = self._PRIORITY_LABELS.get(request.priority)
        if priority_label:
            labels.append(priority_label)

        # Add issue type as label
        if request.issue_type and request.issue_type != "Bug":
            labels.append(f"type:{request.issue_type.lower()}")
        else:
            labels.append("type:bug")

        # Build description with CodeScope metadata
        description = request.description
        if request.codescope_issue_ids:
            description += f"\n\n---\n**CodeScope Issue IDs:** {', '.join(request.codescope_issue_ids)}"

        payload: dict[str, Any] = {
            "title": request.title,
            "body": description,
            "labels": labels,
        }

        # Add assignee if provided
        if request.assignee:
            payload["assignees"] = [request.assignee]

        result = self._request("POST", f"/repos/{self._repo}/issues", data=payload)

        if result and isinstance(result, dict) and "number" in result:
            return self._parse_issue(result)
        return None

    def get_issue(self, issue_key: str) -> Optional[TrackerIssue]:
        """Get an issue by its number."""
        # Extract issue number from key
        issue_number = self._extract_issue_number(issue_key)
        if not issue_number:
            return None

        result = self._request("GET", f"/repos/{self._repo}/issues/{issue_number}")

        if result and isinstance(result, dict):
            return self._parse_issue(result)
        return None

    def update_issue(
        self, issue_key: str, request: UpdateIssueRequest
    ) -> Optional[TrackerIssue]:
        """Update an existing issue."""
        issue_number = self._extract_issue_number(issue_key)
        if not issue_number:
            return None

        payload: dict[str, Any] = {}

        if request.title:
            payload["title"] = request.title

        if request.description:
            payload["body"] = request.description

        if request.status:
            if request.status in (IssueStatus.CLOSED, IssueStatus.RESOLVED):
                payload["state"] = "closed"
                payload["state_reason"] = "completed"
            elif request.status == IssueStatus.REOPENED:
                payload["state"] = "open"
            else:
                payload["state"] = "open"

        if request.assignee:
            payload["assignees"] = [request.assignee]

        if request.labels is not None:
            payload["labels"] = request.labels

        if not payload:
            return self.get_issue(issue_key)

        result = self._request(
            "PATCH", f"/repos/{self._repo}/issues/{issue_number}", data=payload
        )

        if result and isinstance(result, dict):
            return self._parse_issue(result)
        return None

    def add_comment(self, issue_key: str, comment: str) -> bool:
        """Add a comment to an issue."""
        issue_number = self._extract_issue_number(issue_key)
        if not issue_number:
            return False

        result = self._request(
            "POST",
            f"/repos/{self._repo}/issues/{issue_number}/comments",
            data={"body": comment},
        )
        return result is not None

    def search_issues(
        self,
        query: Optional[str] = None,
        status: Optional[IssueStatus] = None,
        labels: Optional[list[str]] = None,
        max_results: int = 50,
    ) -> list[TrackerIssue]:
        """Search for issues."""
        # Build search query
        q_parts = [f"repo:{self._repo}", "is:issue", "label:codescope"]

        if query:
            q_parts.append(query)

        if status:
            if status in (IssueStatus.CLOSED, IssueStatus.RESOLVED):
                q_parts.append("is:closed")
            else:
                q_parts.append("is:open")

        if labels:
            for label in labels:
                q_parts.append(f'label:"{label}"')

        params = {
            "q": " ".join(q_parts),
            "sort": "updated",
            "order": "desc",
            "per_page": min(max_results, 100),
        }

        result = self._request("GET", "/search/issues", params=params)

        if not result or not isinstance(result, dict):
            return []

        items = result.get("items", [])
        return [self._parse_issue(issue) for issue in items[:max_results]]

    def get_available_issue_types(self) -> list[str]:
        """Get available issue types (label-based)."""
        # GitHub doesn't have issue types, but we use labels
        return ["Bug", "Feature", "Enhancement", "Documentation", "Question", "Task"]

    def get_available_priorities(self) -> list[str]:
        """Get available priority levels."""
        return ["Critical", "High", "Medium", "Low", "Lowest"]

    def get_project_users(self) -> list[dict[str, str]]:
        """Get users that can be assigned to issues."""
        result = self._request("GET", f"/repos/{self._repo}/collaborators")

        if not result or not isinstance(result, list):
            return []

        return [
            {
                "id": user.get("login", ""),
                "name": user.get("login", ""),
                "email": "",  # Not available from this endpoint
            }
            for user in result
        ]

    def get_labels(self) -> list[str]:
        """Get all labels in the repository."""
        result = self._request(
            "GET", f"/repos/{self._repo}/labels", params={"per_page": 100}
        )

        if not result or not isinstance(result, list):
            return []

        return [label.get("name", "") for label in result]

    def create_label(self, name: str, color: str = "ededed", description: str = "") -> bool:
        """Create a new label in the repository."""
        result = self._request(
            "POST",
            f"/repos/{self._repo}/labels",
            data={"name": name, "color": color, "description": description},
        )
        return result is not None

    def ensure_labels_exist(self) -> None:
        """Ensure required CodeScope labels exist in the repository."""
        required_labels = [
            ("codescope", "0366d6", "Created by CodeScope SCA"),
            ("priority:critical", "b60205", "Critical priority"),
            ("priority:high", "d93f0b", "High priority"),
            ("priority:medium", "fbca04", "Medium priority"),
            ("priority:low", "0e8a16", "Low priority"),
            ("priority:lowest", "c5def5", "Lowest priority"),
            ("type:bug", "d73a4a", "Bug report"),
            ("type:security", "b60205", "Security issue"),
            ("type:vulnerability", "b60205", "Vulnerability"),
        ]

        existing_labels = set(self.get_labels())

        for name, color, description in required_labels:
            if name not in existing_labels:
                self.create_label(name, color, description)
                logger.debug("Created label: %s", name)

    def _extract_issue_number(self, issue_key: str) -> Optional[int]:
        """Extract issue number from various key formats."""
        # Handle formats: "123", "#123", "owner/repo#123", "GH#123"
        key = issue_key.strip()

        if key.startswith("#"):
            key = key[1:]

        if "#" in key:
            key = key.split("#")[-1]

        if key.startswith("GH"):
            key = key[2:]

        try:
            return int(key)
        except ValueError:
            logger.warning("Invalid issue key format: %s", issue_key)
            return None

    def _parse_issue(self, data: dict) -> TrackerIssue:
        """Parse GitHub issue response into TrackerIssue."""
        # Parse status
        state = data.get("state", "open")
        status = self._STATUS_MAP.get(state, IssueStatus.OPEN)

        # Handle reopened state
        if state == "open" and data.get("state_reason") == "reopened":
            status = IssueStatus.REOPENED

        # Parse priority from labels
        priority = IssuePriority.MEDIUM
        labels = [label.get("name", "") for label in data.get("labels", [])]
        for p, label in self._PRIORITY_LABELS.items():
            if label in labels:
                priority = p
                break

        # Parse issue type from labels
        issue_type = "Bug"
        for label in labels:
            if label.startswith("type:"):
                issue_type = label.split(":")[1].title()
                break

        # Parse dates
        created_at = None
        updated_at = None
        if data.get("created_at"):
            try:
                created_at = datetime.fromisoformat(
                    data["created_at"].replace("Z", "+00:00")
                )
            except ValueError:
                pass
        if data.get("updated_at"):
            try:
                updated_at = datetime.fromisoformat(
                    data["updated_at"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

        # Get assignees
        assignees = data.get("assignees", [])
        assignee = assignees[0].get("login", "") if assignees else ""

        # Get reporter
        user = data.get("user", {})
        reporter = user.get("login", "") if user else ""

        # Build issue URL
        issue_number = data.get("number", "")
        url = data.get("html_url", f"{self._web_base}/{self._repo}/issues/{issue_number}")

        # Extract CodeScope issue IDs from body
        codescope_ids = []
        body = data.get("body", "") or ""
        if "CodeScope Issue IDs:" in body:
            try:
                ids_part = body.split("CodeScope Issue IDs:")[-1].strip().split("\n")[0]
                codescope_ids = [id.strip() for id in ids_part.split(",")]
            except Exception:
                pass

        # Filter out codescope-specific labels for display
        display_labels = [
            l for l in labels if not l.startswith("priority:") and l != "codescope"
        ]

        return TrackerIssue(
            id=str(data.get("id", "")),
            key=f"#{issue_number}",
            title=data.get("title", ""),
            description=body,
            status=status,
            priority=priority,
            issue_type=issue_type,
            assignee=assignee,
            reporter=reporter,
            labels=display_labels,
            created_at=created_at,
            updated_at=updated_at,
            url=url,
            codescope_issue_ids=codescope_ids,
        )

    def _format_description(
        self,
        issue_id: str,
        rule_id: str,
        message: str,
        file_path: str,
        line: int,
        severity: str,
        dashboard_url: str,
    ) -> str:
        """Format description for GitHub (markdown)."""
        lines = [
            "## CodeScope Issue",
            "",
            "| Property | Value |",
            "|----------|-------|",
            f"| **Rule** | `{rule_id}` |",
            f"| **Severity** | {severity} |",
            f"| **File** | `{file_path}` |",
            f"| **Line** | {line} |",
            "",
            "### Description",
            message,
            "",
            "---",
            f"*Issue ID: `{issue_id}`*",
        ]
        if dashboard_url:
            lines.append(f"\n[View in CodeScope Dashboard]({dashboard_url})")

        return "\n".join(lines)


class GitHubPullRequestClient:
    """GitHub Pull Request integration for code review comments.

    This client is used to post CodeScope analysis results as PR review comments.
    """

    def __init__(self, config: TrackerConfig) -> None:
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
    def _repo(self) -> str:
        """Get repository path (owner/repo)."""
        return self.config.project_key

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
    ) -> Optional[dict | list]:
        """Make an HTTP request to the GitHub API."""
        url = f"{self._api_base}{endpoint}"

        headers = {
            "Authorization": f"Bearer {self.config.api_token}",
            "Accept": "application/vnd.github+json",
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

    def get_pull_request(self, pr_number: int) -> Optional[dict]:
        """Get pull request details."""
        result = self._request("GET", f"/repos/{self._repo}/pulls/{pr_number}")
        if result and isinstance(result, dict):
            return result
        return None

    def create_review_comment(
        self,
        pr_number: int,
        body: str,
        commit_id: str,
        path: str,
        line: int,
        side: str = "RIGHT",
    ) -> bool:
        """Create a review comment on a specific line of a PR.

        Args:
            pr_number: Pull request number
            body: Comment body
            commit_id: SHA of the commit to comment on
            path: File path relative to repository root
            line: Line number in the diff
            side: LEFT or RIGHT (RIGHT for additions)
        """
        result = self._request(
            "POST",
            f"/repos/{self._repo}/pulls/{pr_number}/comments",
            data={
                "body": body,
                "commit_id": commit_id,
                "path": path,
                "line": line,
                "side": side,
            },
        )
        return result is not None

    def create_issue_comment(self, pr_number: int, body: str) -> bool:
        """Create a general comment on a PR (not attached to a line)."""
        result = self._request(
            "POST",
            f"/repos/{self._repo}/issues/{pr_number}/comments",
            data={"body": body},
        )
        return result is not None

    def create_review(
        self,
        pr_number: int,
        body: str,
        event: str = "COMMENT",
        comments: Optional[list[dict]] = None,
    ) -> bool:
        """Create a full PR review.

        Args:
            pr_number: Pull request number
            body: Review summary
            event: APPROVE, REQUEST_CHANGES, or COMMENT
            comments: List of review comments with path, line, body
        """
        payload: dict[str, Any] = {
            "body": body,
            "event": event,
        }

        if comments:
            payload["comments"] = comments

        result = self._request(
            "POST",
            f"/repos/{self._repo}/pulls/{pr_number}/reviews",
            data=payload,
        )
        return result is not None

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
        """Post a summary of CodeScope analysis results to a PR.

        Args:
            pr_number: Pull request number
            total_issues: Total number of issues found
            critical_count: Number of critical issues
            high_count: Number of high issues
            medium_count: Number of medium issues
            low_count: Number of low issues
            quality_gate_passed: Whether quality gate passed
            dashboard_url: URL to CodeScope dashboard
        """
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

    def post_check_run(
        self,
        head_sha: str,
        name: str = "CodeScope Analysis",
        conclusion: str = "success",
        title: str = "Analysis Complete",
        summary: str = "",
        text: str = "",
        annotations: Optional[list[dict]] = None,
    ) -> bool:
        """Create a check run with annotations.

        This is the preferred way to show inline annotations in GitHub.

        Args:
            head_sha: SHA of the commit to annotate
            name: Check run name
            conclusion: success, failure, neutral, cancelled, skipped, timed_out, action_required
            title: Check run title
            summary: Summary of the check run
            text: Detailed markdown text
            annotations: List of annotations with path, start_line, end_line, annotation_level, message
        """
        payload: dict[str, Any] = {
            "name": name,
            "head_sha": head_sha,
            "status": "completed",
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
            f"/repos/{self._repo}/check-runs",
            data=payload,
        )
        return result is not None
