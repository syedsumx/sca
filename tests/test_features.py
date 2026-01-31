"""Comprehensive tests for all 10 new features, auth, and API."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ── Feature 1: CI/CD Pipeline Integration ──────────────────────────────────


class TestCICDGenerators:
    """Test CI/CD pipeline config generators."""

    def test_github_actions_generator(self):
        from codescope.cicd.generators import generate_github_actions

        result = generate_github_actions()
        assert "name:" in result
        assert "codescope" in result.lower()
        assert isinstance(result, str)
        assert len(result) > 100

    def test_gitlab_ci_generator(self):
        from codescope.cicd.generators import generate_gitlab_ci

        result = generate_gitlab_ci()
        assert isinstance(result, str)
        assert len(result) > 50

    def test_jenkins_generator(self):
        from codescope.cicd.generators import generate_jenkins

        result = generate_jenkins()
        assert isinstance(result, str)
        assert len(result) > 50

    def test_azure_pipelines_generator(self):
        from codescope.cicd.generators import generate_azure_pipelines

        result = generate_azure_pipelines()
        assert isinstance(result, str)
        assert len(result) > 50

    def test_all_platforms_produce_different_output(self):
        from codescope.cicd.generators import (
            generate_azure_pipelines,
            generate_github_actions,
            generate_gitlab_ci,
            generate_jenkins,
        )

        outputs = [
            generate_github_actions(),
            generate_gitlab_ci(),
            generate_jenkins(),
            generate_azure_pipelines(),
        ]
        assert len(set(outputs)) == 4


# ── Feature 2: Webhooks & Notifications ────────────────────────────────────


class TestNotificationEngine:
    """Test webhook and notification engine."""

    def test_notification_event_creation(self):
        from codescope.notifications.engine import NotificationEvent

        event = NotificationEvent(
            event_type="scan_complete",
            project_name="test-project",
            timestamp=datetime.now(timezone.utc).isoformat(),
            severity="critical",
            summary="Scan completed with 5 issues",
            details={"issues_count": 5},
        )
        assert event.event_type == "scan_complete"
        assert event.project_name == "test-project"

    def test_notification_event_to_dict(self):
        from codescope.notifications.engine import NotificationEvent

        event = NotificationEvent(
            event_type="quality_gate_failed",
            project_name="my-app",
            timestamp="2025-01-01T00:00:00Z",
            severity="warning",
            summary="Gate failed",
        )
        d = event.to_dict()
        assert d["event_type"] == "quality_gate_failed"
        assert d["project_name"] == "my-app"
        assert "timestamp" in d

    def test_slack_notifier_creation(self):
        from codescope.notifications.engine import SlackNotifier

        notifier = SlackNotifier(webhook_url="https://hooks.slack.com/test")
        assert notifier.webhook_url == "https://hooks.slack.com/test"

    def test_teams_notifier_creation(self):
        from codescope.notifications.engine import TeamsNotifier

        notifier = TeamsNotifier(webhook_url="https://outlook.office.com/test")
        assert notifier.webhook_url == "https://outlook.office.com/test"

    def test_webhook_target_creation(self):
        from codescope.notifications.engine import WebhookTarget

        target = WebhookTarget(url="https://example.com/webhook", secret="test-key")
        assert target.secret == "test-key"
        assert target.active is True

    def test_webhook_delivery_creation(self):
        from codescope.notifications.engine import WebhookDelivery, WebhookTarget

        target = WebhookTarget(url="https://example.com/hook", secret="s3cret")
        delivery = WebhookDelivery(target=target)
        assert delivery.target.url == "https://example.com/hook"

    def test_notification_engine_from_config(self):
        from codescope.notifications.engine import NotificationEngine

        config = {"slack_webhook_url": "https://hooks.slack.com/test"}
        engine = NotificationEngine.from_config(config)
        assert engine is not None
        assert len(engine._slack) == 1

    def test_notification_engine_add_channels(self):
        from codescope.notifications.engine import NotificationEngine, WebhookTarget

        engine = NotificationEngine()
        engine.add_slack("https://hooks.slack.com/test")
        engine.add_teams("https://outlook.office.com/test")
        engine.add_webhook(WebhookTarget(url="https://example.com/hook"))
        assert len(engine._slack) == 1
        assert len(engine._teams) == 1
        assert len(engine._webhooks) == 1


# ── Feature 3: Historical Trend Tracking ───────────────────────────────────


class TestTrendStore:
    """Test trend tracking with SQLite backend."""

    def test_record_and_retrieve(self):
        from codescope.trends.store import TrendSnapshot, TrendStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            store = TrendStore(db_path)
            snap = TrendSnapshot(
                project="test-project",
                timestamp=datetime.now(timezone.utc).isoformat(),
                total_issues=10,
                vulnerabilities=2,
                code_smells=5,
                bugs=3,
                coverage=80.5,
                duplication_pct=12.3,
            )
            store.record(snap)

            trends = store.get_trends("test-project")
            assert len(trends) == 1
            assert trends[0]["total_issues"] == 10
            assert trends[0]["vulnerabilities"] == 2
            store.close()
        finally:
            os.unlink(db_path)

    def test_delta_computation(self):
        from codescope.trends.store import TrendSnapshot, TrendStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            store = TrendStore(db_path)
            store.record(TrendSnapshot(
                project="proj", timestamp="2025-01-01T00:00:00Z",
                total_issues=10, vulnerabilities=5, code_smells=3, bugs=2,
            ))
            store.record(TrendSnapshot(
                project="proj", timestamp="2025-01-02T00:00:00Z",
                total_issues=7, vulnerabilities=3, code_smells=2, bugs=2,
            ))

            delta = store.get_delta("proj")
            assert delta is not None
            assert delta["total_issues"]["delta"] == -3
            assert delta["vulnerabilities"]["delta"] == -2
            store.close()
        finally:
            os.unlink(db_path)

    def test_get_projects(self):
        from codescope.trends.store import TrendSnapshot, TrendStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            store = TrendStore(db_path)
            store.record(TrendSnapshot(project="proj-a", timestamp="2025-01-01T00:00:00Z", total_issues=5))
            store.record(TrendSnapshot(project="proj-b", timestamp="2025-01-01T00:00:00Z", total_issues=10))

            projects = store.get_projects()
            assert "proj-a" in projects
            assert "proj-b" in projects
            store.close()
        finally:
            os.unlink(db_path)

    def test_delete_project(self):
        from codescope.trends.store import TrendSnapshot, TrendStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            store = TrendStore(db_path)
            store.record(TrendSnapshot(project="del-me", timestamp="2025-01-01T00:00:00Z"))
            assert "del-me" in store.get_projects()
            store.delete_project("del-me")
            assert "del-me" not in store.get_projects()
            store.close()
        finally:
            os.unlink(db_path)


# ── Feature 4: SARIF Import ───────────────────────────────────────────────


class TestSARIFImport:
    """Test SARIF format import functionality."""

    def test_parse_valid_sarif(self):
        from codescope.api.routes.sarif_import import _parse_sarif

        sarif_data = {
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "TestTool",
                            "version": "1.0",
                            "rules": [],
                        }
                    },
                    "results": [
                        {
                            "ruleId": "TEST001",
                            "message": {"text": "Test issue found"},
                            "level": "error",
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "src/main.py"},
                                        "region": {"startLine": 10},
                                    }
                                }
                            ],
                        }
                    ],
                }
            ],
        }

        issues = _parse_sarif(sarif_data)
        assert len(issues) >= 1
        assert "TEST001" in issues[0]["rule_id"]

    def test_parse_empty_sarif(self):
        from codescope.api.routes.sarif_import import _parse_sarif

        sarif_data = {"version": "2.1.0", "runs": []}
        issues = _parse_sarif(sarif_data)
        assert len(issues) == 0


# ── Feature 5: Custom Rules Engine ────────────────────────────────────────


class TestCustomRulesEngine:
    """Test custom rules engine."""

    def test_load_rules_from_list(self):
        from codescope.rules.custom.engine import CustomRulesEngine

        engine = CustomRulesEngine()
        count = engine.load_from_dict([
            {
                "id": "CUSTOM001",
                "name": "No TODO comments",
                "pattern": r"#\s*TODO",
                "severity": "INFO",
                "message": "TODO comment found",
            }
        ])
        assert count == 1
        assert len(engine.rules) == 1
        assert engine.rules[0].id == "CUSTOM001"

    def test_scan_file_with_custom_rule(self):
        from codescope.rules.custom.engine import CustomRulesEngine

        engine = CustomRulesEngine()
        engine.load_from_dict([
            {
                "id": "CUSTOM002",
                "name": "Debug print",
                "pattern": r"print\(",
                "severity": "WARNING",
                "message": "Debug print statement found",
            }
        ])

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            content = 'x = 1\nprint("debug")\ny = 2\n'
            tmp.write(content)
            tmp_path = tmp.name

        try:
            results = engine.scan_file(tmp_path, content)
            assert len(results) >= 1
            assert results[0].rule.id == "CUSTOM002"
        finally:
            os.unlink(tmp_path)

    def test_scan_file_no_match(self):
        from codescope.rules.custom.engine import CustomRulesEngine

        engine = CustomRulesEngine()
        engine.load_from_dict([
            {
                "id": "CUSTOM003",
                "name": "No eval",
                "pattern": r"\beval\(",
                "severity": "BLOCKER",
                "message": "eval() usage found",
            }
        ])

        content = "x = 1 + 2\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            results = engine.scan_file(tmp_path, content)
            assert len(results) == 0
        finally:
            os.unlink(tmp_path)

    def test_language_filter(self):
        from codescope.rules.custom.engine import CustomRulesEngine

        engine = CustomRulesEngine()
        engine.load_from_dict([
            {
                "id": "CUSTOM004",
                "name": "Java only",
                "pattern": r"System\.out\.println",
                "severity": "INFO",
                "message": "System.out found",
                "languages": ["java"],
            }
        ])

        content = "System.out.println('test')\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            results = engine.scan_file(tmp_path, content)
            assert len(results) == 0
        finally:
            os.unlink(tmp_path)

    def test_negate_mode(self):
        from codescope.rules.custom.engine import CustomRulesEngine

        engine = CustomRulesEngine()
        engine.load_from_dict([
            {
                "id": "CUSTOM005",
                "name": "Missing copyright",
                "pattern": r"Copyright",
                "negate": True,
                "severity": "INFO",
                "message": "No copyright header found",
            }
        ])

        content = "x = 1\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        try:
            results = engine.scan_file(tmp_path, content)
            assert len(results) >= 1
        finally:
            os.unlink(tmp_path)


# ── Feature 6: PR Diff Analysis ───────────────────────────────────────────


class TestPRDiffAnalysis:
    """Test PR diff / comparison analysis."""

    def test_issue_fingerprint(self):
        from codescope.api.routes.compare import _issue_fingerprint

        fp1 = _issue_fingerprint(
            {"rule_id": "S001", "file": "main.py", "line": 10, "message": "issue"}
        )
        fp2 = _issue_fingerprint(
            {"rule_id": "S001", "file": "main.py", "line": 10, "message": "issue"}
        )
        fp3 = _issue_fingerprint(
            {"rule_id": "S002", "file": "main.py", "line": 10, "message": "other"}
        )
        assert fp1 == fp2
        assert fp1 != fp3

    def test_issue_file_fingerprint(self):
        from codescope.api.routes.compare import _issue_file_fingerprint

        fp1 = _issue_file_fingerprint(
            {"rule_id": "S001", "file": "main.py", "line": 10, "message": "issue"}
        )
        fp2 = _issue_file_fingerprint(
            {"rule_id": "S001", "file": "main.py", "line": 20, "message": "issue"}
        )
        assert fp1 == fp2


# ── Feature 7: Secret Scanning ────────────────────────────────────────────


class TestSecretScanner:
    """Test secret scanning functionality."""

    def test_scan_file_with_aws_key(self):
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        # AKIA + 16 uppercase alphanumeric chars = 20 chars total
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write('AWS_KEY = "AKIAIOSFODNN7EXAMPAA"\n')
            tmp_path = tmp.name

        try:
            results = scanner.scan_file(tmp_path)
            assert len(results) >= 1
            assert any("aws" in r.rule_id.lower() for r in results)
        finally:
            os.unlink(tmp_path)

    def test_scan_file_clean(self):
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write("x = 1\ny = 2\nresult = x + y\n")
            tmp_path = tmp.name

        try:
            results = scanner.scan_file(tmp_path)
            assert len(results) == 0
        finally:
            os.unlink(tmp_path)

    def test_allowlist_excludes_placeholders(self):
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as tmp:
            tmp.write('SECRET = "<CHANGE_ME>"\nKEY = "${ENV_VAR}"\n')
            tmp_path = tmp.name

        try:
            results = scanner.scan_file(tmp_path)
            assert len(results) == 0
        finally:
            os.unlink(tmp_path)

    def test_entropy_calculation(self):
        from codescope.analyzers.secrets.scanner import _shannon_entropy

        high = _shannon_entropy("aB3$kL9mNp2qRs5tUv8wXy")
        low = _shannon_entropy("aaaaaaaaaa")
        assert high > low

    def test_scan_directory(self):
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        with tempfile.TemporaryDirectory() as tmpdir:
            p1 = Path(tmpdir) / "clean.py"
            p1.write_text("x = 1\n")
            p2 = Path(tmpdir) / "secret.py"
            p2.write_text('AWS_ACCESS = "AKIAIOSFODNN7EXAMPAA"\n')

            results = scanner.scan_directory(tmpdir)
            assert len(results) >= 1

    def test_secret_finding_to_dict(self):
        from codescope.analyzers.secrets.scanner import SecretFinding

        f = SecretFinding(
            rule_id="aws-access-key",
            rule_name="AWS Access Key",
            severity="CRITICAL",
            file="test.py",
            line=1,
            entropy=4.5,
        )
        d = f.to_dict()
        assert d["rule_id"] == "aws-access-key"
        assert d["entropy"] == 4.5


# ── Feature 8: SBOM Generation ────────────────────────────────────────────


class TestSBOMGenerator:
    """Test Software Bill of Materials generation."""

    def test_load_from_package_json(self):
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator()

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg = Path(tmpdir) / "package.json"
            pkg.write_text(json.dumps({
                "name": "test-app",
                "version": "1.0.0",
                "dependencies": {"react": "^18.2.0", "axios": "^1.6.0"},
                "devDependencies": {"jest": "^29.0.0"},
            }))

            gen.load_from_manifest(str(pkg))
            assert len(gen.components) >= 2
            names = [c.name for c in gen.components]
            assert "react" in names
            assert "axios" in names

    def test_load_from_requirements_txt(self):
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator()

        with tempfile.TemporaryDirectory() as tmpdir:
            req = Path(tmpdir) / "requirements.txt"
            req.write_text("fastapi==0.104.1\nuvicorn>=0.24.0\nrequests\n")

            gen.load_from_manifest(str(req))
            assert len(gen.components) >= 2
            names = [c.name for c in gen.components]
            assert "fastapi" in names

    def test_cyclonedx_output(self):
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            pkg = Path(tmpdir) / "package.json"
            pkg.write_text(json.dumps({
                "name": "app", "version": "1.0.0",
                "dependencies": {"lodash": "^4.17.0"},
            }))
            gen.load_from_manifest(str(pkg))

        cdx = gen.to_cyclonedx()
        assert isinstance(cdx, dict)
        assert cdx.get("bomFormat") == "CycloneDX"
        assert "components" in cdx

    def test_spdx_output(self):
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator()
        with tempfile.TemporaryDirectory() as tmpdir:
            pkg = Path(tmpdir) / "package.json"
            pkg.write_text(json.dumps({
                "name": "app", "version": "1.0.0",
                "dependencies": {"express": "^4.18.0"},
            }))
            gen.load_from_manifest(str(pkg))

        spdx = gen.to_spdx()
        assert isinstance(spdx, dict)
        assert "spdxVersion" in spdx
        assert "packages" in spdx

    def test_add_component_directly(self):
        from codescope.sbom.generator import SBOMComponent, SBOMGenerator

        gen = SBOMGenerator(project_name="test", project_version="1.0.0")
        gen.add_component(SBOMComponent(name="mylib", version="2.0.0"))
        assert len(gen.components) == 1
        assert gen.components[0].name == "mylib"

    def test_load_from_dependency_scan(self):
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator()
        count = gen.load_from_dependency_scan([
            {"name": "react", "version": "18.2.0", "ecosystem": "npm"},
            {"name": "fastapi", "version": "0.104.1", "ecosystem": "pypi"},
        ])
        assert count == 2
        assert len(gen.components) == 2


# ── Feature 9: Team/Organization Management ───────────────────────────────


class TestTeamsManagement:
    """Test team/org management data structures."""

    def test_teams_route_module_imports(self):
        from codescope.api.routes import teams

        assert hasattr(teams, "router")

    def test_teams_router_has_endpoints(self):
        from codescope.api.routes.teams import router

        routes = [r.path for r in router.routes]
        assert len(routes) > 0


# ── Feature 10: Remediation Suggestions ───────────────────────────────────


class TestRemediationEngine:
    """Test remediation suggestion engine."""

    def test_get_suggestion_by_rule_id(self):
        from codescope.remediation.suggestions import RemediationEngine

        engine = RemediationEngine()
        # Use an actual rule_id from the DB
        suggestion = engine.get("python:S508")
        assert suggestion is not None
        assert suggestion.rule_id == "python:S508"

    def test_get_suggestion_for_unknown_rule(self):
        from codescope.remediation.suggestions import RemediationEngine

        engine = RemediationEngine()
        suggestion = engine.get("nonexistent:RULE999")
        assert suggestion is None

    def test_list_all_suggestions(self):
        from codescope.remediation.suggestions import RemediationEngine

        engine = RemediationEngine()
        all_suggestions = engine.list_all()
        assert isinstance(all_suggestions, list)
        assert len(all_suggestions) > 5

    def test_get_for_issue(self):
        from codescope.remediation.suggestions import RemediationEngine

        engine = RemediationEngine()
        issue = {"rule_id": "python:S509", "message": "Command injection detected"}
        suggestion = engine.get_for_issue(issue)
        assert suggestion is not None
        assert "rule_id" in suggestion

    def test_add_custom_suggestion(self):
        from codescope.remediation.suggestions import RemediationEngine

        engine = RemediationEngine()
        engine.add_custom(
            rule_id="CUSTOM:001",
            title="Use prepared statements",
            description="Always use parameterized queries",
            bad_example="cursor.execute(f'SELECT * FROM {table}')",
            fix_example="cursor.execute('SELECT * FROM ?', (table,))",
        )
        suggestion = engine.get("CUSTOM:001")
        assert suggestion is not None
        assert suggestion.title == "Use prepared statements"

    def test_batch_suggestions(self):
        from codescope.remediation.suggestions import RemediationEngine

        engine = RemediationEngine()
        results = engine.get_batch([
            {"rule_id": "python:S508"},
            {"rule_id": "python:S509"},
            {"rule_id": "nonexistent:X"},
        ])
        assert isinstance(results, list)
        assert len(results) == 3

    def test_remediation_to_dict(self):
        from codescope.remediation.suggestions import Remediation

        r = Remediation(
            rule_id="test:001",
            title="Test",
            description="Test desc",
            fix_example="good code",
            bad_example="bad code",
        )
        d = r.to_dict()
        assert d["rule_id"] == "test:001"
        assert d["fix_example"] == "good code"


# ── Auth Module Tests ─────────────────────────────────────────────────────


class TestAuthModule:
    """Test authentication and authorization modules."""

    def test_token_manager_imports(self):
        from codescope.auth.tokens import TokenManager

        assert TokenManager is not None

    def test_oauth_providers_registered(self):
        from codescope.auth.oauth import _PROVIDERS

        assert "github" in _PROVIDERS
        assert "gitlab" in _PROVIDERS
        assert "azure" in _PROVIDERS

    def test_azure_entra_oauth_class(self):
        from codescope.auth.oauth import AzureEntraOAuth

        os.environ["CODESCOPE_AZURE_CLIENT_ID"] = "test-client-id"
        os.environ["CODESCOPE_AZURE_CLIENT_SECRET"] = "test-secret"
        try:
            provider = AzureEntraOAuth()
            assert provider is not None
            auth_url = provider.get_authorize_url("http://localhost/cb", "state123")
            assert "login.microsoftonline.com" in auth_url
            assert "test-client-id" in auth_url
        finally:
            os.environ.pop("CODESCOPE_AZURE_CLIENT_ID", None)
            os.environ.pop("CODESCOPE_AZURE_CLIENT_SECRET", None)

    def test_github_oauth_class(self):
        from codescope.auth.oauth import GitHubOAuth

        os.environ["CODESCOPE_GITHUB_CLIENT_ID"] = "test-gh-id"
        os.environ["CODESCOPE_GITHUB_CLIENT_SECRET"] = "test-secret"
        try:
            provider = GitHubOAuth()
            auth_url = provider.get_authorize_url("http://localhost/cb", "state123")
            assert "github.com" in auth_url
        finally:
            os.environ.pop("CODESCOPE_GITHUB_CLIENT_ID", None)
            os.environ.pop("CODESCOPE_GITHUB_CLIENT_SECRET", None)

    def test_gitlab_oauth_class(self):
        from codescope.auth.oauth import GitLabOAuth

        os.environ["CODESCOPE_GITLAB_CLIENT_ID"] = "test-gl-id"
        os.environ["CODESCOPE_GITLAB_CLIENT_SECRET"] = "test-secret"
        try:
            provider = GitLabOAuth()
            auth_url = provider.get_authorize_url("http://localhost/cb", "state123")
            assert "gitlab.com" in auth_url
        finally:
            os.environ.pop("CODESCOPE_GITLAB_CLIENT_ID", None)
            os.environ.pop("CODESCOPE_GITLAB_CLIENT_SECRET", None)

    def test_role_model(self):
        from codescope.auth.models import Role

        assert Role.ADMIN is not None
        assert Role.VIEWER is not None

    def test_auth_database_module(self):
        from codescope.auth.database import AuthDatabase

        assert AuthDatabase is not None


# ── API App Tests ─────────────────────────────────────────────────────────


class TestAPIApp:
    """Test API app setup and route registration."""

    def test_app_creation(self):
        from codescope.api.app import create_app

        app = create_app()
        assert app is not None
        assert app.title == "CodeScope API"

    def test_all_routes_registered(self):
        from codescope.api.app import create_app

        app = create_app()
        route_paths = [r.path for r in app.routes]
        route_paths_str = " ".join(route_paths)

        feature_keywords = [
            "trends", "remediation", "compare", "secrets",
            "sbom", "cicd", "webhooks", "teams",
        ]
        for keyword in feature_keywords:
            assert any(keyword in p for p in route_paths), (
                f"Route containing '{keyword}' not found in: {route_paths}"
            )

    def test_health_endpoint_exists(self):
        from codescope.api.app import create_app

        app = create_app()
        route_paths = [r.path for r in app.routes]
        assert any("health" in p for p in route_paths)


# ── Core Module Tests ─────────────────────────────────────────────────────


class TestCoreModules:
    """Test core utility modules."""

    def test_cache_config(self):
        from codescope.core.cache import CacheConfig

        config = CacheConfig(max_size_mb=100)
        assert config.max_size_mb == 100

    def test_analysis_cache(self):
        from codescope.core.cache import AnalysisCache

        cache = AnalysisCache()
        assert cache is not None

    def test_config_module(self):
        from codescope.core.config import Config

        config = Config()
        assert config is not None

    def test_streaming_module(self):
        from codescope.core.streaming import StreamingAnalyzer

        assert StreamingAnalyzer is not None

    def test_enums_module(self):
        from codescope.core.enums import Severity

        assert Severity.BLOCKER is not None
        assert Severity.CRITICAL is not None

    def test_location_model(self):
        from codescope.core.models import Location

        loc = Location(file_path=Path("test.py"), start_line=1, end_line=1)
        assert str(loc) == "test.py:1"

    def test_issue_model(self):
        from codescope.core.models import Issue

        assert Issue is not None


# ── Reporter Tests ────────────────────────────────────────────────────────


class TestReporters:
    """Test all output format reporters."""

    def test_html_reporter_imports(self):
        from codescope.reporters.html_reporter import HTMLReporter

        assert HTMLReporter is not None

    def test_markdown_reporter_imports(self):
        from codescope.reporters.markdown_reporter import MarkdownReporter

        assert MarkdownReporter is not None

    def test_gitlab_reporter_imports(self):
        from codescope.reporters.gitlab_reporter import GitLabReporter

        assert GitLabReporter is not None

    def test_sarif_reporter(self):
        from codescope.reporters.sarif import SARIFReporter

        assert SARIFReporter is not None


# ── Parser Tests ──────────────────────────────────────────────────────────


class TestAllParsers:
    """Test that all language parsers load."""

    @pytest.mark.parametrize(
        "language",
        [
            "python", "javascript", "java", "go", "csharp", "cpp",
            "php", "ruby", "rust", "kotlin", "scala", "swift",
        ],
    )
    def test_parser_loads(self, language):
        from codescope.parsers.registry import ParserRegistry

        registry = ParserRegistry()
        parser = registry.get_parser(language)
        assert parser is not None, f"Parser for {language} not found"
        assert parser.language_id == language


# ── Git Module Tests ──────────────────────────────────────────────────────


class TestGitModules:
    """Test git integration modules."""

    def test_blame_module(self):
        from codescope.git.blame import GitBlame

        blame = GitBlame(repo_path=Path("."))
        assert blame is not None

    def test_repo_info(self):
        from codescope.git.repo import get_repo_info

        info = get_repo_info(Path("."))
        assert info is not None

    def test_diff_module(self):
        from codescope.git.diff import GitDiff

        assert GitDiff is not None

    def test_is_git_repo(self):
        from codescope.git.repo import is_git_repo

        assert is_git_repo(Path(".")) is True
        assert is_git_repo(Path("/tmp")) is False
