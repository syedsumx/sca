"""Tests for issue tracker integrations (Jira, Azure Boards).

This module tests the issue tracker integration functionality including:
- Base classes and models
- Jira client
- Azure Boards client
- Registry and configuration
- API routes
"""

import json
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from codescope.integrations.issue_trackers.base import (
    CreateIssueRequest,
    IssuePriority,
    IssueStatus,
    IssueTrackerProvider,
    TrackerConfig,
    TrackerIssue,
    UpdateIssueRequest,
)


# ── Model Tests ────────────────────────────────────────────────────────────


class TestTrackerConfig:
    """Test TrackerConfig dataclass."""

    def test_create_basic_config(self):
        """Should create a basic config with required fields."""
        config = TrackerConfig(
            provider="jira",
            base_url="https://test.atlassian.net",
            project_key="TEST",
            api_token="secret-token",
        )
        assert config.provider == "jira"
        assert config.base_url == "https://test.atlassian.net"
        assert config.project_key == "TEST"
        assert config.api_token == "secret-token"

    def test_to_dict_masks_token(self):
        """Should mask API token in to_dict output."""
        config = TrackerConfig(
            provider="jira",
            base_url="https://test.atlassian.net",
            project_key="TEST",
            api_token="secret-token",
        )
        result = config.to_dict()
        assert result["api_token"] == "***"
        assert result["base_url"] == "https://test.atlassian.net"

    def test_default_values(self):
        """Should have sensible defaults."""
        config = TrackerConfig(
            provider="jira",
            base_url="https://test.atlassian.net",
            project_key="TEST",
        )
        assert config.default_issue_type == "Bug"
        assert config.default_labels == []
        assert config.username == ""


class TestTrackerIssue:
    """Test TrackerIssue dataclass."""

    def test_create_issue(self):
        """Should create an issue with all fields."""
        issue = TrackerIssue(
            id="123",
            key="TEST-123",
            title="Test Issue",
            description="A test issue",
            status=IssueStatus.OPEN,
            priority=IssuePriority.HIGH,
            issue_type="Bug",
            assignee="john.doe",
            reporter="jane.doe",
            labels=["codescope", "security"],
            created_at=datetime(2024, 1, 15, 10, 30),
            url="https://test.atlassian.net/browse/TEST-123",
        )
        assert issue.id == "123"
        assert issue.key == "TEST-123"
        assert issue.status == IssueStatus.OPEN
        assert issue.priority == IssuePriority.HIGH

    def test_to_dict(self):
        """Should convert to dictionary correctly."""
        issue = TrackerIssue(
            id="123",
            key="TEST-123",
            title="Test Issue",
            status=IssueStatus.IN_PROGRESS,
            priority=IssuePriority.MEDIUM,
        )
        result = issue.to_dict()
        assert result["id"] == "123"
        assert result["key"] == "TEST-123"
        assert result["status"] == "in_progress"
        assert result["priority"] == "medium"


class TestCreateIssueRequest:
    """Test CreateIssueRequest dataclass."""

    def test_create_request(self):
        """Should create a request with all fields."""
        request = CreateIssueRequest(
            title="New Bug",
            description="Found a bug",
            priority=IssuePriority.HIGH,
            issue_type="Bug",
            labels=["urgent"],
            codescope_issue_ids=["abc-123", "def-456"],
        )
        assert request.title == "New Bug"
        assert request.priority == IssuePriority.HIGH
        assert len(request.codescope_issue_ids) == 2


# ── Jira Client Tests ──────────────────────────────────────────────────────


class TestJiraClient:
    """Test JiraClient integration."""

    @pytest.fixture
    def jira_config(self):
        """Create a test Jira configuration."""
        return TrackerConfig(
            provider="jira",
            base_url="https://test.atlassian.net",
            project_key="TEST",
            api_token="test-token",
            username="test@example.com",
        )

    @pytest.fixture
    def jira_client(self, jira_config):
        """Create a JiraClient instance."""
        from codescope.integrations.issue_trackers.jira import JiraClient
        return JiraClient(jira_config)

    def test_name_property(self, jira_client):
        """Should return correct provider name."""
        assert jira_client.name == "jira"

    def test_display_name_property(self, jira_client):
        """Should return correct display name."""
        assert jira_client.display_name == "Jira"

    def test_api_base_cloud(self, jira_client):
        """Should use API v3 for Jira Cloud."""
        assert jira_client._api_base == "https://test.atlassian.net/rest/api/3"

    def test_api_base_server(self, jira_config):
        """Should use API v2 for Jira Server."""
        from codescope.integrations.issue_trackers.jira import JiraClient

        jira_config.base_url = "https://jira.company.com"
        client = JiraClient(jira_config)
        assert client._api_base == "https://jira.company.com/rest/api/2"

    def test_auth_header_basic(self, jira_client):
        """Should generate correct Basic auth header."""
        header = jira_client._auth_header
        assert header.startswith("Basic ")

    def test_priority_mapping(self, jira_client):
        """Should map priorities correctly."""
        assert jira_client._PRIORITY_MAP[IssuePriority.HIGHEST] == "Highest"
        assert jira_client._PRIORITY_MAP[IssuePriority.LOW] == "Low"

    def test_status_mapping(self, jira_client):
        """Should map statuses correctly."""
        assert jira_client._STATUS_MAP["to do"] == IssueStatus.OPEN
        assert jira_client._STATUS_MAP["in progress"] == IssueStatus.IN_PROGRESS
        assert jira_client._STATUS_MAP["done"] == IssueStatus.RESOLVED

    @patch.object(
        __import__("codescope.integrations.issue_trackers.jira", fromlist=["JiraClient"]).JiraClient,
        "_request"
    )
    def test_test_connection_success(self, mock_request, jira_client):
        """Should return success on valid connection."""
        mock_request.return_value = {"name": "Test Project", "key": "TEST"}
        success, message = jira_client.test_connection()
        assert success is True
        assert "Test Project" in message

    @patch.object(
        __import__("codescope.integrations.issue_trackers.jira", fromlist=["JiraClient"]).JiraClient,
        "_request"
    )
    def test_test_connection_failure(self, mock_request, jira_client):
        """Should return failure on invalid connection."""
        mock_request.return_value = None
        success, message = jira_client.test_connection()
        assert success is False

    def test_format_codescope_issue(self, jira_client):
        """Should format CodeScope issue correctly."""
        request = jira_client.format_codescope_issue(
            issue_id="abc-123",
            rule_id="python:S3776",
            message="Function has too high cognitive complexity",
            file_path="src/utils.py",
            line=42,
            severity="MAJOR",
            dashboard_url="https://dashboard.example.com/issues/abc-123",
        )
        assert "[CodeScope]" in request.title
        assert "python:S3776" in request.title
        assert request.priority == IssuePriority.HIGH
        assert "codescope" in request.labels
        assert "severity:major" in request.labels


# ── Azure Boards Client Tests ──────────────────────────────────────────────


class TestAzureBoardsClient:
    """Test AzureBoardsClient integration."""

    @pytest.fixture
    def azure_config(self):
        """Create a test Azure Boards configuration."""
        return TrackerConfig(
            provider="azure_boards",
            base_url="",
            project_key="TestProject",
            api_token="test-pat",
            organization="test-org",
        )

    @pytest.fixture
    def azure_client(self, azure_config):
        """Create an AzureBoardsClient instance."""
        from codescope.integrations.issue_trackers.azure_boards import AzureBoardsClient
        return AzureBoardsClient(azure_config)

    def test_name_property(self, azure_client):
        """Should return correct provider name."""
        assert azure_client.name == "azure_boards"

    def test_display_name_property(self, azure_client):
        """Should return correct display name."""
        assert azure_client.display_name == "Azure Boards"

    def test_api_base_cloud(self, azure_client):
        """Should use correct API base for Azure DevOps Services."""
        assert azure_client._api_base == "https://dev.azure.com/test-org"

    def test_api_base_server(self, azure_config):
        """Should use correct API base for Azure DevOps Server."""
        from codescope.integrations.issue_trackers.azure_boards import AzureBoardsClient

        azure_config.base_url = "https://azuredevops.company.com"
        client = AzureBoardsClient(azure_config)
        assert client._api_base == "https://azuredevops.company.com/test-org"

    def test_auth_header(self, azure_client):
        """Should generate correct Basic auth header."""
        header = azure_client._auth_header
        assert header.startswith("Basic ")

    def test_priority_mapping(self, azure_client):
        """Should map priorities correctly."""
        assert azure_client._PRIORITY_MAP[IssuePriority.HIGHEST] == 1
        assert azure_client._PRIORITY_MAP[IssuePriority.LOW] == 4

    def test_status_mapping(self, azure_client):
        """Should map statuses correctly."""
        assert azure_client._STATUS_MAP["new"] == IssueStatus.OPEN
        assert azure_client._STATUS_MAP["active"] == IssueStatus.IN_PROGRESS
        assert azure_client._STATUS_MAP["resolved"] == IssueStatus.RESOLVED

    def test_convert_to_html(self, azure_client):
        """Should convert markdown to HTML."""
        result = azure_client._convert_to_html("**Bold** text\nNew line")
        assert "<strong>" in result or "<br>" in result

    def test_format_codescope_issue(self, azure_client):
        """Should format CodeScope issue correctly."""
        request = azure_client.format_codescope_issue(
            issue_id="def-456",
            rule_id="java:S1135",
            message="Complete the TODO",
            file_path="src/Main.java",
            line=15,
            severity="MINOR",
            dashboard_url="",
        )
        assert "[CodeScope]" in request.title
        assert "java:S1135" in request.title
        assert request.priority == IssuePriority.MEDIUM


# ── Registry Tests ─────────────────────────────────────────────────────────


class TestRegistry:
    """Test issue tracker registry."""

    def test_get_available_providers(self):
        """Should return list of available providers."""
        from codescope.integrations.issue_trackers.registry import get_available_providers

        providers = get_available_providers()
        provider_names = [p["name"] for p in providers]
        assert "jira" in provider_names
        assert "azure_boards" in provider_names

    def test_create_tracker_from_config_jira(self):
        """Should create Jira tracker from config."""
        from codescope.integrations.issue_trackers.registry import create_tracker_from_config
        from codescope.integrations.issue_trackers.jira import JiraClient

        config = TrackerConfig(
            provider="jira",
            base_url="https://test.atlassian.net",
            project_key="TEST",
            api_token="token",
        )
        tracker = create_tracker_from_config(config)
        assert isinstance(tracker, JiraClient)

    def test_create_tracker_from_config_azure(self):
        """Should create Azure Boards tracker from config."""
        from codescope.integrations.issue_trackers.registry import create_tracker_from_config
        from codescope.integrations.issue_trackers.azure_boards import AzureBoardsClient

        config = TrackerConfig(
            provider="azure_boards",
            base_url="",
            project_key="TEST",
            api_token="pat",
            organization="org",
        )
        tracker = create_tracker_from_config(config)
        assert isinstance(tracker, AzureBoardsClient)

    def test_create_tracker_unknown_provider(self):
        """Should return None for unknown provider."""
        from codescope.integrations.issue_trackers.registry import create_tracker_from_config

        config = TrackerConfig(
            provider="unknown",
            base_url="",
            project_key="TEST",
        )
        tracker = create_tracker_from_config(config)
        assert tracker is None


# ── API Route Tests ────────────────────────────────────────────────────────


try:
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    TestClient = None


@pytest.mark.skipif(not HAS_FASTAPI, reason="FastAPI not installed")
class TestIssueTrackerAPI:
    """Test issue tracker API endpoints."""

    @pytest.fixture
    def app(self):
        """Create test application instance."""
        from codescope.api.app import create_app
        return create_app()

    @pytest.fixture
    def client(self, app):
        """Create test client for the application."""
        return TestClient(app)

    @pytest.fixture
    def admin_headers(self):
        """Generate admin-level authentication headers."""
        from codescope.auth.tokens import TokenManager
        from codescope.auth.models import User, Role

        mgr = TokenManager()
        user = User(
            user_id="admin-user",
            username="admin",
            email="admin@example.com",
            role=Role.ADMIN,
        )
        token = mgr.create_access_token(user)
        return {"Authorization": f"Bearer {token}"}

    def test_list_providers(self, client, admin_headers):
        """Should list available providers."""
        response = client.get(
            "/api/v1/issue-trackers/providers",
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert "providers" in data
        provider_names = [p["name"] for p in data["providers"]]
        assert "jira" in provider_names
        assert "azure_boards" in provider_names

    def test_list_configs_empty(self, client, admin_headers):
        """Should list configured trackers (empty initially)."""
        response = client.get(
            "/api/v1/issue-trackers/configs",
            headers=admin_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert "configs" in data

    def test_create_config_invalid_provider(self, client, admin_headers):
        """Should reject invalid provider."""
        response = client.post(
            "/api/v1/issue-trackers/configs",
            headers=admin_headers,
            json={
                "provider": "invalid",
                "project_key": "TEST",
                "api_token": "token",
            },
        )
        assert response.status_code == 400

    def test_create_config_missing_jira_url(self, client, admin_headers):
        """Should require base_url for Jira."""
        response = client.post(
            "/api/v1/issue-trackers/configs",
            headers=admin_headers,
            json={
                "provider": "jira",
                "project_key": "TEST",
                "api_token": "token",
            },
        )
        assert response.status_code == 400

    def test_create_config_missing_azure_org(self, client, admin_headers):
        """Should require organization for Azure Boards."""
        response = client.post(
            "/api/v1/issue-trackers/configs",
            headers=admin_headers,
            json={
                "provider": "azure_boards",
                "project_key": "TEST",
                "api_token": "token",
            },
        )
        assert response.status_code == 400

    def test_get_config_not_found(self, client, admin_headers):
        """Should return 404 for non-existent config."""
        response = client.get(
            "/api/v1/issue-trackers/nonexistent/issues",
            headers=admin_headers,
        )
        assert response.status_code == 404

    def test_requires_admin_auth(self, client):
        """Should require admin authentication."""
        response = client.get("/api/v1/issue-trackers/providers")
        assert response.status_code == 401


# ── Priority and Status Enum Tests ─────────────────────────────────────────


class TestEnums:
    """Test priority and status enums."""

    def test_issue_priority_values(self):
        """Should have all expected priority values."""
        assert IssuePriority.HIGHEST.value == "highest"
        assert IssuePriority.HIGH.value == "high"
        assert IssuePriority.MEDIUM.value == "medium"
        assert IssuePriority.LOW.value == "low"
        assert IssuePriority.LOWEST.value == "lowest"

    def test_issue_status_values(self):
        """Should have all expected status values."""
        assert IssueStatus.OPEN.value == "open"
        assert IssueStatus.IN_PROGRESS.value == "in_progress"
        assert IssueStatus.RESOLVED.value == "resolved"
        assert IssueStatus.CLOSED.value == "closed"
        assert IssueStatus.REOPENED.value == "reopened"


# ── Integration Tests ──────────────────────────────────────────────────────


class TestIntegration:
    """Integration tests for issue tracker workflow."""

    def test_full_jira_issue_workflow(self):
        """Test creating and formatting issues for Jira."""
        from codescope.integrations.issue_trackers.jira import JiraClient

        config = TrackerConfig(
            provider="jira",
            base_url="https://test.atlassian.net",
            project_key="TEST",
            api_token="test-token",
            username="user@example.com",
            default_issue_type="Bug",
            default_labels=["automated"],
        )
        client = JiraClient(config)

        # Format a CodeScope issue
        request = client.format_codescope_issue(
            issue_id="issue-001",
            rule_id="python:S1481",
            message="Unused local variable 'x'",
            file_path="src/main.py",
            line=25,
            severity="MINOR",
        )

        assert request.title.startswith("[CodeScope]")
        assert "python:S1481" in request.title
        assert request.issue_type == "Bug"
        assert "automated" in request.labels
        assert "codescope" in request.labels
        assert "issue-001" in request.codescope_issue_ids

    def test_full_azure_boards_issue_workflow(self):
        """Test creating and formatting issues for Azure Boards."""
        from codescope.integrations.issue_trackers.azure_boards import AzureBoardsClient

        config = TrackerConfig(
            provider="azure_boards",
            base_url="",
            project_key="TestProject",
            api_token="test-pat",
            organization="test-org",
            default_issue_type="Bug",
            default_labels=["cicd"],
        )
        client = AzureBoardsClient(config)

        # Format a CodeScope issue
        request = client.format_codescope_issue(
            issue_id="issue-002",
            rule_id="java:S2259",
            message="Null pointer dereference",
            file_path="src/Main.java",
            line=100,
            severity="CRITICAL",
        )

        assert request.title.startswith("[CodeScope]")
        assert "java:S2259" in request.title
        assert request.priority == IssuePriority.HIGHEST
        assert "cicd" in request.labels
        assert "codescope" in request.labels
        assert "severity:critical" in request.labels

    def test_severity_to_priority_mapping(self):
        """Test that severities map to correct priorities."""
        from codescope.integrations.issue_trackers.jira import JiraClient

        config = TrackerConfig(
            provider="jira",
            base_url="https://test.atlassian.net",
            project_key="TEST",
        )
        client = JiraClient(config)

        # Test severity mappings
        assert client._map_severity_to_priority("BLOCKER") == IssuePriority.HIGHEST
        assert client._map_severity_to_priority("CRITICAL") == IssuePriority.HIGHEST
        assert client._map_severity_to_priority("MAJOR") == IssuePriority.HIGH
        assert client._map_severity_to_priority("MINOR") == IssuePriority.MEDIUM
        assert client._map_severity_to_priority("INFO") == IssuePriority.LOW
        assert client._map_severity_to_priority("unknown") == IssuePriority.MEDIUM
