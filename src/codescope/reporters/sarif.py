"""SARIF reporter for security tool integration."""

import json
from typing import Any
from pathlib import Path

from codescope.core.enums import Severity
from codescope.core.models import AnalysisResults, Issue
from codescope.reporters.base import Reporter
from codescope.version import __version__


class SARIFReporter(Reporter):
    """Reporter that outputs SARIF 2.1.0 format.

    SARIF (Static Analysis Results Interchange Format) is an OASIS standard
    for the output format of static analysis tools.
    """

    SARIF_VERSION = "2.1.0"
    SARIF_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"

    @property
    def format_name(self) -> str:
        return "sarif"

    def generate(self, results: AnalysisResults) -> str:
        """Generate SARIF report.

        Args:
            results: Analysis results.

        Returns:
            SARIF JSON string.
        """
        sarif = self._build_sarif(results)
        return json.dumps(sarif, indent=2)

    def _build_sarif(self, results: AnalysisResults) -> dict[str, Any]:
        """Build SARIF document structure.

        Args:
            results: Analysis results.

        Returns:
            SARIF document as dictionary.
        """
        return {
            "$schema": self.SARIF_SCHEMA,
            "version": self.SARIF_VERSION,
            "runs": [self._build_run(results)],
        }

    def _build_run(self, results: AnalysisResults) -> dict[str, Any]:
        """Build a SARIF run object.

        Args:
            results: Analysis results.

        Returns:
            Run object dictionary.
        """
        # Collect unique rules
        rules_map: dict[str, dict[str, Any]] = {}
        for issue in results.all_issues:
            if issue.rule_id not in rules_map:
                rules_map[issue.rule_id] = self._build_rule(issue)

        return {
            "tool": {
                "driver": {
                    "name": "CodeScope",
                    "version": __version__,
                    "informationUri": "https://github.com/codescope/codescope",
                    "rules": list(rules_map.values()),
                }
            },
            "results": [self._build_result(issue, results.project_path) for issue in results.all_issues],
            "invocations": [
                {
                    "executionSuccessful": results.error_message is None,
                    "startTimeUtc": results.started_at.isoformat() + "Z" if results.started_at else None,
                    "endTimeUtc": results.completed_at.isoformat() + "Z" if results.completed_at else None,
                }
            ],
        }

    def _build_rule(self, issue: Issue) -> dict[str, Any]:
        """Build a SARIF rule object from an issue.

        Args:
            issue: Issue to extract rule info from.

        Returns:
            Rule object dictionary.
        """
        rule: dict[str, Any] = {
            "id": issue.rule_id,
            "name": issue.rule_name,
            "shortDescription": {"text": issue.rule_name},
            "fullDescription": {"text": issue.message},
            "defaultConfiguration": {
                "level": self._severity_to_level(issue.severity),
            },
            "properties": {
                "tags": issue.tags,
            },
        }

        # Add security metadata
        if issue.cwe_ids:
            rule["properties"]["cwe"] = [f"CWE-{cwe}" for cwe in issue.cwe_ids]

        if issue.owasp_categories:
            rule["properties"]["owasp"] = issue.owasp_categories

        return rule

    def _build_result(self, issue: Issue, project_path: Path) -> dict[str, Any]:
        """Build a SARIF result object from an issue.

        Args:
            issue: Issue to convert.
            project_path: Project root for relative paths.

        Returns:
            Result object dictionary.
        """
        # Make path relative to project
        try:
            rel_path = issue.location.file_path.relative_to(project_path)
        except ValueError:
            rel_path = issue.location.file_path

        result: dict[str, Any] = {
            "ruleId": issue.rule_id,
            "level": self._severity_to_level(issue.severity),
            "message": {"text": issue.message},
            "locations": [
                {
                    "physicalLocation": {
                        "artifactLocation": {
                            "uri": str(rel_path).replace("\\", "/"),
                            "uriBaseId": "%SRCROOT%",
                        },
                        "region": {
                            "startLine": issue.location.start_line,
                            "endLine": issue.location.end_line,
                            "startColumn": issue.location.start_column + 1,  # SARIF is 1-indexed
                            "endColumn": issue.location.end_column + 1,
                        },
                    }
                }
            ],
            "partialFingerprints": {
                "primaryLocationLineHash": f"{issue.rule_id}:{rel_path}:{issue.location.start_line}",
            },
        }

        # Add snippet if available
        if issue.location.snippet:
            result["locations"][0]["physicalLocation"]["region"]["snippet"] = {
                "text": issue.location.snippet
            }

        # Add CWE info to taxonomies
        if issue.cwe_ids:
            result["taxa"] = [
                {
                    "toolComponent": {"name": "CWE"},
                    "id": str(cwe),
                }
                for cwe in issue.cwe_ids
            ]

        return result

    def _severity_to_level(self, severity: Severity) -> str:
        """Convert CodeScope severity to SARIF level.

        SARIF levels: error, warning, note, none

        Args:
            severity: CodeScope severity.

        Returns:
            SARIF level string.
        """
        mapping = {
            Severity.BLOCKER: "error",
            Severity.CRITICAL: "error",
            Severity.MAJOR: "warning",
            Severity.MINOR: "note",
            Severity.INFO: "note",
        }
        return mapping.get(severity, "warning")
