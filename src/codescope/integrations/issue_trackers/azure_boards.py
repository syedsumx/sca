"""Azure Boards (Azure DevOps) issue tracker integration."""

from __future__ import annotations

import base64
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


class AzureBoardsClient(IssueTrackerProvider):
    """Azure Boards (Azure DevOps) integration client."""

    # Map CodeScope priority to Azure DevOps priority values
    _PRIORITY_MAP = {
        IssuePriority.HIGHEST: 1,
        IssuePriority.HIGH: 2,
        IssuePriority.MEDIUM: 3,
        IssuePriority.LOW: 4,
        IssuePriority.LOWEST: 4,
    }

    # Reverse priority map
    _PRIORITY_REVERSE_MAP = {
        1: IssuePriority.HIGHEST,
        2: IssuePriority.HIGH,
        3: IssuePriority.MEDIUM,
        4: IssuePriority.LOW,
    }

    # Map Azure DevOps state to IssueStatus
    _STATUS_MAP = {
        "new": IssueStatus.OPEN,
        "active": IssueStatus.IN_PROGRESS,
        "resolved": IssueStatus.RESOLVED,
        "closed": IssueStatus.CLOSED,
        "removed": IssueStatus.CLOSED,
        "to do": IssueStatus.OPEN,
        "doing": IssueStatus.IN_PROGRESS,
        "done": IssueStatus.RESOLVED,
    }

    # Reverse status map (for transitions)
    _STATUS_REVERSE_MAP = {
        IssueStatus.OPEN: "New",
        IssueStatus.IN_PROGRESS: "Active",
        IssueStatus.RESOLVED: "Resolved",
        IssueStatus.CLOSED: "Closed",
        IssueStatus.REOPENED: "Active",
    }

    @property
    def name(self) -> str:
        return "azure_boards"

    @property
    def display_name(self) -> str:
        return "Azure Boards"

    @property
    def _api_base(self) -> str:
        """Base URL for API calls."""
        org = self.config.organization
        if self.config.base_url:
            # On-premise Azure DevOps Server
            base = self.config.base_url.rstrip("/")
            return f"{base}/{org}"
        else:
            # Azure DevOps Services (cloud)
            return f"https://dev.azure.com/{org}"

    @property
    def _auth_header(self) -> str:
        """Generate authorization header."""
        # Azure DevOps uses PAT with Basic auth (empty username)
        credentials = f":{self.config.api_token}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"

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
        url = f"{self._api_base}{endpoint}"

        # Add API version
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
        """Test connection to Azure Boards."""
        result = self._request("GET", f"/{self.config.project_key}/_apis/projects")
        if result:
            return True, f"Connected to Azure DevOps organization: {self.config.organization}"

        # Try getting specific project
        result = self._request(
            "GET", f"/_apis/projects/{urllib.parse.quote(self.config.project_key)}"
        )
        if result:
            project_name = result.get("name", self.config.project_key)
            return True, f"Connected to project: {project_name}"

        return False, "Failed to connect to Azure DevOps. Check PAT and organization/project."

    def create_issue(self, request: CreateIssueRequest) -> Optional[TrackerIssue]:
        """Create a new work item in Azure Boards."""
        # Build JSON Patch document
        operations = [
            {"op": "add", "path": "/fields/System.Title", "value": request.title},
            {
                "op": "add",
                "path": "/fields/System.Description",
                "value": self._convert_to_html(request.description),
            },
        ]

        # Add work item type
        work_item_type = request.issue_type or self.config.default_issue_type or "Bug"

        # Add priority
        priority = self._PRIORITY_MAP.get(request.priority, 3)
        operations.append(
            {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority}
        )

        # Add tags (Azure uses semicolon-separated string)
        all_tags = list(request.labels) + self.config.default_labels + ["codescope"]
        if request.codescope_issue_ids:
            # Add CodeScope issue IDs as tags
            for issue_id in request.codescope_issue_ids[:5]:  # Limit to avoid tag overflow
                all_tags.append(f"codescope-id:{issue_id[:8]}")

        if all_tags:
            operations.append(
                {"op": "add", "path": "/fields/System.Tags", "value": "; ".join(all_tags)}
            )

        # Add assignee
        if request.assignee:
            operations.append(
                {"op": "add", "path": "/fields/System.AssignedTo", "value": request.assignee}
            )

        # Add custom fields
        for field_path, value in request.custom_fields.items():
            if not field_path.startswith("/fields/"):
                field_path = f"/fields/{field_path}"
            operations.append({"op": "add", "path": field_path, "value": value})

        # Create work item
        endpoint = f"/{self.config.project_key}/_apis/wit/workitems/${urllib.parse.quote(work_item_type)}"
        result = self._request(
            "POST",
            endpoint,
            data=operations,
            content_type="application/json-patch+json",
        )

        if result and "id" in result:
            return self._parse_work_item(result)
        return None

    def get_issue(self, issue_key: str) -> Optional[TrackerIssue]:
        """Get a work item by its ID."""
        # Azure Boards uses numeric IDs
        try:
            work_item_id = int(issue_key)
        except ValueError:
            # Try extracting ID from key like "AB#123"
            if "#" in issue_key:
                try:
                    work_item_id = int(issue_key.split("#")[1])
                except ValueError:
                    return None
            else:
                return None

        result = self._request(
            "GET",
            f"/{self.config.project_key}/_apis/wit/workitems/{work_item_id}",
            params={"$expand": "all"},
        )

        if not result:
            return None
        return self._parse_work_item(result)

    def update_issue(
        self, issue_key: str, request: UpdateIssueRequest
    ) -> Optional[TrackerIssue]:
        """Update an existing work item."""
        try:
            work_item_id = int(issue_key)
        except ValueError:
            if "#" in issue_key:
                work_item_id = int(issue_key.split("#")[1])
            else:
                return None

        operations = []

        if request.title:
            operations.append(
                {"op": "replace", "path": "/fields/System.Title", "value": request.title}
            )

        if request.description:
            operations.append(
                {
                    "op": "replace",
                    "path": "/fields/System.Description",
                    "value": self._convert_to_html(request.description),
                }
            )

        if request.priority:
            priority = self._PRIORITY_MAP.get(request.priority, 3)
            operations.append(
                {"op": "replace", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": priority}
            )

        if request.status:
            state = self._STATUS_REVERSE_MAP.get(request.status, "Active")
            operations.append(
                {"op": "replace", "path": "/fields/System.State", "value": state}
            )

        if request.assignee:
            operations.append(
                {"op": "replace", "path": "/fields/System.AssignedTo", "value": request.assignee}
            )

        if request.labels is not None:
            tags = "; ".join(request.labels)
            operations.append(
                {"op": "replace", "path": "/fields/System.Tags", "value": tags}
            )

        if request.custom_fields:
            for field_path, value in request.custom_fields.items():
                if not field_path.startswith("/fields/"):
                    field_path = f"/fields/{field_path}"
                operations.append({"op": "replace", "path": field_path, "value": value})

        if not operations:
            return self.get_issue(issue_key)

        result = self._request(
            "PATCH",
            f"/{self.config.project_key}/_apis/wit/workitems/{work_item_id}",
            data=operations,
            content_type="application/json-patch+json",
        )

        if result:
            return self._parse_work_item(result)
        return None

    def add_comment(self, issue_key: str, comment: str) -> bool:
        """Add a comment to a work item."""
        try:
            work_item_id = int(issue_key)
        except ValueError:
            if "#" in issue_key:
                work_item_id = int(issue_key.split("#")[1])
            else:
                return False

        result = self._request(
            "POST",
            f"/{self.config.project_key}/_apis/wit/workitems/{work_item_id}/comments",
            data={"text": self._convert_to_html(comment)},
            api_version="7.1-preview.3",
        )
        return result is not None

    def search_issues(
        self,
        query: Optional[str] = None,
        status: Optional[IssueStatus] = None,
        labels: Optional[list[str]] = None,
        max_results: int = 50,
    ) -> list[TrackerIssue]:
        """Search for work items using WIQL."""
        conditions = [
            f"[System.TeamProject] = '{self.config.project_key}'",
            "[System.Tags] CONTAINS 'codescope'",
        ]

        if query:
            conditions.append(f"[System.Title] CONTAINS '{query}'")

        if status:
            state = self._STATUS_REVERSE_MAP.get(status, "Active")
            conditions.append(f"[System.State] = '{state}'")

        if labels:
            for label in labels:
                conditions.append(f"[System.Tags] CONTAINS '{label}'")

        wiql = f"SELECT [System.Id] FROM WorkItems WHERE {' AND '.join(conditions)} ORDER BY [System.ChangedDate] DESC"

        result = self._request(
            "POST",
            f"/{self.config.project_key}/_apis/wit/wiql",
            data={"query": wiql},
            params={"$top": str(max_results)},
        )

        if not result or "workItems" not in result:
            return []

        work_item_ids = [wi["id"] for wi in result["workItems"][:max_results]]
        if not work_item_ids:
            return []

        # Fetch work item details in batch
        ids_param = ",".join(str(i) for i in work_item_ids)
        details = self._request(
            "GET",
            f"/{self.config.project_key}/_apis/wit/workitems",
            params={"ids": ids_param, "$expand": "all"},
        )

        if not details or "value" not in details:
            return []

        return [self._parse_work_item(wi) for wi in details["value"]]

    def get_available_issue_types(self) -> list[str]:
        """Get available work item types for the project."""
        result = self._request(
            "GET",
            f"/{self.config.project_key}/_apis/wit/workitemtypes",
        )

        if not result or "value" not in result:
            return ["Bug", "Task", "User Story", "Issue"]

        return [wit["name"] for wit in result["value"]]

    def get_available_priorities(self) -> list[str]:
        """Get available priority levels."""
        # Azure DevOps uses numeric priorities 1-4
        return ["1 - Critical", "2 - High", "3 - Medium", "4 - Low"]

    def get_project_users(self) -> list[dict[str, str]]:
        """Get users that can be assigned to work items."""
        # Get project team members
        result = self._request(
            "GET",
            f"/_apis/projects/{urllib.parse.quote(self.config.project_key)}/teams",
        )

        if not result or "value" not in result:
            return []

        users = []
        for team in result["value"][:5]:  # Limit to first 5 teams
            team_id = team["id"]
            members = self._request(
                "GET",
                f"/_apis/projects/{urllib.parse.quote(self.config.project_key)}/teams/{team_id}/members",
            )
            if members and "value" in members:
                for member in members["value"]:
                    identity = member.get("identity", {})
                    user = {
                        "id": identity.get("id", ""),
                        "name": identity.get("displayName", ""),
                        "email": identity.get("uniqueName", ""),
                    }
                    if user["id"] and user not in users:
                        users.append(user)

        return users

    def _parse_work_item(self, data: dict) -> TrackerIssue:
        """Parse Azure DevOps work item response into TrackerIssue."""
        fields = data.get("fields", {})

        # Parse status
        state = fields.get("System.State", "New").lower()
        status = self._STATUS_MAP.get(state, IssueStatus.OPEN)

        # Parse priority
        priority_val = fields.get("Microsoft.VSTS.Common.Priority", 3)
        priority = self._PRIORITY_REVERSE_MAP.get(priority_val, IssuePriority.MEDIUM)

        # Parse description (HTML to text)
        description = fields.get("System.Description", "")
        if description:
            # Simple HTML strip
            import re
            description = re.sub(r"<[^>]+>", "", description)
            description = description.replace("&nbsp;", " ").replace("&amp;", "&")

        # Parse dates
        created_at = None
        updated_at = None
        if fields.get("System.CreatedDate"):
            try:
                created_at = datetime.fromisoformat(
                    fields["System.CreatedDate"].replace("Z", "+00:00")
                )
            except ValueError:
                pass
        if fields.get("System.ChangedDate"):
            try:
                updated_at = datetime.fromisoformat(
                    fields["System.ChangedDate"].replace("Z", "+00:00")
                )
            except ValueError:
                pass

        # Build work item URL
        work_item_id = data.get("id", "")
        org = self.config.organization
        project = self.config.project_key
        if self.config.base_url:
            url = f"{self.config.base_url}/{org}/{project}/_workitems/edit/{work_item_id}"
        else:
            url = f"https://dev.azure.com/{org}/{project}/_workitems/edit/{work_item_id}"

        # Parse tags
        tags_str = fields.get("System.Tags", "")
        labels = [t.strip() for t in tags_str.split(";") if t.strip()]

        # Extract CodeScope issue IDs from tags
        codescope_ids = []
        for label in labels:
            if label.startswith("codescope-id:"):
                codescope_ids.append(label.split(":")[1])

        # Get assignee
        assignee = ""
        assigned_to = fields.get("System.AssignedTo")
        if isinstance(assigned_to, dict):
            assignee = assigned_to.get("displayName", "")
        elif isinstance(assigned_to, str):
            assignee = assigned_to

        # Get reporter
        reporter = ""
        created_by = fields.get("System.CreatedBy")
        if isinstance(created_by, dict):
            reporter = created_by.get("displayName", "")
        elif isinstance(created_by, str):
            reporter = created_by

        return TrackerIssue(
            id=str(work_item_id),
            key=f"AB#{work_item_id}",
            title=fields.get("System.Title", ""),
            description=description,
            status=status,
            priority=priority,
            issue_type=fields.get("System.WorkItemType", "Bug"),
            assignee=assignee,
            reporter=reporter,
            labels=labels,
            created_at=created_at,
            updated_at=updated_at,
            url=url,
            codescope_issue_ids=codescope_ids,
        )

    def _convert_to_html(self, text: str) -> str:
        """Convert markdown-like text to simple HTML for Azure DevOps."""
        # Basic conversions
        html = text.replace("\n", "<br>")
        html = html.replace("**", "<strong>").replace("**", "</strong>")
        html = html.replace("*", "<em>").replace("*", "</em>")
        html = html.replace("`", "<code>").replace("`", "</code>")

        # Convert links [text](url) to <a href="url">text</a>
        import re
        html = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', html)

        return f"<div>{html}</div>"

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
        """Format description for Azure DevOps (HTML)."""
        html_parts = [
            "<div>",
            "<h3>CodeScope Issue</h3>",
            "<table>",
            f"<tr><td><strong>Rule:</strong></td><td>{rule_id}</td></tr>",
            f"<tr><td><strong>Severity:</strong></td><td>{severity}</td></tr>",
            f"<tr><td><strong>File:</strong></td><td><code>{file_path}</code></td></tr>",
            f"<tr><td><strong>Line:</strong></td><td>{line}</td></tr>",
            "</table>",
            "<h4>Description:</h4>",
            f"<p>{message}</p>",
            "<hr>",
            f"<p><em>Issue ID: {issue_id}</em></p>",
        ]

        if dashboard_url:
            html_parts.append(f'<p><a href="{dashboard_url}">View in CodeScope Dashboard</a></p>')

        html_parts.append("</div>")
        return "".join(html_parts)
