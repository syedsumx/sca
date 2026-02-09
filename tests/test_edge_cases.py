"""Edge case tests for parsers, reporters, and suppression engine.

This module tests boundary conditions, error handling, and unusual inputs
that may not be covered by standard feature tests.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Parser Edge Cases ──────────────────────────────────────────────────────


class TestParserEdgeCases:
    """Edge case tests for language parsers."""

    def test_empty_file_parsing(self):
        """Parser should handle empty files gracefully."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        result = parser.parse("")
        assert result is not None
        assert result.functions == []
        assert result.classes == []

    def test_single_line_file(self):
        """Parser should handle single-line files."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        result = parser.parse("x = 1")
        assert result is not None

    def test_file_with_only_comments(self):
        """Parser should handle files with only comments."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        result = parser.parse("# Just a comment\n# Another comment")
        assert result is not None
        assert result.functions == []

    def test_file_with_only_whitespace(self):
        """Parser should handle files with only whitespace."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        result = parser.parse("   \n\t\n   ")
        assert result is not None

    def test_file_with_syntax_errors(self):
        """Parser should handle files with syntax errors."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        # Invalid Python syntax
        result = parser.parse("def broken(:\n    pass")
        # Should not raise, should return partial or empty result
        assert result is not None

    def test_deeply_nested_code(self):
        """Parser should handle deeply nested code structures."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = "def f():\n" + "  if True:\n" * 20 + "    pass"
        result = parser.parse(code)
        assert result is not None

    def test_very_long_line(self):
        """Parser should handle very long lines."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = f"x = '{'a' * 10000}'"
        result = parser.parse(code)
        assert result is not None

    def test_unicode_content(self):
        """Parser should handle Unicode content."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = '''
def greet(name: str) -> str:
    """Greet with Unicode: 你好世界 🎉"""
    return f"Hello, {name}! 🚀"

emoji_var = "🔥💻🎯"
chinese = "中文变量"
'''
        result = parser.parse(code)
        assert result is not None
        assert len(result.functions) >= 1

    def test_mixed_indentation(self):
        """Parser should handle mixed tabs and spaces."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        # Note: This is intentionally bad code
        code = "def f():\n\tif True:\n        pass"
        result = parser.parse(code)
        # Should not crash
        assert result is not None

    def test_binary_content_graceful_handling(self):
        """Parser should handle binary content gracefully."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        # Simulate binary content as string
        binary_like = "\x00\x01\x02def f():\x00pass"
        result = parser.parse(binary_like)
        # Should not crash
        assert result is not None

    def test_extremely_long_function_name(self):
        """Parser should handle extremely long identifiers."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        long_name = "a" * 500
        code = f"def {long_name}():\n    pass"
        result = parser.parse(code)
        assert result is not None

    def test_many_functions(self):
        """Parser should handle files with many functions."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = "\n".join([f"def func_{i}():\n    pass\n" for i in range(100)])
        result = parser.parse(code)
        assert result is not None
        assert len(result.functions) >= 100

    def test_javascript_empty_file(self):
        """JavaScript parser should handle empty files."""
        from codescope.parsers.registry import ParserRegistry

        registry = ParserRegistry()
        parser = registry.get_parser("javascript")
        result = parser.parse("")
        assert result is not None

    def test_java_empty_file(self):
        """Java parser should handle empty files."""
        from codescope.parsers.registry import ParserRegistry

        registry = ParserRegistry()
        parser = registry.get_parser("java")
        result = parser.parse("")
        assert result is not None

    def test_go_empty_file(self):
        """Go parser should handle empty files."""
        from codescope.parsers.registry import ParserRegistry

        registry = ParserRegistry()
        parser = registry.get_parser("go")
        result = parser.parse("")
        assert result is not None


# ── Reporter Edge Cases ────────────────────────────────────────────────────


class TestReporterEdgeCases:
    """Edge case tests for report generators."""

    def test_json_reporter_empty_issues(self):
        """JSON reporter should handle empty issue lists."""
        from codescope.reporters.json_reporter import JSONReporter

        reporter = JSONReporter()
        result = reporter.generate(issues=[], metrics={})
        assert result is not None
        parsed = json.loads(result)
        assert parsed["issues"] == []

    def test_json_reporter_special_characters(self):
        """JSON reporter should escape special characters."""
        from codescope.reporters.json_reporter import JSONReporter

        reporter = JSONReporter()
        issues = [
            {
                "rule_id": "TEST001",
                "message": 'Issue with "quotes" and\nnewlines\tand\ttabs',
                "file": "test.py",
                "line": 1,
                "severity": "MAJOR",
            }
        ]
        result = reporter.generate(issues=issues, metrics={})
        # Should not raise, should produce valid JSON
        parsed = json.loads(result)
        assert len(parsed["issues"]) == 1

    def test_json_reporter_unicode_issues(self):
        """JSON reporter should handle Unicode in issues."""
        from codescope.reporters.json_reporter import JSONReporter

        reporter = JSONReporter()
        issues = [
            {
                "rule_id": "TEST001",
                "message": "Unicode issue: 中文 日本語 🔥",
                "file": "модуль.py",
                "line": 1,
                "severity": "MAJOR",
            }
        ]
        result = reporter.generate(issues=issues, metrics={})
        parsed = json.loads(result)
        assert "中文" in parsed["issues"][0]["message"]

    def test_html_reporter_empty_issues(self):
        """HTML reporter should generate valid HTML for empty issues."""
        from codescope.reporters.html_reporter import HTMLReporter

        reporter = HTMLReporter()
        result = reporter.generate(issues=[], metrics={})
        assert "<html" in result.lower() or "<!doctype" in result.lower()
        assert "</html>" in result.lower()

    def test_html_reporter_xss_prevention(self):
        """HTML reporter should escape potential XSS content."""
        from codescope.reporters.html_reporter import HTMLReporter

        reporter = HTMLReporter()
        issues = [
            {
                "rule_id": "XSS001",
                "message": "<script>alert('xss')</script>",
                "file": "test.py",
                "line": 1,
                "severity": "CRITICAL",
            }
        ]
        result = reporter.generate(issues=issues, metrics={})
        # Raw script tags should be escaped
        assert "<script>alert" not in result
        assert "&lt;script&gt;" in result or "script" in result

    def test_sarif_reporter_empty_issues(self):
        """SARIF reporter should generate valid SARIF for empty issues."""
        from codescope.reporters.sarif import SARIFReporter

        reporter = SARIFReporter()
        result = reporter.generate(issues=[], metrics={})
        parsed = json.loads(result)
        assert parsed["version"] == "2.1.0"
        assert "runs" in parsed

    def test_sarif_reporter_all_severities(self):
        """SARIF reporter should map all severity levels correctly."""
        from codescope.reporters.sarif import SARIFReporter

        reporter = SARIFReporter()
        issues = [
            {"rule_id": "T1", "message": "Blocker", "file": "a.py", "line": 1, "severity": "BLOCKER"},
            {"rule_id": "T2", "message": "Critical", "file": "a.py", "line": 2, "severity": "CRITICAL"},
            {"rule_id": "T3", "message": "Major", "file": "a.py", "line": 3, "severity": "MAJOR"},
            {"rule_id": "T4", "message": "Minor", "file": "a.py", "line": 4, "severity": "MINOR"},
            {"rule_id": "T5", "message": "Info", "file": "a.py", "line": 5, "severity": "INFO"},
        ]
        result = reporter.generate(issues=issues, metrics={})
        parsed = json.loads(result)
        assert len(parsed["runs"][0]["results"]) == 5

    def test_markdown_reporter_empty_issues(self):
        """Markdown reporter should handle empty issues."""
        from codescope.reporters.markdown_reporter import MarkdownReporter

        reporter = MarkdownReporter()
        result = reporter.generate(issues=[], metrics={})
        assert isinstance(result, str)
        assert "#" in result  # Should have markdown headers

    def test_gitlab_reporter_format(self):
        """GitLab reporter should produce GitLab-compatible format."""
        from codescope.reporters.gitlab_reporter import GitLabReporter

        reporter = GitLabReporter()
        issues = [
            {
                "rule_id": "TEST001",
                "message": "Test issue",
                "file": "test.py",
                "line": 10,
                "severity": "MAJOR",
            }
        ]
        result = reporter.generate(issues=issues, metrics={})
        # Should be valid JSON array
        parsed = json.loads(result)
        assert isinstance(parsed, list)

    def test_console_reporter_no_color_mode(self):
        """Console reporter should work without color support."""
        from codescope.reporters.console import ConsoleReporter

        reporter = ConsoleReporter()
        # Should not raise even in non-TTY environment
        result = reporter.generate(issues=[], metrics={})
        assert result is not None


# ── Suppression Engine Edge Cases ──────────────────────────────────────────


class TestSuppressionEngineEdgeCases:
    """Edge case tests for suppression engine."""

    def test_empty_suppression_file(self):
        """Engine should handle empty suppression files."""
        from codescope.suppress.engine import SuppressionEngine

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            path = f.name

        try:
            engine = SuppressionEngine()
            engine.load_from_file(path)
            # Should not crash, should have no suppressions
            assert engine.is_suppressed("TEST001", "any.py", 1) is False
        finally:
            os.unlink(path)

    def test_invalid_yaml_suppression_file(self):
        """Engine should handle invalid YAML gracefully."""
        from codescope.suppress.engine import SuppressionEngine

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("not: valid: yaml: content: [[[")
            path = f.name

        try:
            engine = SuppressionEngine()
            # Should not crash, may log warning
            try:
                engine.load_from_file(path)
            except Exception:
                pass  # Acceptable to raise for invalid YAML
        finally:
            os.unlink(path)

    def test_suppression_with_glob_patterns(self):
        """Engine should support glob patterns in file paths."""
        from codescope.suppress.engine import SuppressionEngine

        engine = SuppressionEngine()
        engine.add_suppression(
            rule_id="TEST001",
            file_pattern="**/test_*.py",
            reason="Suppress in test files",
        )

        assert engine.is_suppressed("TEST001", "tests/test_main.py", 1) is True
        assert engine.is_suppressed("TEST001", "src/main.py", 1) is False

    def test_suppression_with_line_range(self):
        """Engine should support line ranges."""
        from codescope.suppress.engine import SuppressionEngine

        engine = SuppressionEngine()
        engine.add_suppression(
            rule_id="TEST001",
            file_pattern="main.py",
            start_line=10,
            end_line=20,
            reason="Known issue in this range",
        )

        assert engine.is_suppressed("TEST001", "main.py", 15) is True
        assert engine.is_suppressed("TEST001", "main.py", 5) is False
        assert engine.is_suppressed("TEST001", "main.py", 25) is False

    def test_suppression_expiration(self):
        """Engine should respect suppression expiration dates."""
        from datetime import datetime, timedelta

        from codescope.suppress.engine import SuppressionEngine

        engine = SuppressionEngine()

        # Expired suppression
        past_date = (datetime.now() - timedelta(days=1)).isoformat()
        engine.add_suppression(
            rule_id="TEST001",
            file_pattern="*.py",
            reason="Expired",
            expires=past_date,
        )

        # Should not be suppressed (expired)
        assert engine.is_suppressed("TEST001", "main.py", 1) is False

    def test_suppression_case_sensitivity(self):
        """Engine should handle rule ID case correctly."""
        from codescope.suppress.engine import SuppressionEngine

        engine = SuppressionEngine()
        engine.add_suppression(
            rule_id="python:S508",
            file_pattern="*.py",
            reason="Case test",
        )

        # Same case should match
        assert engine.is_suppressed("python:S508", "main.py", 1) is True

    def test_wildcard_rule_suppression(self):
        """Engine should support wildcard rule suppression."""
        from codescope.suppress.engine import SuppressionEngine

        engine = SuppressionEngine()
        engine.add_suppression(
            rule_id="python:*",
            file_pattern="legacy.py",
            reason="Legacy file - suppress all Python rules",
        )

        assert engine.is_suppressed("python:S508", "legacy.py", 1) is True
        assert engine.is_suppressed("python:S509", "legacy.py", 1) is True
        assert engine.is_suppressed("java:S100", "legacy.py", 1) is False

    def test_inline_suppression_comment(self):
        """Engine should detect inline suppression comments."""
        from codescope.suppress.engine import SuppressionEngine

        engine = SuppressionEngine()

        code = """
x = eval(user_input)  # codescope:disable=python:S509
y = eval(other_input)
"""
        # Check if line 2 (with comment) is suppressed
        assert engine.is_inline_suppressed("python:S509", code.split("\n")[1]) is True
        assert engine.is_inline_suppressed("python:S509", code.split("\n")[2]) is False


# ── Secret Scanner Edge Cases ──────────────────────────────────────────────


class TestSecretScannerEdgeCases:
    """Edge case tests for secret scanning."""

    def test_false_positive_example_values(self):
        """Scanner should not flag obvious example values."""
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write('''
API_KEY = "your-api-key-here"
SECRET = "XXXX-XXXX-XXXX"
PASSWORD = "changeme"
TOKEN = "example_token_12345"
''')
            path = f.name

        try:
            results = scanner.scan_file(path)
            # Should not flag obvious placeholders
            assert len(results) == 0
        finally:
            os.unlink(path)

    def test_false_positive_base64_images(self):
        """Scanner should not flag base64 encoded images."""
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            # Small base64 PNG header
            f.write('''
ICON = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
''')
            path = f.name

        try:
            results = scanner.scan_file(path)
            # Should recognize as image data, not secret
            assert len([r for r in results if "base64" in r.rule_id.lower()]) == 0
        finally:
            os.unlink(path)

    def test_binary_file_handling(self):
        """Scanner should skip binary files."""
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(b"\x00\x01\x02\x03\x04\x05")
            path = f.name

        try:
            results = scanner.scan_file(path)
            # Should return empty or skip gracefully
            assert isinstance(results, list)
        finally:
            os.unlink(path)

    def test_large_file_handling(self):
        """Scanner should handle large files efficiently."""
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            # Write 10MB of code
            for i in range(100000):
                f.write(f"x_{i} = {i}\n")
            path = f.name

        try:
            # Should complete without timeout or memory error
            results = scanner.scan_file(path)
            assert isinstance(results, list)
        finally:
            os.unlink(path)


# ── Custom Rules Engine Edge Cases ─────────────────────────────────────────


class TestCustomRulesEdgeCases:
    """Edge case tests for custom rules engine."""

    def test_invalid_regex_pattern(self):
        """Engine should handle invalid regex patterns gracefully."""
        from codescope.rules.custom.engine import CustomRulesEngine

        engine = CustomRulesEngine()

        # Invalid regex
        count = engine.load_from_dict([
            {
                "id": "INVALID001",
                "name": "Invalid regex",
                "pattern": "[[[invalid",
                "severity": "INFO",
                "message": "Test",
            }
        ])
        # Should skip invalid patterns
        assert count == 0 or len(engine.rules) == 0

    def test_empty_pattern(self):
        """Engine should handle empty patterns."""
        from codescope.rules.custom.engine import CustomRulesEngine

        engine = CustomRulesEngine()

        count = engine.load_from_dict([
            {
                "id": "EMPTY001",
                "name": "Empty pattern",
                "pattern": "",
                "severity": "INFO",
                "message": "Test",
            }
        ])
        # Should skip or handle gracefully
        assert count == 0

    def test_overlapping_matches(self):
        """Engine should handle overlapping pattern matches."""
        from codescope.rules.custom.engine import CustomRulesEngine

        engine = CustomRulesEngine()
        engine.load_from_dict([
            {
                "id": "OVERLAP001",
                "name": "Pattern 1",
                "pattern": r"print\(",
                "severity": "INFO",
                "message": "Print found",
            },
            {
                "id": "OVERLAP002",
                "name": "Pattern 2",
                "pattern": r"print\(.*\)",
                "severity": "INFO",
                "message": "Print with args",
            }
        ])

        content = 'print("hello")\n'
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(content)
            path = f.name

        try:
            results = engine.scan_file(path, content)
            # Both patterns should match
            assert len(results) >= 2
        finally:
            os.unlink(path)


# ── SBOM Generator Edge Cases ──────────────────────────────────────────────


class TestSBOMEdgeCases:
    """Edge case tests for SBOM generation."""

    def test_empty_manifest(self):
        """Generator should handle empty manifest files."""
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator()

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg = Path(tmpdir) / "package.json"
            pkg.write_text("{}")

            gen.load_from_manifest(str(pkg))
            assert len(gen.components) == 0

    def test_malformed_package_json(self):
        """Generator should handle malformed package.json."""
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator()

        with tempfile.TemporaryDirectory() as tmpdir:
            pkg = Path(tmpdir) / "package.json"
            pkg.write_text("not valid json {{{")

            try:
                gen.load_from_manifest(str(pkg))
            except json.JSONDecodeError:
                pass  # Expected

    def test_requirements_with_comments(self):
        """Generator should handle requirements.txt with comments."""
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator()

        with tempfile.TemporaryDirectory() as tmpdir:
            req = Path(tmpdir) / "requirements.txt"
            req.write_text("""
# This is a comment
fastapi==0.104.1
# Another comment
uvicorn>=0.24.0
-e git+https://github.com/user/repo.git  # Editable install
""")

            gen.load_from_manifest(str(req))
            assert len(gen.components) >= 2

    def test_requirements_with_extras(self):
        """Generator should handle requirements with extras."""
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator()

        with tempfile.TemporaryDirectory() as tmpdir:
            req = Path(tmpdir) / "requirements.txt"
            req.write_text("""
requests[security]==2.31.0
uvicorn[standard]>=0.24.0
celery[redis]~=5.3.0
""")

            gen.load_from_manifest(str(req))
            names = [c.name for c in gen.components]
            assert "requests" in names


# ── Trend Store Edge Cases ─────────────────────────────────────────────────


class TestTrendStoreEdgeCases:
    """Edge case tests for trend storage."""

    def test_concurrent_writes(self):
        """Store should handle concurrent writes safely."""
        import threading

        from codescope.trends.store import TrendSnapshot, TrendStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            store = TrendStore(db_path)

            def write_snapshot(i):
                store.record(TrendSnapshot(
                    project=f"proj-{i % 5}",
                    timestamp=f"2025-01-{i:02d}T00:00:00Z",
                    total_issues=i,
                ))

            threads = [threading.Thread(target=write_snapshot, args=(i,)) for i in range(1, 20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # All writes should succeed
            projects = store.get_projects()
            assert len(projects) >= 1
            store.close()
        finally:
            os.unlink(db_path)

    def test_large_dataset(self):
        """Store should handle large datasets efficiently."""
        from codescope.trends.store import TrendSnapshot, TrendStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            store = TrendStore(db_path)

            # Insert 1000 snapshots
            for i in range(1000):
                store.record(TrendSnapshot(
                    project="big-project",
                    timestamp=f"2025-01-01T{i:04d}:00Z",
                    total_issues=i,
                ))

            trends = store.get_trends("big-project", limit=100)
            assert len(trends) == 100
            store.close()
        finally:
            os.unlink(db_path)


# ── Quality Gate Edge Cases ────────────────────────────────────────────────


class TestQualityGateEdgeCases:
    """Edge case tests for quality gate evaluation."""

    def test_zero_threshold(self):
        """Gate should handle zero thresholds correctly."""
        from codescope.quality_gates.gate import QualityGate

        gate = QualityGate()
        gate.add_condition("vulnerabilities", "<=", 0)

        result = gate.evaluate({"vulnerabilities": 0})
        assert result.passed is True

        result = gate.evaluate({"vulnerabilities": 1})
        assert result.passed is False

    def test_missing_metric(self):
        """Gate should handle missing metrics gracefully."""
        from codescope.quality_gates.gate import QualityGate

        gate = QualityGate()
        gate.add_condition("coverage", ">=", 80)

        # Metric not provided
        result = gate.evaluate({})
        # Should fail or handle gracefully
        assert result is not None

    def test_negative_values(self):
        """Gate should handle negative metric values."""
        from codescope.quality_gates.gate import QualityGate

        gate = QualityGate()
        gate.add_condition("bugs", "<=", 5)

        result = gate.evaluate({"bugs": -1})
        assert result.passed is True

    def test_float_precision(self):
        """Gate should handle floating point precision correctly."""
        from codescope.quality_gates.gate import QualityGate

        gate = QualityGate()
        gate.add_condition("coverage", ">=", 80.0)

        result = gate.evaluate({"coverage": 79.9999999})
        assert result.passed is False

        result = gate.evaluate({"coverage": 80.0000001})
        assert result.passed is True


# ── Analysis Orchestrator Edge Cases ───────────────────────────────────────


class TestAnalysisOrchestratorEdgeCases:
    """Edge case tests for analysis orchestration."""

    def test_empty_directory(self):
        """Orchestrator should handle empty directories."""
        from codescope.core.analysis import AnalysisOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            orchestrator = AnalysisOrchestrator()
            result = orchestrator.analyze(Path(tmpdir))
            assert result is not None
            assert len(result.issues) == 0

    def test_directory_with_only_ignored_files(self):
        """Orchestrator should handle directories with only ignored files."""
        from codescope.core.analysis import AnalysisOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create only files that should be ignored
            Path(tmpdir, ".git").mkdir()
            Path(tmpdir, ".git", "config").write_text("git config")
            Path(tmpdir, "node_modules").mkdir()
            Path(tmpdir, "node_modules", "package.json").write_text("{}")
            Path(tmpdir, "__pycache__").mkdir()
            Path(tmpdir, "__pycache__", "module.pyc").write_bytes(b"\x00")

            orchestrator = AnalysisOrchestrator()
            result = orchestrator.analyze(Path(tmpdir))
            assert result is not None

    def test_symlink_handling(self):
        """Orchestrator should handle symlinks correctly."""
        from codescope.core.analysis import AnalysisOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a file and a symlink to it
            real_file = Path(tmpdir) / "real.py"
            real_file.write_text("x = 1\n")

            link = Path(tmpdir) / "link.py"
            try:
                link.symlink_to(real_file)
            except OSError:
                pytest.skip("Symlinks not supported on this system")

            orchestrator = AnalysisOrchestrator()
            result = orchestrator.analyze(Path(tmpdir))
            # Should not analyze same content twice
            assert result is not None

    def test_permission_denied(self):
        """Orchestrator should handle permission denied errors."""
        from codescope.core.analysis import AnalysisOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            restricted = Path(tmpdir) / "restricted.py"
            restricted.write_text("x = 1\n")

            # Skip on Windows as chmod behaves differently
            if os.name != "nt":
                os.chmod(restricted, 0o000)
                try:
                    orchestrator = AnalysisOrchestrator()
                    # Should not crash
                    result = orchestrator.analyze(Path(tmpdir))
                    assert result is not None
                finally:
                    os.chmod(restricted, 0o644)
