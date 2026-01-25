"""Tests for quality gates module."""

import pytest
from pathlib import Path

from codescope.analyzers import analyze_path
from codescope.core.config import QualityGateConfig, QualityGateCondition
from codescope.core.enums import QualityGateStatus
from codescope.quality_gates import QualityGate, evaluate_quality_gate


class TestQualityGate:
    """Tests for QualityGate."""

    def test_default_gate_clean_code(self, temp_project):
        """Test default gate with relatively clean code."""
        # Create a clean file
        clean_file = temp_project / "src" / "clean.py"
        clean_file.write_text('''
def add(a, b):
    return a + b
''')

        results = analyze_path(clean_file)
        gate = QualityGate()
        result = gate.evaluate(results)

        # Should pass (no bugs or vulnerabilities in this simple file)
        assert result.status == QualityGateStatus.PASSED

    def test_gate_fails_with_vulnerabilities(self, temp_project):
        """Test that gate fails when vulnerabilities are found."""
        results = analyze_path(temp_project)

        config = QualityGateConfig(
            name="strict",
            conditions=[
                QualityGateCondition(metric="vulnerabilities", operator="GT", threshold=0),
            ]
        )

        result = evaluate_quality_gate(results, config)

        # Should fail because vulnerable.py has issues
        assert result.status == QualityGateStatus.FAILED

    def test_custom_thresholds(self, temp_project):
        """Test gate with custom thresholds."""
        results = analyze_path(temp_project)

        # Lenient config that should pass
        config = QualityGateConfig(
            name="lenient",
            conditions=[
                QualityGateCondition(metric="bugs", operator="GT", threshold=100),
                QualityGateCondition(metric="vulnerabilities", operator="GT", threshold=100),
            ]
        )

        result = evaluate_quality_gate(results, config)
        assert result.status == QualityGateStatus.PASSED

    def test_condition_details(self, temp_project):
        """Test that condition details are provided."""
        results = analyze_path(temp_project)
        result = evaluate_quality_gate(results)

        assert len(result.conditions) > 0
        for cond in result.conditions:
            assert "metric" in cond
            assert "passed" in cond
            assert "actual" in cond
