"""External integrations for CodeScope."""

from codescope.integrations.issue_trackers import (
    IssueTrackerProvider,
    JiraClient,
    AzureBoardsClient,
    TrackerIssue,
    TrackerConfig,
    IssuePriority,
    IssueStatus,
    get_issue_tracker,
    get_configured_trackers,
    get_available_providers,
    create_tracker_from_config,
)

__all__ = [
    "IssueTrackerProvider",
    "JiraClient",
    "AzureBoardsClient",
    "TrackerIssue",
    "TrackerConfig",
    "IssuePriority",
    "IssueStatus",
    "get_issue_tracker",
    "get_configured_trackers",
    "get_available_providers",
    "create_tracker_from_config",
]
