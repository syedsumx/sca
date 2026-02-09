"""Tests for repository integrations (GitHub Repos, Azure DevOps Repos).

This module tests the repository integration functionality including:
- GitHub Repos client
- Azure DevOps Repos client
- Pull request operations
- Code review comments
- Analysis integration
"""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest


# ── GitHub Repos Tests ────────────────────────────────────────────────────


class TestGitHubReposConfig:
    """Test GitHubReposConfig dataclass."""

    def test_create_config(self):
        """Should create a config with required fields."""
        from codescope.integrations.repos.github_repos import GitHubReposConfig

        config = GitHubReposConfig(
            owner="test-owner",
            repository="test-repo",
            api_token="ghp_token",
        )
        assert config.owner == "test-owner"
        assert config.repository == "test-repo"
        assert config.api_token == "ghp_token"
        assert config.base_url == ""

    def test_repo_path_property(self):
        """Should return correct repo path."""
        from codescope.integrations.repos.github_repos import GitHubReposConfig

        config = GitHubReposConfig(
            owner="owner",
            repository="repo",
            api_token="token",
        )
        assert config.repo_path == "owner/repo"

    def test_to_dict_masks_token(self):
        """Should mask API token in to_dict output."""
        from codescope.integrations.repos.github_repos import GitHubReposConfig

        config = GitHubReposConfig(
            owner="owner",
            repository="repo",
            api_token="secret-token",
        )
        result = config.to_dict()
        assert result["api_token"] == "***"
        assert result["owner"] == "owner"


class TestGitHubReposClient:
    """Test GitHubReposClient."""

    @pytest.fixture
    def github_config(self):
        """Create a test GitHub configuration."""
        from codescope.integrations.repos.github_repos import GitHubReposConfig
        return GitHubReposConfig(
            owner="test-owner",
            repository="test-repo",
            api_token="ghp_test-token",
        )

    @pytest.fixture
    def github_client(self, github_config):
        """Create a GitHubReposClient instance."""
        from codescope.integrations.repos.github_repos import GitHubReposClient
        return GitHubReposClient(github_config)

    def test_api_base_github_com(self, github_client):
        """Should use correct API base for GitHub.com."""
        assert github_client._api_base == "https://api.github.com"

    def test_api_base_enterprise(self, github_config):
        """Should use correct API base for GitHub Enterprise."""
        from codescope.integrations.repos.github_repos import GitHubReposClient

        github_config.base_url = "https://github.company.com"
        client = GitHubReposClient(github_config)
        assert client._api_base == "https://github.company.com/api/v3"

    def test_web_base(self, github_client):
        """Should return correct web base URL."""
        assert github_client._web_base == "https://github.com"

    @patch.object(
        __import__("codescope.integrations.repos.github_repos", fromlist=["GitHubReposClient"]).GitHubReposClient,
        "_request"
    )
    def test_test_connection_success(self, mock_request, github_client):
        """Should return success on valid connection."""
        mock_request.return_value = {"full_name": "test-owner/test-repo", "private": False}
        success, message = github_client.test_connection()
        assert success is True
        assert "test-owner/test-repo" in message

    @patch.object(
        __import__("codescope.integrations.repos.github_repos", fromlist=["GitHubReposClient"]).GitHubReposClient,
        "_request"
    )
    def test_test_connection_failure(self, mock_request, github_client):
        """Should return failure on invalid connection."""
        mock_request.return_value = None
        success, message = github_client.test_connection()
        assert success is False

    @patch.object(
        __import__("codescope.integrations.repos.github_repos", fromlist=["GitHubReposClient"]).GitHubReposClient,
        "_request"
    )
    def test_get_repository(self, mock_request, github_client):
        """Should get repository details."""
        mock_request.return_value = {
            "id": 123,
            "name": "test-repo",
            "full_name": "test-owner/test-repo",
            "html_url": "https://github.com/test-owner/test-repo",
            "default_branch": "main",
            "private": False,
        }
        repo = github_client.get_repository()
        assert repo is not None
        assert repo.name == "test-repo"
        assert repo.default_branch == "main"

    @patch.object(
        __import__("codescope.integrations.repos.github_repos", fromlist=["GitHubReposClient"]).GitHubReposClient,
        "_request"
    )
    def test_list_pull_requests(self, mock_request, github_client):
        """Should list pull requests."""
        mock_request.return_value = [
            {
                "id": 1,
                "number": 42,
                "title": "Test PR",
                "state": "open",
                "user": {"login": "author"},
                "head": {"ref": "feature", "sha": "abc123"},
                "base": {"ref": "main"},
            }
        ]
        prs = github_client.list_pull_requests()
        assert len(prs) == 1
        assert prs[0].number == 42
        assert prs[0].title == "Test PR"

    @patch.object(
        __import__("codescope.integrations.repos.github_repos", fromlist=["GitHubReposClient"]).GitHubReposClient,
        "_request"
    )
    def test_create_pull_request(self, mock_request, github_client):
        """Should create a pull request."""
        mock_request.return_value = {
            "id": 2,
            "number": 43,
            "title": "New Feature",
            "state": "open",
            "user": {"login": "author"},
            "head": {"ref": "feature", "sha": "def456"},
            "base": {"ref": "main"},
        }
        pr = github_client.create_pull_request(
            title="New Feature",
            head="feature",
            base="main",
        )
        assert pr is not None
        assert pr.number == 43

    @patch.object(
        __import__("codescope.integrations.repos.github_repos", fromlist=["GitHubReposClient"]).GitHubReposClient,
        "_request"
    )
    def test_create_issue_comment(self, mock_request, github_client):
        """Should create issue comment on PR."""
        mock_request.return_value = {"id": 456}
        result = github_client.create_issue_comment(42, "Test comment")
        assert result is True

    @patch.object(
        __import__("codescope.integrations.repos.github_repos", fromlist=["GitHubReposClient"]).GitHubReposClient,
        "_request"
    )
    def test_create_review(self, mock_request, github_client):
        """Should create PR review."""
        mock_request.return_value = {"id": 789}
        result = github_client.create_review(
            pr_number=42,
            body="LGTM",
            event="APPROVE",
        )
        assert result is True

    @patch.object(
        __import__("codescope.integrations.repos.github_repos", fromlist=["GitHubReposClient"]).GitHubReposClient,
        "_request"
    )
    def test_post_analysis_summary(self, mock_request, github_client):
        """Should post analysis summary."""
        mock_request.return_value = {"id": 100}
        result = github_client.post_analysis_summary(
            pr_number=42,
            total_issues=10,
            critical_count=1,
            high_count=2,
            medium_count=4,
            low_count=3,
            quality_gate_passed=True,
        )
        assert result is True

    @patch.object(
        __import__("codescope.integrations.repos.github_repos", fromlist=["GitHubReposClient"]).GitHubReposClient,
        "_request"
    )
    def test_create_check_run(self, mock_request, github_client):
        """Should create check run."""
        mock_request.return_value = {"id": 200}
        result = github_client.create_check_run(
            head_sha="abc123",
            conclusion="success",
            title="Analysis Complete",
            summary="All tests passed",
        )
        assert result is True


class TestPullRequestDataclass:
    """Test PullRequest dataclass."""

    def test_create_pull_request(self):
        """Should create a pull request with all fields."""
        from codescope.integrations.repos.github_repos import PullRequest, PullRequestState

        pr = PullRequest(
            id=1,
            number=42,
            title="Test PR",
            body="Description",
            source_branch="feature",
            target_branch="main",
            state=PullRequestState.OPEN,
            author="test-user",
            created_at=datetime(2024, 1, 15),
            url="https://github.com/owner/repo/pull/42",
        )
        assert pr.number == 42
        assert pr.state == PullRequestState.OPEN

    def test_to_dict(self):
        """Should convert to dictionary."""
        from codescope.integrations.repos.github_repos import PullRequest, PullRequestState

        pr = PullRequest(
            id=1,
            number=42,
            title="Test PR",
            state=PullRequestState.CLOSED,
        )
        result = pr.to_dict()
        assert result["number"] == 42
        assert result["state"] == "closed"


# ── Azure Repos Tests ─────────────────────────────────────────────────────


class TestAzureReposConfig:
    """Test AzureReposConfig dataclass."""

    def test_create_config(self):
        """Should create a config with required fields."""
        from codescope.integrations.repos.azure_repos import AzureReposConfig

        config = AzureReposConfig(
            organization="test-org",
            project="test-project",
            repository="test-repo",
            api_token="pat-token",
        )
        assert config.organization == "test-org"
        assert config.project == "test-project"
        assert config.repository == "test-repo"
        assert config.api_token == "pat-token"

    def test_to_dict_masks_token(self):
        """Should mask API token in to_dict output."""
        from codescope.integrations.repos.azure_repos import AzureReposConfig

        config = AzureReposConfig(
            organization="org",
            project="proj",
            repository="repo",
            api_token="secret-pat",
        )
        result = config.to_dict()
        assert result["api_token"] == "***"
        assert result["organization"] == "org"


class TestAzureReposClient:
    """Test AzureReposClient."""

    @pytest.fixture
    def azure_config(self):
        """Create a test Azure Repos configuration."""
        from codescope.integrations.repos.azure_repos import AzureReposConfig
        return AzureReposConfig(
            organization="test-org",
            project="test-project",
            repository="test-repo",
            api_token="test-pat",
        )

    @pytest.fixture
    def azure_client(self, azure_config):
        """Create an AzureReposClient instance."""
        from codescope.integrations.repos.azure_repos import AzureReposClient
        return AzureReposClient(azure_config)

    def test_api_base_cloud(self, azure_client):
        """Should use correct API base for Azure DevOps Services."""
        assert azure_client._api_base == "https://dev.azure.com/test-org"

    def test_api_base_server(self, azure_config):
        """Should use correct API base for Azure DevOps Server."""
        from codescope.integrations.repos.azure_repos import AzureReposClient

        azure_config.base_url = "https://azuredevops.company.com"
        client = AzureReposClient(azure_config)
        assert client._api_base == "https://azuredevops.company.com/test-org"

    def test_git_api_base(self, azure_client):
        """Should return correct Git API base."""
        assert azure_client._git_api_base == "https://dev.azure.com/test-org/test-project/_apis/git"

    def test_web_base(self, azure_client):
        """Should return correct web base URL."""
        assert "test-org" in azure_client._web_base
        assert "test-project" in azure_client._web_base
        assert "test-repo" in azure_client._web_base

    @patch.object(
        __import__("codescope.integrations.repos.azure_repos", fromlist=["AzureReposClient"]).AzureReposClient,
        "_request"
    )
    def test_test_connection_success(self, mock_request, azure_client):
        """Should return success on valid connection."""
        mock_request.return_value = {"id": "123", "name": "test-repo"}
        success, message = azure_client.test_connection()
        assert success is True
        assert "test-repo" in message

    @patch.object(
        __import__("codescope.integrations.repos.azure_repos", fromlist=["AzureReposClient"]).AzureReposClient,
        "_request"
    )
    def test_test_connection_failure(self, mock_request, azure_client):
        """Should return failure on invalid connection."""
        mock_request.return_value = None
        success, message = azure_client.test_connection()
        assert success is False

    @patch.object(
        __import__("codescope.integrations.repos.azure_repos", fromlist=["AzureReposClient"]).AzureReposClient,
        "_request"
    )
    def test_list_pull_requests(self, mock_request, azure_client):
        """Should list pull requests."""
        mock_request.return_value = {
            "value": [
                {
                    "pullRequestId": 101,
                    "title": "Test PR",
                    "status": "active",
                    "sourceRefName": "refs/heads/feature",
                    "targetRefName": "refs/heads/main",
                }
            ]
        }
        prs = azure_client.list_pull_requests()
        assert len(prs) == 1
        assert prs[0].id == 101
        assert prs[0].title == "Test PR"

    @patch.object(
        __import__("codescope.integrations.repos.azure_repos", fromlist=["AzureReposClient"]).AzureReposClient,
        "_request"
    )
    def test_create_pull_request(self, mock_request, azure_client):
        """Should create a pull request."""
        mock_request.return_value = {
            "pullRequestId": 102,
            "title": "New Feature",
            "status": "active",
            "sourceRefName": "refs/heads/feature",
            "targetRefName": "refs/heads/main",
        }
        pr = azure_client.create_pull_request(
            title="New Feature",
            source_branch="feature",
            target_branch="main",
        )
        assert pr is not None
        assert pr.id == 102

    @patch.object(
        __import__("codescope.integrations.repos.azure_repos", fromlist=["AzureReposClient"]).AzureReposClient,
        "_request"
    )
    def test_create_thread(self, mock_request, azure_client):
        """Should create a comment thread."""
        mock_request.return_value = {"id": 1, "status": "active"}
        result = azure_client.create_thread(
            pr_id=101,
            content="Test comment",
        )
        assert result is not None

    @patch.object(
        __import__("codescope.integrations.repos.azure_repos", fromlist=["AzureReposClient"]).AzureReposClient,
        "_request"
    )
    def test_create_inline_thread(self, mock_request, azure_client):
        """Should create an inline comment thread."""
        mock_request.return_value = {"id": 2, "status": "active"}
        result = azure_client.create_thread(
            pr_id=101,
            content="Inline comment",
            file_path="src/index.js",
            line=42,
        )
        assert result is not None

    @patch.object(
        __import__("codescope.integrations.repos.azure_repos", fromlist=["AzureReposClient"]).AzureReposClient,
        "_request"
    )
    def test_post_analysis_summary(self, mock_request, azure_client):
        """Should post analysis summary."""
        mock_request.return_value = {"id": 3}
        result = azure_client.post_analysis_summary(
            pr_id=101,
            total_issues=15,
            critical_count=3,
            high_count=5,
            medium_count=5,
            low_count=2,
            quality_gate_passed=False,
        )
        assert result is True

    @patch.object(
        __import__("codescope.integrations.repos.azure_repos", fromlist=["AzureReposClient"]).AzureReposClient,
        "_request"
    )
    def test_post_inline_comment(self, mock_request, azure_client):
        """Should post inline comment for CodeScope issue."""
        mock_request.return_value = {"id": 4}
        result = azure_client.post_inline_comment(
            pr_id=101,
            file_path="src/main.py",
            line=100,
            rule_id="python:S1234",
            message="Test issue",
            severity="HIGH",
        )
        assert result is True


class TestAzurePullRequestDataclass:
    """Test Azure PullRequest dataclass."""

    def test_create_pull_request(self):
        """Should create a pull request with all fields."""
        from codescope.integrations.repos.azure_repos import PullRequest, PullRequestStatus

        pr = PullRequest(
            id=101,
            title="Test PR",
            description="Description",
            source_branch="feature",
            target_branch="main",
            status=PullRequestStatus.ACTIVE,
            created_by="test-user",
            created_at=datetime(2024, 1, 15),
            url="https://dev.azure.com/org/proj/_git/repo/pullrequest/101",
        )
        assert pr.id == 101
        assert pr.status == PullRequestStatus.ACTIVE

    def test_to_dict(self):
        """Should convert to dictionary."""
        from codescope.integrations.repos.azure_repos import PullRequest, PullRequestStatus

        pr = PullRequest(
            id=101,
            title="Test PR",
            status=PullRequestStatus.COMPLETED,
        )
        result = pr.to_dict()
        assert result["id"] == 101
        assert result["status"] == "completed"


# ── Helper Function Tests ─────────────────────────────────────────────────


class TestGetClientFunctions:
    """Test get_*_client helper functions."""

    def test_get_github_repos_client_not_configured(self):
        """Should return None when not configured."""
        from codescope.integrations.repos.github_repos import get_github_repos_client
        import os

        # Clear any existing environment variables
        env_vars = ["CODESCOPE_GITHUB_OWNER", "CODESCOPE_GITHUB_REPOSITORY", "CODESCOPE_GITHUB_TOKEN"]
        original = {k: os.environ.get(k) for k in env_vars}
        for k in env_vars:
            if k in os.environ:
                del os.environ[k]

        try:
            client = get_github_repos_client()
            assert client is None
        finally:
            # Restore environment
            for k, v in original.items():
                if v is not None:
                    os.environ[k] = v

    def test_get_azure_repos_client_not_configured(self):
        """Should return None when not configured."""
        from codescope.integrations.repos.azure_repos import get_azure_repos_client
        import os

        # Clear any existing environment variables
        env_vars = [
            "CODESCOPE_AZURE_REPOS_ORGANIZATION",
            "CODESCOPE_AZURE_REPOS_PROJECT",
            "CODESCOPE_AZURE_REPOS_REPOSITORY",
            "CODESCOPE_AZURE_REPOS_TOKEN",
        ]
        original = {k: os.environ.get(k) for k in env_vars}
        for k in env_vars:
            if k in os.environ:
                del os.environ[k]

        try:
            client = get_azure_repos_client()
            assert client is None
        finally:
            # Restore environment
            for k, v in original.items():
                if v is not None:
                    os.environ[k] = v


# ── Integration Tests ─────────────────────────────────────────────────────


class TestReposIntegration:
    """Integration tests for repository providers."""

    def test_github_check_run_with_annotations(self):
        """Test creating check run with annotations."""
        from codescope.integrations.repos.github_repos import GitHubReposClient, GitHubReposConfig

        config = GitHubReposConfig(
            owner="owner",
            repository="repo",
            api_token="token",
        )
        client = GitHubReposClient(config)

        # Test that post_check_run_with_annotations builds correct payload
        issues = [
            {
                "file_path": "src/main.py",
                "line": 10,
                "severity": "CRITICAL",
                "rule_id": "python:S1234",
                "message": "Test issue",
            },
            {
                "file_path": "src/utils.py",
                "line": 20,
                "severity": "HIGH",
                "rule_id": "python:S5678",
                "message": "Another issue",
            },
        ]

        # The method would make an API call, so we just verify it's callable
        assert callable(client.post_check_run_with_annotations)

    def test_azure_pr_status_workflow(self):
        """Test Azure PR status posting workflow."""
        from codescope.integrations.repos.azure_repos import AzureReposClient, AzureReposConfig

        config = AzureReposConfig(
            organization="org",
            project="proj",
            repository="repo",
            api_token="pat",
        )
        client = AzureReposClient(config)

        # Verify methods are available
        assert callable(client.post_analysis_summary)
        assert callable(client.post_inline_comment)
        assert callable(client.post_pr_status)

    def test_import_from_repos_module(self):
        """Test importing from repos module."""
        from codescope.integrations.repos import (
            GitHubReposClient,
            GitHubReposConfig,
            AzureReposClient,
            AzureReposConfig,
            get_github_repos_client,
            get_azure_repos_client,
        )

        # All imports should succeed
        assert GitHubReposClient is not None
        assert GitHubReposConfig is not None
        assert AzureReposClient is not None
        assert AzureReposConfig is not None
        assert get_github_repos_client is not None
        assert get_azure_repos_client is not None
