"""Tests for the reporters module."""

import json
import pytest
from pathlib import Path

from codescope.analyzers import analyze_path
from codescope.reporters import ConsoleReporter, JSONReporter, SARIFReporter


class TestConsoleReporter:
    """Tests for ConsoleReporter."""

    def test_generate_report(self, temp_project):
        """Test console report generation."""
        results = analyze_path(temp_project)
        reporter = ConsoleReporter()

        report = reporter.generate(results)

        assert "CODESCOPE ANALYSIS REPORT" in report
        assert "METRICS SUMMARY" in report


class TestJSONReporter:
    """Tests for JSONReporter."""

    def test_generate_valid_json(self, temp_project):
        """Test that JSON output is valid."""
        results = analyze_path(temp_project)
        reporter = JSONReporter()

        report = reporter.generate(results)
        data = json.loads(report)

        assert "version" in data
        assert "analysis" in data
        assert "metrics" in data
        assert "issues" in data

    def test_json_structure(self, temp_project):
        """Test JSON structure correctness."""
        results = analyze_path(temp_project)
        reporter = JSONReporter()

        data = json.loads(reporter.generate(results))

        # Check metrics
        assert "total_files" in data["metrics"]
        assert "bugs_count" in data["metrics"]
        assert "ratings" in data["metrics"]

        # Check issues structure
        if data["issues"]:
            issue = data["issues"][0]
            assert "rule_id" in issue
            assert "severity" in issue
            assert "location" in issue


class TestSARIFReporter:
    """Tests for SARIFReporter."""

    def test_generate_valid_sarif(self, temp_project):
        """Test that SARIF output is valid."""
        results = analyze_path(temp_project)
        reporter = SARIFReporter()

        report = reporter.generate(results)
        data = json.loads(report)

        # Check SARIF structure
        assert data["version"] == "2.1.0"
        assert "$schema" in data
        assert "runs" in data
        assert len(data["runs"]) == 1

    def test_sarif_tool_info(self, temp_project):
        """Test SARIF tool information."""
        results = analyze_path(temp_project)
        reporter = SARIFReporter()

        data = json.loads(reporter.generate(results))
        tool = data["runs"][0]["tool"]["driver"]

        assert tool["name"] == "CodeScope"
        assert "version" in tool
        assert "rules" in tool

    def test_sarif_results(self, temp_project):
        """Test SARIF results structure."""
        results = analyze_path(temp_project)
        reporter = SARIFReporter()

        data = json.loads(reporter.generate(results))
        sarif_results = data["runs"][0]["results"]

        if sarif_results:
            result = sarif_results[0]
            assert "ruleId" in result
            assert "level" in result
            assert "message" in result
            assert "locations" in result
