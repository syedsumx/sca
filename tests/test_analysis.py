"""Tests for the analysis module."""

import pytest
from pathlib import Path

from codescope.analyzers import analyze_path
from codescope.core.config import Config
from codescope.core.enums import AnalysisStatus, IssueType


class TestAnalysisOrchestrator:
    """Tests for AnalysisOrchestrator."""

    def test_analyze_directory(self, temp_project):
        """Test analyzing a directory."""
        results = analyze_path(temp_project)

        assert results.status == AnalysisStatus.COMPLETED
        assert results.metrics.total_files >= 1

    def test_analyze_single_file(self, temp_project):
        """Test analyzing a single file."""
        file_path = temp_project / "src" / "main.py"
        results = analyze_path(file_path)

        assert results.status == AnalysisStatus.COMPLETED
        assert results.metrics.total_files == 1

    def test_analyze_vulnerable_code(self, temp_project):
        """Test that vulnerabilities are detected."""
        results = analyze_path(temp_project)

        # Should find at least the hardcoded password and command injection
        assert results.metrics.vulnerabilities_count >= 1

    def test_metrics_calculation(self, temp_project):
        """Test that metrics are calculated."""
        results = analyze_path(temp_project)

        assert results.metrics.total_lines_of_code > 0
        assert results.metrics.reliability_rating in "ABCDE"
        assert results.metrics.security_rating in "ABCDE"

    def test_analysis_with_config(self, temp_project):
        """Test analysis with custom configuration."""
        config = Config()
        config.rules.disabled = ["python:S2068"]  # Disable hardcoded secret rule

        results = analyze_path(temp_project, config)

        # Hardcoded secret issues should be filtered out
        hardcoded_issues = [
            i for i in results.all_issues
            if i.rule_id == "python:S2068"
        ]
        assert len(hardcoded_issues) == 0
