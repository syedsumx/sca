"""JSON reporter for machine-readable output."""

import json
from typing import Any

from codescope.core.models import AnalysisResults
from codescope.reporters.base import Reporter


class JSONReporter(Reporter):
    """Reporter that outputs JSON format."""

    @property
    def format_name(self) -> str:
        return "json"

    def generate(self, results: AnalysisResults) -> str:
        """Generate JSON report.

        Args:
            results: Analysis results.

        Returns:
            JSON string.
        """
        output = self._build_output(results)
        return json.dumps(output, indent=2, default=str)

    def _build_output(self, results: AnalysisResults) -> dict[str, Any]:
        """Build the output dictionary.

        Args:
            results: Analysis results.

        Returns:
            Dictionary for JSON serialization.
        """
        return {
            "version": "1.0.0",
            "analysis": {
                "project_path": str(results.project_path),
                "started_at": results.started_at.isoformat() if results.started_at else None,
                "completed_at": results.completed_at.isoformat() if results.completed_at else None,
                "status": results.status.value,
                "error_message": results.error_message,
            },
            "metrics": {
                "total_files": results.metrics.total_files,
                "total_lines_of_code": results.metrics.total_lines_of_code,
                "total_issues": results.metrics.total_issues,
                "bugs_count": results.metrics.bugs_count,
                "vulnerabilities_count": results.metrics.vulnerabilities_count,
                "code_smells_count": results.metrics.code_smells_count,
                "hotspots_count": results.metrics.hotspots_count,
                "average_complexity": results.metrics.average_complexity,
                "technical_debt_minutes": results.metrics.technical_debt_minutes,
                "technical_debt_ratio": results.metrics.technical_debt_ratio,
                "duplications_percent": results.metrics.duplications_percent,
                "coverage_percent": results.metrics.coverage_percent,
                "ratings": {
                    "reliability": results.metrics.reliability_rating,
                    "security": results.metrics.security_rating,
                    "maintainability": results.metrics.maintainability_rating,
                },
            },
            "quality_gate": (
                results.quality_gate.to_dict() if results.quality_gate else None
            ),
            "issues": [
                {
                    "id": str(issue.id),
                    "rule_id": issue.rule_id,
                    "rule_name": issue.rule_name,
                    "type": issue.issue_type.value,
                    "severity": issue.severity.value,
                    "message": issue.message,
                    "location": {
                        "file": str(issue.location.file_path),
                        "start_line": issue.location.start_line,
                        "end_line": issue.location.end_line,
                        "start_column": issue.location.start_column,
                        "end_column": issue.location.end_column,
                    },
                    "effort_minutes": issue.effort_minutes,
                    "cwe_ids": issue.cwe_ids,
                    "owasp_categories": issue.owasp_categories,
                    "tags": issue.tags,
                }
                for issue in results.all_issues
            ],
            "files": [
                {
                    "path": str(fa.file_path),
                    "language": fa.language,
                    "issues_count": len(fa.issues),
                    "metrics": fa.metrics.to_dict() if fa.metrics else None,
                }
                for fa in results.files
            ],
        }
