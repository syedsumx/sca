"""Console reporter with rich formatting."""

from pathlib import Path

from codescope.core.enums import IssueType, Severity, QualityGateStatus
from codescope.core.models import AnalysisResults, Issue
from codescope.core.utils import format_duration
from codescope.reporters.base import Reporter


class ConsoleReporter(Reporter):
    """Reporter for console output with colors and formatting."""

    @property
    def format_name(self) -> str:
        return "console"

    def generate(self, results: AnalysisResults) -> str:
        """Generate console-formatted report."""
        lines = []

        # Header
        lines.append("")
        lines.append("=" * 70)
        lines.append("  CODESCOPE ANALYSIS REPORT")
        lines.append("=" * 70)
        lines.append("")

        # Project info
        lines.append(f"Project: {results.project_path}")
        lines.append(f"Status: {results.status.value}")

        if results.started_at and results.completed_at:
            duration = (results.completed_at - results.started_at).total_seconds()
            lines.append(f"Duration: {duration:.2f}s")

        lines.append(f"Files analyzed: {results.metrics.total_files}")
        lines.append(f"Lines of code: {results.metrics.total_lines_of_code}")
        lines.append("")

        # Summary metrics
        lines.append("-" * 70)
        lines.append("  METRICS SUMMARY")
        lines.append("-" * 70)
        lines.append("")

        metrics = results.metrics
        lines.append(f"  Reliability Rating:     {metrics.reliability_rating}  ({metrics.bugs_count} bugs)")
        lines.append(f"  Security Rating:        {metrics.security_rating}  ({metrics.vulnerabilities_count} vulnerabilities)")
        lines.append(f"  Maintainability Rating: {metrics.maintainability_rating}  ({metrics.code_smells_count} code smells)")
        lines.append("")

        debt_str = format_duration(metrics.technical_debt_minutes)
        lines.append(f"  Technical Debt: {debt_str} ({metrics.technical_debt_ratio:.1f}%)")
        lines.append(f"  Average Complexity: {metrics.average_complexity:.1f}")
        lines.append("")

        # Quality gate
        if results.quality_gate:
            lines.append("-" * 70)
            lines.append("  QUALITY GATE")
            lines.append("-" * 70)
            lines.append("")

            status = results.quality_gate.status
            status_symbol = "PASSED" if status == QualityGateStatus.PASSED else "FAILED"
            lines.append(f"  Status: {status_symbol}")
            lines.append("")

            for cond in results.quality_gate.conditions:
                symbol = "[OK]" if cond["passed"] else "[X]"
                lines.append(f"  {symbol} {cond['message']}")
            lines.append("")

        # Issues by severity
        issues = results.all_issues
        if issues:
            lines.append("-" * 70)
            lines.append(f"  ISSUES ({len(issues)} total)")
            lines.append("-" * 70)
            lines.append("")

            # Group by severity
            for severity in [Severity.BLOCKER, Severity.CRITICAL, Severity.MAJOR, Severity.MINOR, Severity.INFO]:
                severity_issues = [i for i in issues if i.severity == severity]
                if severity_issues:
                    lines.append(f"  {severity.value} ({len(severity_issues)})")
                    lines.append("")

                    for issue in severity_issues[:10]:  # Limit to 10 per severity
                        self._format_issue(issue, lines, results.project_path)

                    if len(severity_issues) > 10:
                        lines.append(f"    ... and {len(severity_issues) - 10} more {severity.value} issues")
                        lines.append("")

        # File summary
        lines.append("-" * 70)
        lines.append("  FILES WITH ISSUES")
        lines.append("-" * 70)
        lines.append("")

        files_with_issues = [(f, len(f.issues)) for f in results.files if f.issues]
        files_with_issues.sort(key=lambda x: -x[1])

        for file_analysis, count in files_with_issues[:20]:
            try:
                rel_path = file_analysis.file_path.relative_to(results.project_path)
            except ValueError:
                rel_path = file_analysis.file_path
            lines.append(f"  {rel_path}: {count} issues")

        if len(files_with_issues) > 20:
            lines.append(f"  ... and {len(files_with_issues) - 20} more files")

        lines.append("")
        lines.append("=" * 70)
        lines.append("")

        return "\n".join(lines)

    def _format_issue(self, issue: Issue, lines: list[str], project_path: Path) -> None:
        """Format a single issue for console output."""
        try:
            rel_path = issue.location.file_path.relative_to(project_path)
        except ValueError:
            rel_path = issue.location.file_path

        location = f"{rel_path}:{issue.location.start_line}"
        type_str = self._get_type_symbol(issue.issue_type)

        lines.append(f"    {type_str} [{issue.rule_id}] {issue.message}")
        lines.append(f"       at {location}")

        if issue.location.snippet:
            snippet_lines = issue.location.snippet.split("\n")
            for line in snippet_lines[:3]:
                lines.append(f"       {line}")

        lines.append("")

    def _get_type_symbol(self, issue_type: IssueType) -> str:
        """Get symbol for issue type."""
        symbols = {
            IssueType.BUG: "[BUG]",
            IssueType.VULNERABILITY: "[VUL]",
            IssueType.CODE_SMELL: "[SME]",
            IssueType.SECURITY_HOTSPOT: "[HOT]",
        }
        return symbols.get(issue_type, "[???]")
