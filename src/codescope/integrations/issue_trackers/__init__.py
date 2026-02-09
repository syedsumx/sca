"""Issue tracker integrations (Jira, Azure Boards, GitHub Issues).

This module provides integrations with issue tracking systems for
creating and managing issues from CodeScope analysis results.

Supported Providers:
    - Jira (Cloud and Server)
    - Azure Boards (Azure DevOps)
    - GitHub Issues

Usage:
    from codescope.integrations.issue_trackers import (
        get_issue_tracker,
        get_configured_trackers,
    )

    # Get a specific tracker
    github = get_issue_tracker("github")
    if github:
        issue = github.create_issue(request)

    # Get all configured trackers
    for tracker in get_configured_trackers():
        print(f"Connected to {tracker.display_name}")

Environment Variables:
    Jira:
        - CODESCOPE_JIRA_URL: Jira base URL
        - CODESCOPE_JIRA_TOKEN: API token
        - CODESCOPE_JIRA_PROJECT: Project key
        - CODESCOPE_JIRA_USERNAME: Username (for basic auth)

    Azure Boards:
        - CODESCOPE_AZURE_BOARDS_URL: Base URL (optional for cloud)
        - CODESCOPE_AZURE_BOARDS_TOKEN: Personal Access Token
        - CODESCOPE_AZURE_BOARDS_ORGANIZATION: Organization name
        - CODESCOPE_AZURE_BOARDS_PROJECT: Project name

    GitHub Issues:
        - CODESCOPE_GITHUB_URL: Base URL (optional, for Enterprise)
        - CODESCOPE_GITHUB_TOKEN: Personal Access Token
        - CODESCOPE_GITHUB_PROJECT: Repository (owner/repo format)
"""

from codescope.integrations.issue_trackers.base import (
    CreateIssueRequest,
    IssuePriority,
    IssueStatus,
    IssueTrackerProvider,
    TrackerConfig,
    TrackerIssue,
    UpdateIssueRequest,
)
from codescope.integrations.issue_trackers.azure_boards import AzureBoardsClient
from codescope.integrations.issue_trackers.github import (
    GitHubIssuesClient,
    GitHubPullRequestClient,
)
from codescope.integrations.issue_trackers.jira import JiraClient
from codescope.integrations.issue_trackers.registry import (
    create_tracker_from_config,
    get_available_providers,
    get_configured_trackers,
    get_issue_tracker,
    register_tracker,
)

__all__ = [
    # Base classes and models
    "CreateIssueRequest",
    "IssuePriority",
    "IssueStatus",
    "IssueTrackerProvider",
    "TrackerConfig",
    "TrackerIssue",
    "UpdateIssueRequest",
    # Providers
    "AzureBoardsClient",
    "GitHubIssuesClient",
    "GitHubPullRequestClient",
    "JiraClient",
    # Registry functions
    "create_tracker_from_config",
    "get_available_providers",
    "get_configured_trackers",
    "get_issue_tracker",
    "register_tracker",
]
