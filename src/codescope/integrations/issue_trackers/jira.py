"""Jira issue tracker integration."""

from __future__ import annotations

import base64
import json
import logging
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


class JiraClient(IssueTrackerProvider):
    """Jira Cloud/Server integration client."""

    # Map CodeScope priority to Jira priority names
    _PRIORITY_MAP = {
        IssuePriority.HIGHEST: "Highest",
        IssuePriority.HIGH: "High",
        IssuePriority.MEDIUM: "Medium",
        IssuePriority.LOW: "Low",
        IssuePriority.LOWEST: "Lowest",
    }

    # Map Jira status to IssueStatus
    _STATUS_MAP = {
        "to do": IssueStatus.OPEN,
        "open": IssueStatus.OPEN,
        "new": IssueStatus.OPEN,
        "in progress": IssueStatus.IN_PROGRESS,
        "in review": IssueStatus.IN_PROGRESS,
        "done": IssueStatus.RESOLVED,
        "resolved": IssueStatus.RESOLVED,
        "closed": IssueStatus.CLOSED,
        "reopened": IssueStatus.REOPENED,
    }

    @property
    def name(self) -> str:
        return "jira"

    @property
    def display_name(self) -> str:
        return "Jira"

    @property
    def _api_base(self) -> str:
        """Base URL for API calls."""
        base = self.config.base_url.rstrip("/")
        # Jira Cloud uses /rest/api/3, Server uses /rest/api/2
        if "atlassian.net" in base:
            return f"{base}/rest/api/3"
        return f"{base}/rest/api/2"

    @property
    def _auth_header(self) -> str:
        """Generate authorization header."""
        if self.config.username and self.config.api_token:
            # Basic auth for Jira Cloud (email:api_token)
            credentials = f"{self.config.username}:{self.config.api_token}"
            encoded = base64.b64encode(credentials.encode()).decode()
            return f"Basic {encoded}"
        elif self.config.api_token:
            # Bearer token for Jira Server with PAT
            return f"Bearer {self.config.api_token}"
        return ""

    def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Optional[dict]:
        """Make an HTTP request to the Jira API."""
        url = f"{self._api_base}{endpoint}"

        if params:
            query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{query}"

        headers = {
            "Authorization": self._auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        body = json.dumps(data).encode() if data else None

        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 204:
                    return {}
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else ""
            logger.error("Jira API error %d: %s - %s", e.code, e.reason, error_body)
            return None
        except Exception as exc:
            logger.error("Jira API request failed: %s", exc)
            return None

    def test_connection(self) -> tuple[bool, str]:
        """Test connection to Jira."""
        result = self._request("GET", f"/project/{self.config.project_key}")
        if result:
            project_name = result.get("name", self.config.project_key)
            return True, f"Connected to project: {project_name}"
        return False, "Failed to connect to Jira. Check credentials and project key."

    def create_issue(self, request: CreateIssueRequest) -> Optional[TrackerIssue]:
        """Create a new Jira issue."""
        # Build fields
        fields: dict[str, Any] = {
            "project": {"key": self.config.project_key},
            "summary": request.title,
            "issuetype": {"name": request.issue_type},
        }

        # Add description (Jira Cloud uses ADF, Server uses plain text)
        if "atlassian.net" in self.config.base_url:
            # Atlassian Document Format for Cloud
            fields["description"] = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": request.description}],
                    }
                ],
            }
        else:
            fields["description"] = request.description

        # Add priority
        jira_priority = self._PRIORITY_MAP.get(request.priority, "Medium")
        fields["priority"] = {"name": jira_priority}

        # Add labels
        if request.labels:
            # Jira labels cannot contain spaces
            fields["labels"] = [label.replace(" ", "_") for label in request.labels]

        # Add assignee
        if request.assignee:
            fields["assignee"] = {"accountId": request.assignee}

        # Add custom fields
        for field_id, value in request.custom_fields.items():
            fields[field_id] = value

        # Store CodeScope issue IDs in a custom field or description
        if request.codescope_issue_ids:
            # Append to description
            issue_ids_text = "\n\nCodeScope Issue IDs: " + ", ".join(request.codescope_issue_ids)
            if isinstance(fields["description"], dict):
                fields["description"]["content"].append(
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": issue_ids_text}],
                    }
                )
            else:
                fields["description"] += issue_ids_text

        result = self._request("POST", "/issue", {"fields": fields})
        if result and "key" in result:
            return self.get_issue(result["key"])
        return None

    def get_issue(self, issue_key: str) -> Optional[TrackerIssue]:
        """Get an issue by its key."""
        result = self._request("GET", f"/issue/{issue_key}")
        if not result:
            return None
        return self._parse_issue(result)

    def update_issue(
        self, issue_key: str, request: UpdateIssueRequest
    ) -> Optional[TrackerIssue]:
        """Update an existing issue."""
        fields: dict[str, Any] = {}

        if request.title:
            fields["summary"] = request.title

        if request.description:
            if "atlassian.net" in self.config.base_url:
                fields["description"] = {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": request.description}],
                        }
                    ],
                }
            else:
                fields["description"] = request.description

        if request.priority:
            jira_priority = self._PRIORITY_MAP.get(request.priority, "Medium")
            fields["priority"] = {"name": jira_priority}

        if request.assignee:
            fields["assignee"] = {"accountId": request.assignee}

        if request.labels is not None:
            fields["labels"] = [label.replace(" ", "_") for label in request.labels]

        if request.custom_fields:
            fields.update(request.custom_fields)

        if fields:
            result = self._request("PUT", f"/issue/{issue_key}", {"fields": fields})
            if result is None:
                return None

        # Handle status transition separately
        if request.status:
            self._transition_issue(issue_key, request.status)

        return self.get_issue(issue_key)

    def _transition_issue(self, issue_key: str, target_status: IssueStatus) -> bool:
        """Transition an issue to a new status."""
        # Get available transitions
        transitions = self._request("GET", f"/issue/{issue_key}/transitions")
        if not transitions:
            return False

        # Find matching transition
        target_name = target_status.value.replace("_", " ").lower()
        for t in transitions.get("transitions", []):
            if t.get("to", {}).get("name", "").lower() == target_name:
                result = self._request(
                    "POST",
                    f"/issue/{issue_key}/transitions",
                    {"transition": {"id": t["id"]}},
                )
                return result is not None

        logger.warning("No transition found for status: %s", target_status)
        return False

    def add_comment(self, issue_key: str, comment: str) -> bool:
        """Add a comment to an issue."""
        if "atlassian.net" in self.config.base_url:
            # ADF format for Cloud
            body = {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": comment}],
                        }
                    ],
                }
            }
        else:
            body = {"body": comment}

        result = self._request("POST", f"/issue/{issue_key}/comment", body)
        return result is not None

    def search_issues(
        self,
        query: Optional[str] = None,
        status: Optional[IssueStatus] = None,
        labels: Optional[list[str]] = None,
        max_results: int = 50,
    ) -> list[TrackerIssue]:
        """Search for issues using JQL."""
        jql_parts = [f"project = {self.config.project_key}"]

        if query:
            jql_parts.append(f'text ~ "{query}"')

        if status:
            status_name = status.value.replace("_", " ").title()
            jql_parts.append(f'status = "{status_name}"')

        if labels:
            label_jql = " AND ".join(f'labels = "{label}"' for label in labels)
            jql_parts.append(f"({label_jql})")

        # Add codescope label by default to find linked issues
        jql_parts.append('labels = "codescope"')

        jql = " AND ".join(jql_parts)

        params = {
            "jql": jql,
            "maxResults": max_results,
            "fields": "summary,description,status,priority,assignee,reporter,labels,created,updated,issuetype",
        }

        result = self._request("GET", "/search", params=params)
        if not result:
            return []

        return [self._parse_issue(issue) for issue in result.get("issues", [])]

    def get_available_issue_types(self) -> list[str]:
        """Get available issue types for the project."""
        result = self._request("GET", f"/project/{self.config.project_key}")
        if not result:
            return ["Bug", "Task", "Story"]

        issue_types = result.get("issueTypes", [])
        return [it["name"] for it in issue_types if not it.get("subtask")]

    def get_available_priorities(self) -> list[str]:
        """Get available priority levels."""
        result = self._request("GET", "/priority")
        if not result:
            return ["Highest", "High", "Medium", "Low", "Lowest"]
        return [p["name"] for p in result]

    def get_project_users(self) -> list[dict[str, str]]:
        """Get users that can be assigned to issues."""
        params = {"project": self.config.project_key}
        result = self._request("GET", "/user/assignable/search", params=params)
        if not result:
            return []

        return [
            {
                "id": user.get("accountId", user.get("key", "")),
                "name": user.get("displayName", ""),
                "email": user.get("emailAddress", ""),
            }
            for user in result
        ]

    def _parse_issue(self, data: dict) -> TrackerIssue:
        """Parse Jira issue response into TrackerIssue."""
        fields = data.get("fields", {})

        # Parse status
        status_name = fields.get("status", {}).get("name", "").lower()
        status = self._STATUS_MAP.get(status_name, IssueStatus.OPEN)

        # Parse priority
        priority_name = fields.get("priority", {}).get("name", "Medium")
        priority = IssuePriority.MEDIUM
        for p, name in self._PRIORITY_MAP.items():
            if name.lower() == priority_name.lower():
                priority = p
                break

        # Parse description
        description = ""
        desc_field = fields.get("description")
        if isinstance(desc_field, dict):
            # ADF format - extract text
            description = self._extract_adf_text(desc_field)
        elif isinstance(desc_field, str):
            description = desc_field

        # Parse dates
        created_at = None
        updated_at = None
        if fields.get("created"):
            try:
                created_at = datetime.fromisoformat(fields["created"].replace("Z", "+00:00"))
            except ValueError:
                pass
        if fields.get("updated"):
            try:
                updated_at = datetime.fromisoformat(fields["updated"].replace("Z", "+00:00"))
            except ValueError:
                pass

        # Build issue URL
        base = self.config.base_url.rstrip("/")
        issue_key = data.get("key", "")
        url = f"{base}/browse/{issue_key}"

        # Extract CodeScope issue IDs from labels or description
        codescope_ids = []
        labels = fields.get("labels", [])
        for label in labels:
            if label.startswith("codescope-id:"):
                codescope_ids.append(label.split(":")[1])

        return TrackerIssue(
            id=data.get("id", ""),
            key=issue_key,
            title=fields.get("summary", ""),
            description=description,
            status=status,
            priority=priority,
            issue_type=fields.get("issuetype", {}).get("name", "Bug"),
            assignee=fields.get("assignee", {}).get("displayName", "") if fields.get("assignee") else "",
            reporter=fields.get("reporter", {}).get("displayName", "") if fields.get("reporter") else "",
            labels=labels,
            created_at=created_at,
            updated_at=updated_at,
            url=url,
            codescope_issue_ids=codescope_ids,
        )

    def _extract_adf_text(self, adf: dict) -> str:
        """Extract plain text from Atlassian Document Format."""
        text_parts = []

        def extract(node: dict) -> None:
            if node.get("type") == "text":
                text_parts.append(node.get("text", ""))
            for child in node.get("content", []):
                extract(child)

        extract(adf)
        return "".join(text_parts)

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
        """Format description for Jira (markdown-like)."""
        lines = [
            "*CodeScope Issue*",
            "",
            f"*Rule:* {rule_id}",
            f"*Severity:* {severity}",
            f"*File:* {{code}}{file_path}{{code}}",
            f"*Line:* {line}",
            "",
            "*Description:*",
            message,
            "",
            "----",
            f"_Issue ID: {issue_id}_",
        ]
        if dashboard_url:
            lines.append(f"[View in CodeScope Dashboard|{dashboard_url}]")

        return "\n".join(lines)
