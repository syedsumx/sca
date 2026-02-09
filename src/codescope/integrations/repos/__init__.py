"""Repository and pull request integrations.

This module provides integrations with version control providers for
repository management, pull request operations, and code review comments.

Supported Providers:
    - GitHub (github.com and GitHub Enterprise)
    - Azure DevOps Repos

Usage:
    from codescope.integrations.repos import (
        get_github_repos_client,
        get_azure_repos_client,
    )

    # GitHub
    github = get_github_repos_client()
    if github:
        prs = github.list_pull_requests()
        github.post_analysis_summary(pr_number=123, ...)

    # Azure DevOps
    azure = get_azure_repos_client()
    if azure:
        prs = azure.list_pull_requests()
        azure.post_inline_comment(pr_id=456, ...)

Environment Variables:
    GitHub:
        - CODESCOPE_GITHUB_OWNER: Repository owner
        - CODESCOPE_GITHUB_REPOSITORY: Repository name
        - CODESCOPE_GITHUB_TOKEN: Personal Access Token
        - CODESCOPE_GITHUB_URL: Base URL (optional, for GitHub Enterprise)

    Azure DevOps:
        - CODESCOPE_AZURE_REPOS_ORGANIZATION: Azure DevOps organization
        - CODESCOPE_AZURE_REPOS_PROJECT: Project name
        - CODESCOPE_AZURE_REPOS_REPOSITORY: Repository name
        - CODESCOPE_AZURE_REPOS_TOKEN: Personal Access Token
        - CODESCOPE_AZURE_REPOS_URL: Base URL (optional, for on-premise)
"""

from codescope.integrations.repos.azure_repos import (
    AzureReposClient,
    AzureReposConfig,
    CommentThreadStatus,
    PullRequest as AzurePullRequest,
    PullRequestStatus as AzurePullRequestStatus,
    Repository as AzureRepository,
    get_azure_repos_client,
)
from codescope.integrations.repos.github_repos import (
    GitHubReposClient,
    GitHubReposConfig,
    MergeMethod,
    PullRequest as GitHubPullRequest,
    PullRequestState,
    Repository as GitHubRepository,
    ReviewComment,
    get_github_repos_client,
)

__all__ = [
    # Azure DevOps Repos
    "AzureReposClient",
    "AzureReposConfig",
    "AzurePullRequest",
    "AzurePullRequestStatus",
    "AzureRepository",
    "CommentThreadStatus",
    "get_azure_repos_client",
    # GitHub Repos
    "GitHubReposClient",
    "GitHubReposConfig",
    "GitHubPullRequest",
    "GitHubRepository",
    "MergeMethod",
    "PullRequestState",
    "ReviewComment",
    "get_github_repos_client",
]
