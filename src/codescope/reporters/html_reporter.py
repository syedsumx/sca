"""HTML report generator for CodeScope analysis results."""

from datetime import datetime
from pathlib import Path
from typing import Optional

from codescope.core.models import AnalysisResults


class HTMLReporter:
    """Generate HTML reports from analysis results."""

    def __init__(self, template_path: Optional[Path] = None):
        """Initialize HTML reporter."""
        self.template_path = template_path

    def generate(self, results: AnalysisResults, output_path: Optional[Path] = None) -> str:
        """Generate HTML report from analysis results."""
        html = self._render_html(results)

        if output_path:
            output_path.write_text(html, encoding='utf-8')

        return html

    def _render_html(self, results: AnalysisResults) -> str:
        """Render HTML from results."""
        # Issue counts by type
        bugs = sum(1 for i in results.all_issues if i.issue_type.value == 'BUG')
        vulns = sum(1 for i in results.all_issues if i.issue_type.value == 'VULNERABILITY')
        smells = sum(1 for i in results.all_issues if i.issue_type.value == 'CODE_SMELL')

        # Severity counts
        blockers = sum(1 for i in results.all_issues if i.severity.value == 'BLOCKER')
        criticals = sum(1 for i in results.all_issues if i.severity.value == 'CRITICAL')
        majors = sum(1 for i in results.all_issues if i.severity.value == 'MAJOR')
        minors = sum(1 for i in results.all_issues if i.severity.value == 'MINOR')
        infos = sum(1 for i in results.all_issues if i.severity.value == 'INFO')

        # Quality gate status
        qg_status = "PASSED"
        qg_class = "success"
        if results.quality_gate_status:
            qg_status = results.quality_gate_status.value
            qg_class = "success" if qg_status == "PASSED" else "danger" if qg_status == "FAILED" else "warning"

        # Generate issues table rows
        issues_rows = ""
        for issue in results.all_issues:
            sev_class = {
                "BLOCKER": "danger",
                "CRITICAL": "danger",
                "MAJOR": "warning",
                "MINOR": "info",
                "INFO": "secondary",
            }.get(issue.severity.value, "secondary")

            issues_rows += f"""
            <tr>
                <td><span class="badge bg-{sev_class}">{issue.severity.value}</span></td>
                <td>{issue.issue_type.value}</td>
                <td><code>{issue.rule_id}</code></td>
                <td>{issue.message}</td>
                <td><code>{issue.location.file_path}:{issue.location.start_line}</code></td>
            </tr>
            """

        # Generate file metrics table
        files_rows = ""
        for fm in results.file_metrics[:20]:  # Top 20 files
            files_rows += f"""
            <tr>
                <td><code>{fm.file_path}</code></td>
                <td>{fm.lines_of_code}</td>
                <td>{fm.complexity}</td>
                <td>{fm.functions}</td>
                <td>{fm.issues_count}</td>
            </tr>
            """

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CodeScope Analysis Report - {results.project_name}</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css" rel="stylesheet">
    <style>
        :root {{
            --primary-color: #1e40af;
            --success-color: #22c55e;
            --warning-color: #f59e0b;
            --danger-color: #dc2626;
        }}
        body {{
            background-color: #f8fafc;
        }}
        .navbar {{
            background: linear-gradient(135deg, var(--primary-color), #3b82f6);
        }}
        .metric-card {{
            border-radius: 12px;
            transition: transform 0.2s;
        }}
        .metric-card:hover {{
            transform: translateY(-2px);
        }}
        .metric-value {{
            font-size: 2.5rem;
            font-weight: 700;
        }}
        .quality-gate {{
            padding: 1.5rem;
            border-radius: 12px;
            text-align: center;
        }}
        .quality-gate.success {{
            background: linear-gradient(135deg, #dcfce7, #bbf7d0);
            border: 2px solid var(--success-color);
        }}
        .quality-gate.danger {{
            background: linear-gradient(135deg, #fee2e2, #fecaca);
            border: 2px solid var(--danger-color);
        }}
        .quality-gate.warning {{
            background: linear-gradient(135deg, #fef3c7, #fde68a);
            border: 2px solid var(--warning-color);
        }}
        .table-container {{
            background: white;
            border-radius: 12px;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        .severity-chart {{
            display: flex;
            height: 10px;
            border-radius: 5px;
            overflow: hidden;
            margin-top: 1rem;
        }}
        .severity-blocker {{ background-color: #991b1b; }}
        .severity-critical {{ background-color: #dc2626; }}
        .severity-major {{ background-color: #f59e0b; }}
        .severity-minor {{ background-color: #84cc16; }}
        .severity-info {{ background-color: #06b6d4; }}
        pre {{
            background-color: #1e293b;
            color: #e2e8f0;
            padding: 1rem;
            border-radius: 8px;
            font-size: 0.875rem;
        }}
        footer {{
            background-color: #1e293b;
            color: #94a3b8;
        }}
    </style>
</head>
<body>
    <nav class="navbar navbar-dark mb-4">
        <div class="container">
            <a class="navbar-brand" href="#">
                <i class="bi bi-shield-check me-2"></i>CodeScope Report
            </a>
            <span class="navbar-text">
                Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            </span>
        </div>
    </nav>

    <div class="container mb-5">
        <!-- Header -->
        <div class="row mb-4">
            <div class="col">
                <h1 class="h2 mb-1">{results.project_name}</h1>
                <p class="text-muted mb-0">
                    Analyzed {results.files_analyzed} files in {results.duration_seconds:.2f}s
                </p>
            </div>
            <div class="col-auto">
                <div class="quality-gate {qg_class}">
                    <h5 class="mb-1">Quality Gate</h5>
                    <span class="h3 fw-bold">{qg_status}</span>
                </div>
            </div>
        </div>

        <!-- Summary Metrics -->
        <div class="row g-4 mb-4">
            <div class="col-md-3">
                <div class="card metric-card h-100">
                    <div class="card-body">
                        <h6 class="text-muted mb-2">Total Issues</h6>
                        <div class="metric-value text-primary">{len(results.all_issues)}</div>
                        <div class="severity-chart">
                            <div class="severity-blocker" style="width: {blockers / max(len(results.all_issues), 1) * 100}%"></div>
                            <div class="severity-critical" style="width: {criticals / max(len(results.all_issues), 1) * 100}%"></div>
                            <div class="severity-major" style="width: {majors / max(len(results.all_issues), 1) * 100}%"></div>
                            <div class="severity-minor" style="width: {minors / max(len(results.all_issues), 1) * 100}%"></div>
                            <div class="severity-info" style="width: {infos / max(len(results.all_issues), 1) * 100}%"></div>
                        </div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card metric-card h-100">
                    <div class="card-body">
                        <h6 class="text-muted mb-2">
                            <i class="bi bi-bug text-danger me-1"></i>Bugs
                        </h6>
                        <div class="metric-value">{bugs}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card metric-card h-100">
                    <div class="card-body">
                        <h6 class="text-muted mb-2">
                            <i class="bi bi-shield-exclamation text-warning me-1"></i>Vulnerabilities
                        </h6>
                        <div class="metric-value">{vulns}</div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card metric-card h-100">
                    <div class="card-body">
                        <h6 class="text-muted mb-2">
                            <i class="bi bi-code-slash text-info me-1"></i>Code Smells
                        </h6>
                        <div class="metric-value">{smells}</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Severity Breakdown -->
        <div class="row g-4 mb-4">
            <div class="col-md-6">
                <div class="card h-100">
                    <div class="card-header">
                        <h5 class="mb-0">Issues by Severity</h5>
                    </div>
                    <div class="card-body">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span><span class="badge bg-danger">BLOCKER</span></span>
                            <span class="fw-bold">{blockers}</span>
                        </div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span><span class="badge bg-danger">CRITICAL</span></span>
                            <span class="fw-bold">{criticals}</span>
                        </div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span><span class="badge bg-warning text-dark">MAJOR</span></span>
                            <span class="fw-bold">{majors}</span>
                        </div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span><span class="badge bg-info">MINOR</span></span>
                            <span class="fw-bold">{minors}</span>
                        </div>
                        <div class="d-flex justify-content-between align-items-center">
                            <span><span class="badge bg-secondary">INFO</span></span>
                            <span class="fw-bold">{infos}</span>
                        </div>
                    </div>
                </div>
            </div>
            <div class="col-md-6">
                <div class="card h-100">
                    <div class="card-header">
                        <h5 class="mb-0">Project Metrics</h5>
                    </div>
                    <div class="card-body">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span>Files Analyzed</span>
                            <span class="fw-bold">{results.files_analyzed}</span>
                        </div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span>Lines of Code</span>
                            <span class="fw-bold">{sum(f.lines_of_code for f in results.file_metrics):,}</span>
                        </div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span>Functions</span>
                            <span class="fw-bold">{sum(f.functions for f in results.file_metrics):,}</span>
                        </div>
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span>Classes</span>
                            <span class="fw-bold">{sum(f.classes for f in results.file_metrics):,}</span>
                        </div>
                        <div class="d-flex justify-content-between align-items-center">
                            <span>Avg Complexity</span>
                            <span class="fw-bold">{sum(f.complexity for f in results.file_metrics) / max(len(results.file_metrics), 1):.1f}</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Issues Table -->
        <div class="table-container mb-4">
            <div class="card-header bg-white py-3">
                <h5 class="mb-0">
                    <i class="bi bi-list-check me-2"></i>All Issues ({len(results.all_issues)})
                </h5>
            </div>
            <div class="table-responsive">
                <table class="table table-hover mb-0">
                    <thead class="table-light">
                        <tr>
                            <th>Severity</th>
                            <th>Type</th>
                            <th>Rule</th>
                            <th>Message</th>
                            <th>Location</th>
                        </tr>
                    </thead>
                    <tbody>
                        {issues_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- File Metrics -->
        <div class="table-container mb-4">
            <div class="card-header bg-white py-3">
                <h5 class="mb-0">
                    <i class="bi bi-file-earmark-code me-2"></i>File Metrics
                </h5>
            </div>
            <div class="table-responsive">
                <table class="table table-hover mb-0">
                    <thead class="table-light">
                        <tr>
                            <th>File</th>
                            <th>Lines</th>
                            <th>Complexity</th>
                            <th>Functions</th>
                            <th>Issues</th>
                        </tr>
                    </thead>
                    <tbody>
                        {files_rows}
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <footer class="py-4 mt-5">
        <div class="container text-center">
            <p class="mb-0">Generated by CodeScope v0.1.0</p>
            <p class="small mb-0">Static Code Analysis Tool</p>
        </div>
    </footer>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>"""

        return html
