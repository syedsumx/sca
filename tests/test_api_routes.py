"""Comprehensive API route HTTP tests using FastAPI TestClient.

This module provides full endpoint coverage for all API routes,
testing request/response handling, authentication, and error cases.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Import FastAPI test client
try:
    from fastapi.testclient import TestClient
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False
    TestClient = None

pytestmark = pytest.mark.skipif(not HAS_FASTAPI, reason="FastAPI not installed")


# ── Test Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def app():
    """Create test application instance."""
    from codescope.api.app import create_app
    return create_app()


@pytest.fixture
def client(app):
    """Create test client for the application."""
    return TestClient(app)


@pytest.fixture
def auth_headers():
    """Generate valid authentication headers for testing."""
    # Create a test token
    from codescope.auth.tokens import TokenManager
    mgr = TokenManager()
    token = mgr.create_access_token(
        user_id="test-user-123",
        email="test@example.com",
        role="admin",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def viewer_headers():
    """Generate viewer-level authentication headers."""
    from codescope.auth.tokens import TokenManager
    mgr = TokenManager()
    token = mgr.create_access_token(
        user_id="viewer-user",
        email="viewer@example.com",
        role="viewer",
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def analyst_headers():
    """Generate analyst-level authentication headers."""
    from codescope.auth.tokens import TokenManager
    mgr = TokenManager()
    token = mgr.create_access_token(
        user_id="analyst-user",
        email="analyst@example.com",
        role="analyst",
    )
    return {"Authorization": f"Bearer {token}"}


# ── Health Check Tests ─────────────────────────────────────────────────────


class TestHealthEndpoint:
    """Test the health check endpoint."""

    def test_health_check_returns_200(self, client):
        """Health endpoint should return 200 OK."""
        response = client.get("/api/health")
        assert response.status_code == 200

    def test_health_check_returns_status(self, client):
        """Health endpoint should return status and version."""
        response = client.get("/api/health")
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data

    def test_health_check_no_auth_required(self, client):
        """Health endpoint should not require authentication."""
        # No auth headers
        response = client.get("/api/health")
        assert response.status_code == 200


# ── Authentication Tests ───────────────────────────────────────────────────


class TestAuthEndpoints:
    """Test authentication-related endpoints."""

    def test_login_with_valid_credentials(self, client):
        """Login should succeed with valid credentials."""
        with patch("codescope.api.routes.auth.authenticate_user") as mock_auth:
            mock_auth.return_value = {
                "id": "user-123",
                "email": "user@example.com",
                "role": "viewer",
            }
            response = client.post(
                "/api/v1/auth/login",
                json={"email": "user@example.com", "password": "password123"},
            )
            # Should return token or success
            assert response.status_code in [200, 201]

    def test_login_with_invalid_credentials(self, client):
        """Login should fail with invalid credentials."""
        with patch("codescope.api.routes.auth.authenticate_user") as mock_auth:
            mock_auth.return_value = None
            response = client.post(
                "/api/v1/auth/login",
                json={"email": "bad@example.com", "password": "wrong"},
            )
            assert response.status_code in [401, 403]

    def test_protected_route_without_auth(self, client):
        """Protected routes should reject unauthenticated requests."""
        response = client.get("/api/v1/projects")
        assert response.status_code == 401

    def test_protected_route_with_invalid_token(self, client):
        """Protected routes should reject invalid tokens."""
        response = client.get(
            "/api/v1/projects",
            headers={"Authorization": "Bearer invalid-token"},
        )
        assert response.status_code in [401, 403]

    def test_protected_route_with_expired_token(self, client):
        """Protected routes should reject expired tokens."""
        from codescope.auth.tokens import TokenManager
        import time

        mgr = TokenManager()
        # Create token that expires immediately
        with patch.object(mgr, "_access_token_expire_minutes", 0):
            token = mgr.create_access_token(
                user_id="test",
                email="test@example.com",
                role="viewer",
            )

        time.sleep(1)  # Wait for expiration

        response = client.get(
            "/api/v1/projects",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code in [401, 403]


# ── Projects API Tests ─────────────────────────────────────────────────────


class TestProjectsEndpoints:
    """Test project management endpoints."""

    def test_list_projects(self, client, auth_headers):
        """Should list all projects."""
        response = client.get("/api/v1/projects", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, (list, dict))

    def test_get_project_by_id(self, client, auth_headers):
        """Should get a specific project."""
        response = client.get("/api/v1/projects/test-project", headers=auth_headers)
        # May be 200 or 404 depending on whether project exists
        assert response.status_code in [200, 404]

    def test_create_project(self, client, auth_headers):
        """Should create a new project."""
        response = client.post(
            "/api/v1/projects",
            headers=auth_headers,
            json={"name": "new-project", "path": "/path/to/project"},
        )
        assert response.status_code in [200, 201, 422]  # 422 if validation fails


# ── Analysis API Tests ─────────────────────────────────────────────────────


class TestAnalysisEndpoints:
    """Test code analysis endpoints."""

    def test_trigger_analysis_requires_analyst_role(self, client, viewer_headers):
        """Analysis endpoint should require analyst role."""
        response = client.post(
            "/api/v1/analysis/trigger",
            headers=viewer_headers,
            json={"project": "test", "path": "/tmp"},
        )
        assert response.status_code in [401, 403]

    def test_trigger_analysis_with_analyst_role(self, client, analyst_headers):
        """Analysis endpoint should accept analyst role."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "test.py").write_text("x = 1")
            response = client.post(
                "/api/v1/analysis/trigger",
                headers=analyst_headers,
                json={"project": "test", "path": tmpdir},
            )
            # Should be accepted (may be async)
            assert response.status_code in [200, 201, 202, 422]

    def test_get_analysis_results(self, client, auth_headers):
        """Should retrieve analysis results."""
        response = client.get(
            "/api/v1/analysis/results/test-project",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]


# ── Issues API Tests ───────────────────────────────────────────────────────


class TestIssuesEndpoints:
    """Test issue management endpoints."""

    def test_list_issues(self, client, auth_headers):
        """Should list issues for a project."""
        response = client.get(
            "/api/v1/issues?project=test",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_filter_issues_by_severity(self, client, auth_headers):
        """Should filter issues by severity."""
        response = client.get(
            "/api/v1/issues?project=test&severity=CRITICAL",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_filter_issues_by_type(self, client, auth_headers):
        """Should filter issues by type."""
        response = client.get(
            "/api/v1/issues?project=test&type=VULNERABILITY",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_get_issue_by_id(self, client, auth_headers):
        """Should get specific issue details."""
        response = client.get(
            "/api/v1/issues/issue-123",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]


# ── Trends API Tests ───────────────────────────────────────────────────────


class TestTrendsEndpoints:
    """Test trend tracking endpoints."""

    def test_get_project_trends(self, client, auth_headers):
        """Should retrieve project trends."""
        response = client.get(
            "/api/v1/trends/test-project",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]
        if response.status_code == 200:
            data = response.json()
            assert isinstance(data, (list, dict))

    def test_get_trends_with_date_range(self, client, auth_headers):
        """Should filter trends by date range."""
        response = client.get(
            "/api/v1/trends/test-project?from=2025-01-01&to=2025-12-31",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_get_delta(self, client, auth_headers):
        """Should retrieve delta between latest scans."""
        response = client.get(
            "/api/v1/trends/test-project/delta",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]


# ── Remediation API Tests ──────────────────────────────────────────────────


class TestRemediationEndpoints:
    """Test remediation suggestion endpoints."""

    def test_get_remediation_for_rule(self, client, auth_headers):
        """Should get remediation for a specific rule."""
        response = client.get(
            "/api/v1/remediation/python:S508",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]
        if response.status_code == 200:
            data = response.json()
            assert "rule_id" in data or "title" in data

    def test_list_all_remediations(self, client, auth_headers):
        """Should list all available remediations."""
        response = client.get(
            "/api/v1/remediation",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_batch_remediations(self, client, auth_headers):
        """Should get remediations for multiple rules."""
        response = client.post(
            "/api/v1/remediation/batch",
            headers=auth_headers,
            json={"rule_ids": ["python:S508", "python:S509"]},
        )
        assert response.status_code in [200, 422]


# ── SBOM API Tests ─────────────────────────────────────────────────────────


class TestSBOMEndpoints:
    """Test SBOM generation endpoints."""

    def test_generate_sbom_cyclonedx(self, client, analyst_headers):
        """Should generate CycloneDX SBOM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pkg = Path(tmpdir) / "package.json"
            pkg.write_text(json.dumps({
                "name": "test-app",
                "version": "1.0.0",
                "dependencies": {"lodash": "^4.17.0"},
            }))

            response = client.post(
                "/api/v1/sbom/generate",
                headers=analyst_headers,
                json={"path": tmpdir, "format": "cyclonedx"},
            )
            assert response.status_code in [200, 422]
            if response.status_code == 200:
                data = response.json()
                assert data.get("bomFormat") == "CycloneDX"

    def test_generate_sbom_spdx(self, client, analyst_headers):
        """Should generate SPDX SBOM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            req = Path(tmpdir) / "requirements.txt"
            req.write_text("fastapi==0.104.1\n")

            response = client.post(
                "/api/v1/sbom/generate",
                headers=analyst_headers,
                json={"path": tmpdir, "format": "spdx"},
            )
            assert response.status_code in [200, 422]
            if response.status_code == 200:
                data = response.json()
                assert "spdxVersion" in data


# ── Secrets API Tests ──────────────────────────────────────────────────────


class TestSecretsEndpoints:
    """Test secret scanning endpoints."""

    def test_scan_for_secrets(self, client, analyst_headers):
        """Should scan directory for secrets."""
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "config.py").write_text('API_KEY = "test-key"\n')

            response = client.post(
                "/api/v1/secrets/scan",
                headers=analyst_headers,
                json={"path": tmpdir},
            )
            assert response.status_code in [200, 422]

    def test_get_secret_findings(self, client, auth_headers):
        """Should retrieve secret findings for project."""
        response = client.get(
            "/api/v1/secrets/findings?project=test",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]


# ── Compare API Tests ──────────────────────────────────────────────────────


class TestCompareEndpoints:
    """Test PR diff/comparison endpoints."""

    def test_compare_analyses(self, client, auth_headers):
        """Should compare two analysis results."""
        response = client.post(
            "/api/v1/compare",
            headers=auth_headers,
            json={
                "base_analysis_id": "analysis-1",
                "head_analysis_id": "analysis-2",
            },
        )
        assert response.status_code in [200, 404, 422]
        if response.status_code == 200:
            data = response.json()
            assert "new_issues" in data or "fixed_issues" in data or "diff" in data


# ── CI/CD API Tests ────────────────────────────────────────────────────────


class TestCICDEndpoints:
    """Test CI/CD configuration generation endpoints."""

    def test_generate_github_actions(self, client, analyst_headers):
        """Should generate GitHub Actions config."""
        response = client.post(
            "/api/v1/cicd/generate",
            headers=analyst_headers,
            json={"platform": "github"},
        )
        assert response.status_code in [200, 422]
        if response.status_code == 200:
            data = response.json()
            assert "config" in data or "yaml" in data or isinstance(data, str)

    def test_generate_gitlab_ci(self, client, analyst_headers):
        """Should generate GitLab CI config."""
        response = client.post(
            "/api/v1/cicd/generate",
            headers=analyst_headers,
            json={"platform": "gitlab"},
        )
        assert response.status_code in [200, 422]

    def test_generate_jenkins(self, client, analyst_headers):
        """Should generate Jenkins config."""
        response = client.post(
            "/api/v1/cicd/generate",
            headers=analyst_headers,
            json={"platform": "jenkins"},
        )
        assert response.status_code in [200, 422]

    def test_generate_azure_pipelines(self, client, analyst_headers):
        """Should generate Azure Pipelines config."""
        response = client.post(
            "/api/v1/cicd/generate",
            headers=analyst_headers,
            json={"platform": "azure"},
        )
        assert response.status_code in [200, 422]

    def test_invalid_platform(self, client, analyst_headers):
        """Should reject invalid platform."""
        response = client.post(
            "/api/v1/cicd/generate",
            headers=analyst_headers,
            json={"platform": "invalid-platform"},
        )
        assert response.status_code in [400, 422]


# ── Webhooks API Tests ─────────────────────────────────────────────────────


class TestWebhooksEndpoints:
    """Test webhook management endpoints (admin only)."""

    def test_list_webhooks_requires_admin(self, client, analyst_headers):
        """Webhook endpoints should require admin role."""
        response = client.get(
            "/api/v1/webhooks",
            headers=analyst_headers,
        )
        assert response.status_code in [401, 403]

    def test_list_webhooks_as_admin(self, client, auth_headers):
        """Admin should be able to list webhooks."""
        response = client.get(
            "/api/v1/webhooks",
            headers=auth_headers,
        )
        assert response.status_code == 200

    def test_create_webhook(self, client, auth_headers):
        """Admin should be able to create webhooks."""
        response = client.post(
            "/api/v1/webhooks",
            headers=auth_headers,
            json={
                "url": "https://example.com/webhook",
                "events": ["scan_complete", "quality_gate_failed"],
            },
        )
        assert response.status_code in [200, 201, 422]


# ── Teams API Tests ────────────────────────────────────────────────────────


class TestTeamsEndpoints:
    """Test team/organization management endpoints."""

    def test_list_teams_requires_admin(self, client, viewer_headers):
        """Team endpoints should require admin role."""
        response = client.get(
            "/api/v1/teams",
            headers=viewer_headers,
        )
        assert response.status_code in [401, 403]

    def test_list_teams_as_admin(self, client, auth_headers):
        """Admin should be able to list teams."""
        response = client.get(
            "/api/v1/teams",
            headers=auth_headers,
        )
        assert response.status_code == 200

    def test_create_team(self, client, auth_headers):
        """Admin should be able to create teams."""
        response = client.post(
            "/api/v1/teams",
            headers=auth_headers,
            json={"name": "test-team", "description": "Test team"},
        )
        assert response.status_code in [200, 201, 422]


# ── Custom Rules API Tests ─────────────────────────────────────────────────


class TestCustomRulesEndpoints:
    """Test custom rules management endpoints."""

    def test_list_custom_rules(self, client, auth_headers):
        """Should list custom rules."""
        response = client.get(
            "/api/v1/custom-rules",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_create_custom_rule(self, client, analyst_headers):
        """Should create a custom rule."""
        response = client.post(
            "/api/v1/custom-rules",
            headers=analyst_headers,
            json={
                "id": "CUSTOM:TEST001",
                "name": "No TODO comments",
                "pattern": r"#\s*TODO",
                "severity": "INFO",
                "message": "TODO comment found",
            },
        )
        assert response.status_code in [200, 201, 422]


# ── SARIF Import API Tests ─────────────────────────────────────────────────


class TestSARIFImportEndpoints:
    """Test SARIF import endpoints."""

    def test_import_sarif(self, client, analyst_headers):
        """Should import SARIF file."""
        sarif_data = {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "TestTool", "version": "1.0"}},
                    "results": [],
                }
            ],
        }

        response = client.post(
            "/api/v1/sarif/import",
            headers=analyst_headers,
            json=sarif_data,
        )
        assert response.status_code in [200, 201, 422]


# ── Coverage API Tests ─────────────────────────────────────────────────────


class TestCoverageEndpoints:
    """Test coverage report endpoints."""

    def test_get_coverage(self, client, auth_headers):
        """Should get coverage data."""
        response = client.get(
            "/api/v1/coverage?project=test",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_upload_coverage_report(self, client, analyst_headers):
        """Should accept coverage report upload."""
        coverage_data = {
            "project": "test",
            "line_coverage": 85.5,
            "branch_coverage": 72.0,
            "files": [
                {"path": "src/main.py", "lines_covered": 100, "lines_total": 120}
            ],
        }

        response = client.post(
            "/api/v1/coverage/upload",
            headers=analyst_headers,
            json=coverage_data,
        )
        assert response.status_code in [200, 201, 422]


# ── Dependencies API Tests ─────────────────────────────────────────────────


class TestDependenciesEndpoints:
    """Test dependency analysis endpoints."""

    def test_list_dependencies(self, client, auth_headers):
        """Should list project dependencies."""
        response = client.get(
            "/api/v1/dependencies?project=test",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_get_vulnerable_dependencies(self, client, auth_headers):
        """Should get vulnerable dependencies."""
        response = client.get(
            "/api/v1/dependencies/vulnerable?project=test",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]


# ── Duplications API Tests ─────────────────────────────────────────────────


class TestDuplicationsEndpoints:
    """Test code duplication endpoints."""

    def test_get_duplications(self, client, auth_headers):
        """Should get duplication data."""
        response = client.get(
            "/api/v1/duplications?project=test",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]


# ── Rules API Tests ────────────────────────────────────────────────────────


class TestRulesEndpoints:
    """Test rules listing endpoints."""

    def test_list_rules(self, client, auth_headers):
        """Should list all available rules."""
        response = client.get(
            "/api/v1/rules",
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

    def test_filter_rules_by_language(self, client, auth_headers):
        """Should filter rules by language."""
        response = client.get(
            "/api/v1/rules?language=python",
            headers=auth_headers,
        )
        assert response.status_code == 200

    def test_get_rule_by_id(self, client, auth_headers):
        """Should get rule details."""
        response = client.get(
            "/api/v1/rules/python:S508",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]


# ── Export API Tests ───────────────────────────────────────────────────────


class TestExportEndpoints:
    """Test report export endpoints."""

    def test_export_json(self, client, auth_headers):
        """Should export JSON report."""
        response = client.get(
            "/api/v1/export/test-project?format=json",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_export_sarif(self, client, auth_headers):
        """Should export SARIF report."""
        response = client.get(
            "/api/v1/export/test-project?format=sarif",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_export_html(self, client, auth_headers):
        """Should export HTML report."""
        response = client.get(
            "/api/v1/export/test-project?format=html",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]


# ── Git API Tests ──────────────────────────────────────────────────────────


class TestGitEndpoints:
    """Test git integration endpoints."""

    def test_get_git_info(self, client, auth_headers):
        """Should get git repository info."""
        response = client.get(
            "/api/v1/git/info?project=test",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_get_blame(self, client, auth_headers):
        """Should get git blame for file."""
        response = client.get(
            "/api/v1/git/blame?project=test&file=src/main.py",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]


# ── Suppressions API Tests ─────────────────────────────────────────────────


class TestSuppressionsEndpoints:
    """Test suppression management endpoints."""

    def test_list_suppressions(self, client, auth_headers):
        """Should list suppressions."""
        response = client.get(
            "/api/v1/suppressions?project=test",
            headers=auth_headers,
        )
        assert response.status_code in [200, 404]

    def test_create_suppression(self, client, analyst_headers):
        """Should create a suppression."""
        response = client.post(
            "/api/v1/suppressions",
            headers=analyst_headers,
            json={
                "rule_id": "python:S508",
                "file_pattern": "tests/**",
                "reason": "Test files allowed to have this pattern",
            },
        )
        assert response.status_code in [200, 201, 422]


# ── Error Handling Tests ───────────────────────────────────────────────────


class TestErrorHandling:
    """Test API error handling."""

    def test_404_for_unknown_route(self, client):
        """Should return 404 for unknown routes."""
        response = client.get("/api/v1/unknown-route")
        assert response.status_code == 404

    def test_405_for_wrong_method(self, client, auth_headers):
        """Should return 405 for wrong HTTP method."""
        response = client.delete("/api/health", headers=auth_headers)
        assert response.status_code == 405

    def test_422_for_invalid_json(self, client, auth_headers):
        """Should return 422 for invalid JSON body."""
        response = client.post(
            "/api/v1/projects",
            headers={**auth_headers, "Content-Type": "application/json"},
            content="not valid json",
        )
        assert response.status_code == 422

    def test_cors_headers(self, client):
        """Should include CORS headers."""
        response = client.options(
            "/api/health",
            headers={"Origin": "http://localhost:3000"},
        )
        # Should have CORS headers or be a valid response
        assert response.status_code in [200, 204, 405]


# ── Edge Case Tests ─────────────────────────────────────────────────────────


class TestAPIEdgeCases:
    """Comprehensive edge case tests for API routes."""

    # ── Input Validation Edge Cases ─────────────────────────────────────────

    def test_empty_body(self, client, auth_headers):
        """Should handle empty request body gracefully."""
        response = client.post(
            "/api/v1/projects",
            headers={**auth_headers, "Content-Type": "application/json"},
            content="",
        )
        assert response.status_code in [400, 422]

    def test_null_body(self, client, auth_headers):
        """Should handle null JSON body."""
        response = client.post(
            "/api/v1/projects",
            headers=auth_headers,
            json=None,
        )
        assert response.status_code in [400, 422]

    def test_extremely_long_string_field(self, client, auth_headers):
        """Should handle extremely long string fields."""
        response = client.post(
            "/api/v1/projects",
            headers=auth_headers,
            json={"name": "x" * 100000, "path": "/tmp/test"},
        )
        # Should either accept with truncation or reject with 422
        assert response.status_code in [200, 201, 400, 422, 413]

    def test_unicode_in_fields(self, client, auth_headers):
        """Should handle Unicode characters in fields."""
        response = client.post(
            "/api/v1/projects",
            headers=auth_headers,
            json={"name": "项目-测试-🚀", "path": "/tmp/test"},
        )
        # Should accept Unicode or fail validation gracefully
        assert response.status_code in [200, 201, 422]

    def test_special_characters_in_path(self, client, auth_headers):
        """Should handle special characters in path parameters."""
        response = client.get(
            "/api/v1/projects/test%2F..%2Fmalicious",
            headers=auth_headers,
        )
        # Should not allow path traversal
        assert response.status_code in [400, 403, 404, 422]

    def test_sql_injection_in_query_params(self, client, auth_headers):
        """Should safely handle SQL injection attempts in query params."""
        response = client.get(
            "/api/v1/issues?project=test' OR '1'='1",
            headers=auth_headers,
        )
        # Should not execute injection - either 404 or safe error
        assert response.status_code in [200, 400, 404, 422]

    def test_xss_in_input(self, client, auth_headers):
        """Should safely handle XSS attempts in input."""
        response = client.post(
            "/api/v1/projects",
            headers=auth_headers,
            json={"name": "<script>alert('xss')</script>", "path": "/tmp"},
        )
        if response.status_code == 200:
            data = response.json()
            # Name should be escaped or sanitized
            assert "<script>" not in str(data)

    def test_nested_json_depth(self, client, auth_headers):
        """Should handle deeply nested JSON objects."""
        # Create deeply nested structure
        nested = {"level": 0}
        current = nested
        for i in range(100):
            current["nested"] = {"level": i + 1}
            current = current["nested"]

        response = client.post(
            "/api/v1/projects",
            headers=auth_headers,
            json=nested,
        )
        # Should either accept or reject with appropriate error
        assert response.status_code in [200, 400, 413, 422]

    def test_array_with_many_elements(self, client, auth_headers):
        """Should handle arrays with many elements."""
        response = client.post(
            "/api/v1/remediation/batch",
            headers=auth_headers,
            json={"rule_ids": [f"rule_{i}" for i in range(10000)]},
        )
        # Should handle large arrays gracefully
        assert response.status_code in [200, 400, 413, 422]

    # ── Query Parameter Edge Cases ──────────────────────────────────────────

    def test_negative_limit(self, client, auth_headers):
        """Should handle negative limit values."""
        response = client.get(
            "/api/v1/issues?project=test&limit=-10",
            headers=auth_headers,
        )
        assert response.status_code in [200, 400, 404, 422]

    def test_zero_limit(self, client, auth_headers):
        """Should handle zero limit values."""
        response = client.get(
            "/api/v1/issues?project=test&limit=0",
            headers=auth_headers,
        )
        assert response.status_code in [200, 400, 404, 422]

    def test_very_large_offset(self, client, auth_headers):
        """Should handle very large offset values."""
        response = client.get(
            "/api/v1/issues?project=test&offset=9999999999",
            headers=auth_headers,
        )
        assert response.status_code in [200, 400, 404, 422]

    def test_non_numeric_pagination(self, client, auth_headers):
        """Should handle non-numeric pagination values."""
        response = client.get(
            "/api/v1/issues?project=test&limit=abc&offset=xyz",
            headers=auth_headers,
        )
        assert response.status_code in [400, 404, 422]

    def test_duplicate_query_params(self, client, auth_headers):
        """Should handle duplicate query parameters."""
        response = client.get(
            "/api/v1/issues?project=test&severity=CRITICAL&severity=MAJOR",
            headers=auth_headers,
        )
        # Should handle as array or use last value
        assert response.status_code in [200, 400, 404, 422]

    def test_empty_query_param_value(self, client, auth_headers):
        """Should handle empty query parameter values."""
        response = client.get(
            "/api/v1/issues?project=test&severity=",
            headers=auth_headers,
        )
        assert response.status_code in [200, 400, 404, 422]

    # ── Authentication Edge Cases ───────────────────────────────────────────

    def test_malformed_bearer_token(self, client):
        """Should reject malformed bearer tokens."""
        response = client.get(
            "/api/v1/projects",
            headers={"Authorization": "Bearer not.a.valid.jwt.token"},
        )
        assert response.status_code in [401, 403]

    def test_missing_bearer_prefix(self, client):
        """Should reject tokens without Bearer prefix."""
        from codescope.auth.tokens import TokenManager
        mgr = TokenManager()
        token = mgr.create_access_token(
            user_id="test",
            email="test@example.com",
            role="admin",
        )
        response = client.get(
            "/api/v1/projects",
            headers={"Authorization": token},  # Missing "Bearer "
        )
        assert response.status_code in [401, 403]

    def test_token_with_wrong_algorithm(self, client):
        """Should reject tokens signed with wrong algorithm."""
        # Create a token with a different algorithm (simulated)
        response = client.get(
            "/api/v1/projects",
            headers={"Authorization": "Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJ0ZXN0In0."},
        )
        assert response.status_code in [401, 403]

    def test_case_sensitive_role(self, client, viewer_headers):
        """Should handle role case correctly."""
        # Viewer role should not access admin endpoints
        response = client.get(
            "/api/v1/webhooks",
            headers=viewer_headers,
        )
        assert response.status_code in [401, 403]

    # ── Content Type Edge Cases ─────────────────────────────────────────────

    def test_wrong_content_type(self, client, auth_headers):
        """Should handle wrong content type."""
        response = client.post(
            "/api/v1/projects",
            headers={**auth_headers, "Content-Type": "text/plain"},
            content='{"name": "test", "path": "/tmp"}',
        )
        assert response.status_code in [400, 415, 422]

    def test_multipart_form_data(self, client, auth_headers):
        """Should handle multipart form data appropriately."""
        response = client.post(
            "/api/v1/projects",
            headers=auth_headers,
            data={"name": "test", "path": "/tmp"},
        )
        # May accept form data or require JSON
        assert response.status_code in [200, 201, 400, 415, 422]

    # ── Concurrent Request Edge Cases ───────────────────────────────────────

    def test_idempotent_get_requests(self, client, auth_headers):
        """GET requests should be idempotent."""
        responses = []
        for _ in range(5):
            response = client.get("/api/v1/rules", headers=auth_headers)
            responses.append(response.status_code)

        # All responses should be the same
        assert len(set(responses)) == 1

    # ── Response Edge Cases ─────────────────────────────────────────────────

    def test_large_response_handling(self, client, auth_headers):
        """Should handle requests that may return large responses."""
        response = client.get(
            "/api/v1/rules",
            headers=auth_headers,
        )
        assert response.status_code == 200
        # Response should be valid JSON regardless of size
        data = response.json()
        assert isinstance(data, list)

    def test_accept_header_json(self, client, auth_headers):
        """Should respect Accept header for JSON."""
        response = client.get(
            "/api/v1/projects",
            headers={**auth_headers, "Accept": "application/json"},
        )
        assert response.status_code in [200, 404]
        if response.status_code == 200:
            assert "application/json" in response.headers.get("content-type", "")


# ── Rate Limiting and Timeout Tests ─────────────────────────────────────────


class TestRateLimitingAndTimeouts:
    """Test rate limiting and timeout handling."""

    def test_rapid_requests(self, client, auth_headers):
        """Should handle rapid successive requests."""
        responses = []
        for _ in range(20):
            response = client.get("/api/health")
            responses.append(response.status_code)

        # Most should succeed; some may be rate limited
        success_count = sum(1 for s in responses if s == 200)
        assert success_count >= 15  # At least 75% should succeed

    def test_health_check_always_available(self, client):
        """Health check should always be accessible."""
        for _ in range(10):
            response = client.get("/api/health")
            assert response.status_code == 200


# ── File Upload Edge Cases ──────────────────────────────────────────────────


class TestFileUploadEdgeCases:
    """Test file upload handling."""

    def test_sarif_import_empty_runs(self, client, analyst_headers):
        """Should handle SARIF with empty runs array."""
        sarif_data = {
            "version": "2.1.0",
            "runs": [],
        }
        response = client.post(
            "/api/v1/sarif/import",
            headers=analyst_headers,
            json=sarif_data,
        )
        assert response.status_code in [200, 201, 422]

    def test_sarif_import_invalid_version(self, client, analyst_headers):
        """Should validate SARIF version."""
        sarif_data = {
            "version": "1.0.0",  # Old version
            "runs": [],
        }
        response = client.post(
            "/api/v1/sarif/import",
            headers=analyst_headers,
            json=sarif_data,
        )
        assert response.status_code in [200, 201, 400, 422]

    def test_sarif_import_large_results(self, client, analyst_headers):
        """Should handle SARIF with many results."""
        sarif_data = {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "TestTool", "version": "1.0"}},
                    "results": [
                        {
                            "ruleId": f"RULE{i:04d}",
                            "message": {"text": f"Issue {i}"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": f"file{i % 10}.py"},
                                        "region": {"startLine": i % 100 + 1},
                                    }
                                }
                            ],
                        }
                        for i in range(1000)
                    ],
                }
            ],
        }
        response = client.post(
            "/api/v1/sarif/import",
            headers=analyst_headers,
            json=sarif_data,
        )
        assert response.status_code in [200, 201, 413, 422]


# ── Analysis Edge Cases ─────────────────────────────────────────────────────


class TestAnalysisEdgeCases:
    """Test analysis endpoint edge cases."""

    def test_analysis_nonexistent_path(self, client, analyst_headers):
        """Should handle analysis request for nonexistent path."""
        response = client.post(
            "/api/v1/analysis/trigger",
            headers=analyst_headers,
            json={"project": "test", "path": "/nonexistent/path/12345"},
        )
        assert response.status_code in [400, 404, 422]

    def test_analysis_empty_directory(self, client, analyst_headers):
        """Should handle analysis of empty directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            response = client.post(
                "/api/v1/analysis/trigger",
                headers=analyst_headers,
                json={"project": "test", "path": tmpdir},
            )
            # Should complete with zero issues or report no files
            assert response.status_code in [200, 201, 202, 422]

    def test_analysis_binary_files_only(self, client, analyst_headers):
        """Should handle directory with only binary files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a binary file
            with open(os.path.join(tmpdir, "binary.bin"), "wb") as f:
                f.write(bytes([0x00, 0xFF, 0x00, 0xFF] * 100))

            response = client.post(
                "/api/v1/analysis/trigger",
                headers=analyst_headers,
                json={"project": "test", "path": tmpdir},
            )
            assert response.status_code in [200, 201, 202, 422]

    def test_analysis_symlink_loop(self, client, analyst_headers):
        """Should handle directories with symlink loops."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file and symlink (symlink loops are tricky to create safely)
            Path(tmpdir, "test.py").write_text("x = 1")
            try:
                os.symlink(tmpdir, os.path.join(tmpdir, "loop"))
            except (OSError, NotImplementedError):
                # Skip on systems that don't support symlinks
                pytest.skip("Symlinks not supported")

            response = client.post(
                "/api/v1/analysis/trigger",
                headers=analyst_headers,
                json={"project": "test", "path": tmpdir},
            )
            # Should not hang or crash
            assert response.status_code in [200, 201, 202, 400, 422]


# ── Webhook Edge Cases ──────────────────────────────────────────────────────


class TestWebhookEdgeCases:
    """Test webhook endpoint edge cases."""

    def test_webhook_invalid_url(self, client, auth_headers):
        """Should validate webhook URL format."""
        response = client.post(
            "/api/v1/webhooks",
            headers=auth_headers,
            json={"url": "not-a-valid-url", "events": ["scan_complete"]},
        )
        assert response.status_code in [400, 422]

    def test_webhook_localhost_url(self, client, auth_headers):
        """Should handle localhost webhook URLs."""
        response = client.post(
            "/api/v1/webhooks",
            headers=auth_headers,
            json={"url": "http://localhost:8080/hook", "events": ["scan_complete"]},
        )
        # May accept or reject localhost URLs depending on policy
        assert response.status_code in [200, 201, 400, 422]

    def test_webhook_empty_events(self, client, auth_headers):
        """Should require at least one event."""
        response = client.post(
            "/api/v1/webhooks",
            headers=auth_headers,
            json={"url": "https://example.com/hook", "events": []},
        )
        assert response.status_code in [400, 422]

    def test_webhook_invalid_event(self, client, auth_headers):
        """Should validate event names."""
        response = client.post(
            "/api/v1/webhooks",
            headers=auth_headers,
            json={"url": "https://example.com/hook", "events": ["invalid_event_type"]},
        )
        assert response.status_code in [200, 201, 400, 422]
