"""GitLab Code Quality report generator."""

import json
from pathlib import Path
from typing import Optional
import hashlib

from codescope.core.models import AnalysisResults


class GitLabReporter:
    """Generate GitLab Code Quality compatible JSON reports.

    This format integrates with GitLab's Code Quality feature
    to display issues in merge requests.
    """

    def generate(self, results: AnalysisResults, output_path: Optional[Path] = None) -> str:
        """Generate GitLab Code Quality report."""
        report = self._build_report(results)
        json_output = json.dumps(report, indent=2)

        if output_path:
            output_path.write_text(json_output, encoding='utf-8')

        return json_output

    def _build_report(self, results: AnalysisResults) -> list[dict]:
        """Build GitLab Code Quality report structure."""
        issues = []

        for issue in results.issues:
            # Generate fingerprint for deduplication
            fingerprint = hashlib.md5(
                f"{issue.rule_id}:{issue.location.file_path}:{issue.location.start_line}:{issue.message}".encode()
            ).hexdigest()

            gl_issue = {
                "description": issue.message,
                "check_name": issue.rule_id,
                "fingerprint": fingerprint,
                "severity": self._map_severity(issue.severity.value),
                "location": {
                    "path": issue.location.file_path,
                    "lines": {
                        "begin": issue.location.start_line,
                        "end": issue.location.end_line,
                    }
                }
            }

            # Add optional fields
            if issue.location.start_column:
                gl_issue["location"]["positions"] = {
                    "begin": {
                        "line": issue.location.start_line,
                        "column": issue.location.start_column,
                    }
                }

            # Add categories based on issue type
            categories = []
            if issue.issue_type.value == 'VULNERABILITY':
                categories.append("Security")
            elif issue.issue_type.value == 'BUG':
                categories.append("Bug Risk")
            elif issue.issue_type.value == 'CODE_SMELL':
                categories.append("Style")
                categories.append("Clarity")

            if categories:
                gl_issue["categories"] = categories

            issues.append(gl_issue)

        return issues

    def _map_severity(self, severity: str) -> str:
        """Map CodeScope severity to GitLab severity."""
        mapping = {
            "BLOCKER": "blocker",
            "CRITICAL": "critical",
            "MAJOR": "major",
            "MINOR": "minor",
            "INFO": "info",
        }
        return mapping.get(severity, "info")


class SonarQubeReporter:
    """Generate SonarQube Generic Issue Data format.

    This format can be imported into SonarQube as external issues.
    """

    def generate(self, results: AnalysisResults, output_path: Optional[Path] = None) -> str:
        """Generate SonarQube Generic Issue Data report."""
        report = self._build_report(results)
        json_output = json.dumps(report, indent=2)

        if output_path:
            output_path.write_text(json_output, encoding='utf-8')

        return json_output

    def _build_report(self, results: AnalysisResults) -> dict:
        """Build SonarQube Generic Issue Data structure."""
        issues = []

        for issue in results.issues:
            sq_issue = {
                "engineId": "codescope",
                "ruleId": issue.rule_id,
                "severity": self._map_severity(issue.severity.value),
                "type": self._map_type(issue.issue_type.value),
                "primaryLocation": {
                    "message": issue.message,
                    "filePath": issue.location.file_path,
                    "textRange": {
                        "startLine": issue.location.start_line,
                        "endLine": issue.location.end_line,
                    }
                }
            }

            # Add column info if available
            if issue.location.start_column:
                sq_issue["primaryLocation"]["textRange"]["startColumn"] = issue.location.start_column

            if issue.location.end_column:
                sq_issue["primaryLocation"]["textRange"]["endColumn"] = issue.location.end_column

            # Add effort if available
            if issue.effort_minutes:
                sq_issue["effortMinutes"] = issue.effort_minutes

            issues.append(sq_issue)

        return {"issues": issues}

    def _map_severity(self, severity: str) -> str:
        """Map CodeScope severity to SonarQube severity."""
        mapping = {
            "BLOCKER": "BLOCKER",
            "CRITICAL": "CRITICAL",
            "MAJOR": "MAJOR",
            "MINOR": "MINOR",
            "INFO": "INFO",
        }
        return mapping.get(severity, "INFO")

    def _map_type(self, issue_type: str) -> str:
        """Map CodeScope issue type to SonarQube type."""
        mapping = {
            "BUG": "BUG",
            "VULNERABILITY": "VULNERABILITY",
            "CODE_SMELL": "CODE_SMELL",
            "SECURITY_HOTSPOT": "SECURITY_HOTSPOT",
        }
        return mapping.get(issue_type, "CODE_SMELL")
