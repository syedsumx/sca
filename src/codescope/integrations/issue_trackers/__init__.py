"""Issue tracker integrations (Jira, Azure Boards)."""

from codescope.integrations.issue_trackers.base import (
    IssueTrackerProvider,
    TrackerIssue,
    TrackerConfig,
    IssuePriority,
    IssueStatus,
)
from codescope.integrations.issue_trackers.jira import JiraClient
from codescope.integrations.issue_trackers.azure_boards import AzureBoardsClient
from codescope.integrations.issue_trackers.registry import (
    get_issue_tracker,
    get_configured_trackers,
    register_tracker,
    get_available_providers,
    create_tracker_from_config,
)

__all__ = [
    "IssueTrackerProvider",
    "TrackerIssue",
    "TrackerConfig",
    "IssuePriority",
    "IssueStatus",
    "JiraClient",
    "AzureBoardsClient",
    "get_issue_tracker",
    "get_configured_trackers",
    "register_tracker",
    "get_available_providers",
    "create_tracker_from_config",
]
