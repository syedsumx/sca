"""Base classes and models for issue tracker integrations."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class IssuePriority(str, Enum):
    """Standard issue priority levels."""

    HIGHEST = "highest"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    LOWEST = "lowest"


class IssueStatus(str, Enum):
    """Standard issue status values."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    REOPENED = "reopened"


@dataclass
class TrackerConfig:
    """Configuration for an issue tracker connection."""

    provider: str  # jira, azure_boards
    base_url: str
    project_key: str
    api_token: str = ""
    username: str = ""  # For Jira basic auth
    organization: str = ""  # For Azure DevOps
    default_issue_type: str = "Bug"
    default_labels: list[str] = field(default_factory=list)
    custom_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary (masks sensitive data)."""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "project_key": self.project_key,
            "username": self.username,
            "organization": self.organization,
            "default_issue_type": self.default_issue_type,
            "default_labels": self.default_labels,
            "api_token": "***" if self.api_token else "",
        }


@dataclass
class TrackerIssue:
    """Represents an issue in an external tracker."""

    id: str
    key: str  # e.g., "PROJ-123" for Jira, work item ID for Azure
    title: str
    description: str = ""
    status: IssueStatus = IssueStatus.OPEN
    priority: IssuePriority = IssuePriority.MEDIUM
    issue_type: str = "Bug"
    assignee: str = ""
    reporter: str = ""
    labels: list[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    url: str = ""
    codescope_issue_ids: list[str] = field(default_factory=list)
    custom_fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "id": self.id,
            "key": self.key,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority.value,
            "issue_type": self.issue_type,
            "assignee": self.assignee,
            "reporter": self.reporter,
            "labels": self.labels,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "url": self.url,
            "codescope_issue_ids": self.codescope_issue_ids,
            "custom_fields": self.custom_fields,
        }


@dataclass
class CreateIssueRequest:
    """Request to create a new issue."""

    title: str
    description: str = ""
    priority: IssuePriority = IssuePriority.MEDIUM
    issue_type: str = "Bug"
    assignee: str = ""
    labels: list[str] = field(default_factory=list)
    codescope_issue_ids: list[str] = field(default_factory=list)
    custom_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class UpdateIssueRequest:
    """Request to update an existing issue."""

    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[IssueStatus] = None
    priority: Optional[IssuePriority] = None
    assignee: Optional[str] = None
    labels: Optional[list[str]] = None
    custom_fields: Optional[dict[str, Any]] = None


class IssueTrackerProvider(ABC):
    """Base class for issue tracker providers."""

    def __init__(self, config: TrackerConfig) -> None:
        self.config = config

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g., 'jira', 'azure_boards')."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable provider name."""
        ...

    @abstractmethod
    def test_connection(self) -> tuple[bool, str]:
        """Test connection to the issue tracker.

        Returns:
            Tuple of (success, message)
        """
        ...

    @abstractmethod
    def create_issue(self, request: CreateIssueRequest) -> Optional[TrackerIssue]:
        """Create a new issue.

        Args:
            request: Issue creation request

        Returns:
            Created issue or None on failure
        """
        ...

    @abstractmethod
    def get_issue(self, issue_key: str) -> Optional[TrackerIssue]:
        """Get an issue by its key.

        Args:
            issue_key: Issue key (e.g., "PROJ-123")

        Returns:
            Issue or None if not found
        """
        ...

    @abstractmethod
    def update_issue(
        self, issue_key: str, request: UpdateIssueRequest
    ) -> Optional[TrackerIssue]:
        """Update an existing issue.

        Args:
            issue_key: Issue key
            request: Update request

        Returns:
            Updated issue or None on failure
        """
        ...

    @abstractmethod
    def add_comment(self, issue_key: str, comment: str) -> bool:
        """Add a comment to an issue.

        Args:
            issue_key: Issue key
            comment: Comment text

        Returns:
            True on success
        """
        ...

    @abstractmethod
    def search_issues(
        self,
        query: Optional[str] = None,
        status: Optional[IssueStatus] = None,
        labels: Optional[list[str]] = None,
        max_results: int = 50,
    ) -> list[TrackerIssue]:
        """Search for issues.

        Args:
            query: Search query string
            status: Filter by status
            labels: Filter by labels
            max_results: Maximum results to return

        Returns:
            List of matching issues
        """
        ...

    @abstractmethod
    def get_available_issue_types(self) -> list[str]:
        """Get available issue types for the project.

        Returns:
            List of issue type names
        """
        ...

    @abstractmethod
    def get_available_priorities(self) -> list[str]:
        """Get available priority levels.

        Returns:
            List of priority names
        """
        ...

    @abstractmethod
    def get_project_users(self) -> list[dict[str, str]]:
        """Get users that can be assigned to issues.

        Returns:
            List of user dicts with 'id', 'name', 'email'
        """
        ...

    def format_codescope_issue(
        self,
        issue_id: str,
        rule_id: str,
        message: str,
        file_path: str,
        line: int,
        severity: str,
        dashboard_url: str = "",
    ) -> CreateIssueRequest:
        """Format a CodeScope issue for creation in the tracker.

        Args:
            issue_id: CodeScope issue UUID
            rule_id: Rule ID (e.g., "python:S3776")
            message: Issue message
            file_path: File path
            line: Line number
            severity: Severity level
            dashboard_url: URL to CodeScope dashboard

        Returns:
            CreateIssueRequest for the tracker
        """
        title = f"[CodeScope] {rule_id}: {message[:80]}"
        if len(message) > 80:
            title += "..."

        description = self._format_description(
            issue_id, rule_id, message, file_path, line, severity, dashboard_url
        )

        priority = self._map_severity_to_priority(severity)

        return CreateIssueRequest(
            title=title,
            description=description,
            priority=priority,
            issue_type=self.config.default_issue_type,
            labels=self.config.default_labels + ["codescope", f"severity:{severity.lower()}"],
            codescope_issue_ids=[issue_id],
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
        """Format issue description."""
        lines = [
            f"**CodeScope Issue**",
            "",
            f"**Rule:** {rule_id}",
            f"**Severity:** {severity}",
            f"**File:** `{file_path}`",
            f"**Line:** {line}",
            "",
            "**Description:**",
            message,
            "",
            "---",
            f"*Issue ID: {issue_id}*",
        ]
        if dashboard_url:
            lines.append(f"[View in CodeScope Dashboard]({dashboard_url})")

        return "\n".join(lines)

    def _map_severity_to_priority(self, severity: str) -> IssuePriority:
        """Map CodeScope severity to issue priority."""
        severity_map = {
            "BLOCKER": IssuePriority.HIGHEST,
            "CRITICAL": IssuePriority.HIGHEST,
            "MAJOR": IssuePriority.HIGH,
            "MINOR": IssuePriority.MEDIUM,
            "INFO": IssuePriority.LOW,
        }
        return severity_map.get(severity.upper(), IssuePriority.MEDIUM)
