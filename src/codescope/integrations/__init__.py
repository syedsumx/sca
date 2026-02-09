"""External integrations for CodeScope.

This module provides integrations with external services:

Issue Trackers:
    - Jira (Cloud and Server)
    - Azure Boards (Azure DevOps)
    - GitHub Issues

Repository Providers:
    - GitHub (github.com and GitHub Enterprise)
    - Azure DevOps Repos

Usage:
    # Issue Trackers
    from codescope.integrations import get_issue_tracker

    github_issues = get_issue_tracker("github")
    jira = get_issue_tracker("jira")
    azure_boards = get_issue_tracker("azure_boards")

    # Repository Providers
    from codescope.integrations.repos import (
        get_github_repos_client,
        get_azure_repos_client,
    )

    github_repos = get_github_repos_client()
    azure_repos = get_azure_repos_client()
"""

from codescope.integrations.issue_trackers import (
    AzureBoardsClient,
    CreateIssueRequest,
    GitHubIssuesClient,
    GitHubPullRequestClient,
    IssuePriority,
    IssueStatus,
    IssueTrackerProvider,
    JiraClient,
    TrackerConfig,
    TrackerIssue,
    UpdateIssueRequest,
    create_tracker_from_config,
    get_available_providers,
    get_configured_trackers,
    get_issue_tracker,
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
    # Issue Tracker Providers
    "AzureBoardsClient",
    "GitHubIssuesClient",
    "GitHubPullRequestClient",
    "JiraClient",
    # Registry functions
    "create_tracker_from_config",
    "get_available_providers",
    "get_configured_trackers",
    "get_issue_tracker",
]
