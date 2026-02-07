"""Main CLI entry point for CodeScope."""

import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from codescope.version import __version__

# Create app
app = typer.Typer(
    name="codescope",
    help="CodeScope - Software Composition Analysis Tool",
    add_completion=False,
)

console = Console()


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        console.print(f"CodeScope version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        help="Show version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """CodeScope - A powerful Software Composition Analysis tool."""
    pass


@app.command()
def scan(
    path: Path = typer.Argument(
        Path("."),
        help="Path to scan (file or directory).",
        exists=True,
    ),
    format: str = typer.Option(
        "console",
        "--format",
        "-f",
        help="Output format: console, json, sarif",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path.",
    ),
    quality_gate: Optional[str] = typer.Option(
        None,
        "--quality-gate",
        "-q",
        help="Apply quality gate (use 'default' for built-in gate).",
    ),
    config: Optional[Path] = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to configuration file.",
    ),
    verbose: bool = typer.Option(
        False,
        "--verbose",
        help="Enable verbose output.",
    ),
) -> None:
    """Analyze code for issues, vulnerabilities, and code smells."""
    from codescope.analyzers import analyze_path
    from codescope.core.config import load_config, QualityGateConfig, QualityGateCondition
    from codescope.core.enums import QualityGateStatus
    from codescope.quality_gates import evaluate_quality_gate
    from codescope.reporters import get_reporter

    # Load configuration
    cfg = load_config(config)

    # Progress tracking
    current_file = ""
    file_count = 0

    def progress_callback(file_path: str, current: int, total: int) -> None:
        nonlocal current_file, file_count
        current_file = file_path
        file_count = total

    # Run analysis with progress
    console.print()
    console.print("[bold]CodeScope[/bold] - Software Composition Analysis")
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Analyzing...", total=None)

        results = analyze_path(path, cfg, progress_callback)

        progress.update(task, description=f"Analyzed {len(results.files)} files")

    # Apply quality gate if requested
    if quality_gate:
        if quality_gate.lower() == "default":
            # Use default quality gate
            qg_config = QualityGateConfig(
                name="default",
                conditions=[
                    QualityGateCondition(metric="bugs", operator="GT", threshold=0),
                    QualityGateCondition(metric="vulnerabilities", operator="GT", threshold=0),
                ]
            )
        elif cfg.quality_gate:
            qg_config = cfg.quality_gate
        else:
            qg_config = QualityGateConfig(name=quality_gate, conditions=[])

        results.quality_gate = evaluate_quality_gate(results, qg_config)

    # Generate report
    reporter = get_reporter(format)
    report_content = reporter.generate(results)

    # Output report
    if output:
        output.write_text(report_content)
        console.print(f"Report written to: {output}")
    else:
        if format == "console":
            console.print(report_content)
        else:
            print(report_content)

    # Exit with appropriate code
    if results.quality_gate:
        if results.quality_gate.status == QualityGateStatus.FAILED:
            console.print("[red]Quality gate FAILED[/red]")
            raise typer.Exit(1)
        else:
            console.print("[green]Quality gate PASSED[/green]")

    # Exit with error if critical issues found
    if results.metrics.vulnerabilities_count > 0:
        raise typer.Exit(1)


@app.command()
def init(
    path: Path = typer.Argument(
        Path("."),
        help="Directory to initialize.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing configuration.",
    ),
) -> None:
    """Initialize a CodeScope configuration file."""
    from codescope.core.config import generate_default_config

    config_path = path / "codescope.yml"

    if config_path.exists() and not force:
        console.print(f"[yellow]Configuration already exists: {config_path}[/yellow]")
        console.print("Use --force to overwrite.")
        raise typer.Exit(1)

    config_path.write_text(generate_default_config())
    console.print(f"[green]Created configuration file: {config_path}[/green]")


@app.command(name="rules")
def list_rules(
    language: Optional[str] = typer.Option(
        None,
        "--language",
        "-l",
        help="Filter by language.",
    ),
    rule_type: Optional[str] = typer.Option(
        None,
        "--type",
        "-t",
        help="Filter by type: bug, vulnerability, code_smell, security_hotspot",
    ),
) -> None:
    """List available detection rules."""
    from rich.table import Table

    from codescope.rules import get_rules

    rules = get_rules(language)

    if rule_type:
        rules = [r for r in rules if r.issue_type.value.lower() == rule_type.lower()]

    table = Table(title="Available Rules")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="white")
    table.add_column("Type", style="yellow")
    table.add_column("Severity", style="red")
    table.add_column("Languages")

    for rule in sorted(rules, key=lambda r: r.id):
        table.add_row(
            rule.id,
            rule.name,
            rule.issue_type.value,
            rule.severity.value,
            ", ".join(rule.languages),
        )

    console.print(table)
    console.print(f"\nTotal: {len(rules)} rules")


@app.command()
def show(
    rule_id: str = typer.Argument(
        ...,
        help="Rule ID to show details for.",
    ),
) -> None:
    """Show details for a specific rule."""
    from rich.panel import Panel
    from rich.markdown import Markdown

    from codescope.rules import get_rule

    rule = get_rule(rule_id)

    if not rule:
        console.print(f"[red]Rule not found: {rule_id}[/red]")
        raise typer.Exit(1)

    # Build rule details
    details = f"""
## {rule.name}

**ID:** `{rule.id}`
**Type:** {rule.issue_type.value}
**Severity:** {rule.severity.value}
**Languages:** {', '.join(rule.languages)}
**Effort to fix:** {rule.effort_minutes} minutes

### Description
{rule.description}
"""

    if rule.cwe_ids:
        details += f"\n### CWE\n"
        for cwe in rule.cwe_ids:
            details += f"- [CWE-{cwe}](https://cwe.mitre.org/data/definitions/{cwe}.html)\n"

    if rule.owasp_categories:
        details += f"\n### OWASP\n"
        for owasp in rule.owasp_categories:
            details += f"- {owasp}\n"

    if rule.tags:
        details += f"\n### Tags\n"
        details += ", ".join(f"`{tag}`" for tag in rule.tags)

    console.print(Panel(Markdown(details), title=rule_id))


@app.command()
def duplications(
    path: Path = typer.Argument(
        Path("."),
        help="Path to analyze (file or directory).",
        exists=True,
    ),
    min_lines: int = typer.Option(
        6,
        "--min-lines",
        "-l",
        help="Minimum number of lines for a duplicate block.",
    ),
    min_tokens: int = typer.Option(
        50,
        "--min-tokens",
        "-t",
        help="Minimum number of tokens for a duplicate block.",
    ),
    format: str = typer.Option(
        "console",
        "--format",
        "-f",
        help="Output format: console, json",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path.",
    ),
    threshold: Optional[float] = typer.Option(
        None,
        "--threshold",
        help="Fail if duplication percentage exceeds threshold.",
    ),
) -> None:
    """Detect code duplications in the codebase."""
    import json
    from rich.table import Table
    from rich.panel import Panel

    from codescope.analyzers.duplication import analyze_duplication

    console.print()
    console.print("[bold]CodeScope[/bold] - Code Duplication Analysis")
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Analyzing duplications...", total=None)
        result = analyze_duplication(path, min_lines=min_lines, min_tokens=min_tokens)
        progress.update(task, description="Analysis complete")

    if format == "json":
        output_content = json.dumps(result.to_dict(), indent=2)
        if output:
            output.write_text(output_content)
            console.print(f"Report written to: {output}")
        else:
            print(output_content)
    else:
        # Console output
        # Summary
        summary_table = Table(title="Duplication Summary", show_header=False)
        summary_table.add_column("Metric", style="cyan")
        summary_table.add_column("Value", style="white")

        summary_table.add_row("Files Analyzed", str(result.files_analyzed))
        summary_table.add_row("Total Lines", str(result.total_lines))
        summary_table.add_row("Duplicated Lines", str(result.duplicated_lines))

        dup_pct = result.duplication_percentage
        pct_color = "green" if dup_pct < 3 else "yellow" if dup_pct < 10 else "red"
        summary_table.add_row("Duplication %", f"[{pct_color}]{dup_pct:.1f}%[/{pct_color}]")
        summary_table.add_row("Duplicate Blocks", str(result.duplication_count))

        console.print(summary_table)
        console.print()

        # Show top duplications
        if result.duplications:
            console.print("[bold]Top Duplications:[/bold]")
            console.print()

            for i, dup in enumerate(result.duplications[:10], 1):
                console.print(f"[cyan]#{i}[/cyan] - {dup.duplicated_lines} lines duplicated in {len(dup.blocks)} locations:")
                for block in dup.blocks[:5]:
                    console.print(f"   • {block.file_path}:{block.start_line}-{block.end_line}")
                if len(dup.blocks) > 5:
                    console.print(f"   ... and {len(dup.blocks) - 5} more")
                console.print()

        if output:
            output.write_text(json.dumps(result.to_dict(), indent=2))
            console.print(f"Detailed report written to: {output}")

    # Check threshold
    if threshold is not None:
        if result.duplication_percentage > threshold:
            console.print(f"[red]FAILED: Duplication {result.duplication_percentage:.1f}% exceeds threshold {threshold}%[/red]")
            raise typer.Exit(1)
        else:
            console.print(f"[green]PASSED: Duplication {result.duplication_percentage:.1f}% is below threshold {threshold}%[/green]")


@app.command()
def dependencies(
    path: Path = typer.Argument(
        Path("."),
        help="Path to scan for dependencies.",
        exists=True,
    ),
    format: str = typer.Option(
        "console",
        "--format",
        "-f",
        help="Output format: console, json",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path.",
    ),
    no_check: bool = typer.Option(
        False,
        "--no-check",
        help="Skip online vulnerability check.",
    ),
    fail_on: Optional[str] = typer.Option(
        None,
        "--fail-on",
        help="Fail if vulnerabilities of this severity or higher exist (critical, high, medium, low).",
    ),
) -> None:
    """Scan dependencies for known vulnerabilities."""
    import json
    from rich.table import Table

    from codescope.analyzers.dependencies import scan_dependencies

    console.print()
    console.print("[bold]CodeScope[/bold] - Dependency Vulnerability Scanner")
    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scanning dependencies...", total=None)
        result = scan_dependencies(path, check_vulnerabilities=not no_check)
        progress.update(task, description="Scan complete")

    if format == "json":
        output_content = json.dumps(result.to_dict(), indent=2)
        if output:
            output.write_text(output_content)
            console.print(f"Report written to: {output}")
        else:
            print(output_content)
    else:
        # Console output
        # Summary table
        summary_table = Table(title="Dependency Scan Summary", show_header=False)
        summary_table.add_column("Metric", style="cyan")
        summary_table.add_column("Value", style="white")

        summary_table.add_row("Files Scanned", str(len(result.files_scanned)))
        summary_table.add_row("Total Dependencies", str(result.total_dependencies))
        summary_table.add_row("Vulnerable Dependencies", str(len(result.vulnerable_dependencies)))
        summary_table.add_row("Total Vulnerabilities", str(result.vulnerability_count))

        if result.critical_count > 0:
            summary_table.add_row("Critical", f"[red]{result.critical_count}[/red]")
        if result.high_count > 0:
            summary_table.add_row("High", f"[orange1]{result.high_count}[/orange1]")
        if result.medium_count > 0:
            summary_table.add_row("Medium", f"[yellow]{result.medium_count}[/yellow]")
        if result.low_count > 0:
            summary_table.add_row("Low", f"[blue]{result.low_count}[/blue]")

        console.print(summary_table)
        console.print()

        # Vulnerable dependencies
        if result.vulnerable_dependencies:
            vuln_table = Table(title="Vulnerable Dependencies")
            vuln_table.add_column("Package", style="cyan")
            vuln_table.add_column("Version", style="white")
            vuln_table.add_column("Ecosystem", style="dim")
            vuln_table.add_column("Severity", style="red")
            vuln_table.add_column("Vulnerability", style="yellow")
            vuln_table.add_column("Fixed In", style="green")

            for dep in result.vulnerable_dependencies:
                for vuln in dep.vulnerabilities:
                    severity_color = {
                        "CRITICAL": "red",
                        "HIGH": "orange1",
                        "MEDIUM": "yellow",
                        "LOW": "blue",
                    }.get(vuln.severity, "white")

                    vuln_table.add_row(
                        dep.name,
                        dep.version or "unknown",
                        dep.ecosystem,
                        f"[{severity_color}]{vuln.severity}[/{severity_color}]",
                        f"{vuln.id}: {vuln.title[:50]}...",
                        vuln.fixed_version or "N/A",
                    )

            console.print(vuln_table)
        else:
            console.print("[green]No vulnerable dependencies found![/green]")

        # Show scanned files
        console.print()
        console.print("[dim]Scanned files:[/dim]")
        for f in result.files_scanned:
            console.print(f"  [dim]• {f}[/dim]")

        if output:
            output.write_text(json.dumps(result.to_dict(), indent=2))
            console.print(f"\nDetailed report written to: {output}")

    # Check fail condition
    if fail_on:
        severity_levels = ["low", "medium", "high", "critical"]
        fail_level = fail_on.lower()
        if fail_level in severity_levels:
            fail_index = severity_levels.index(fail_level)
            counts = [result.low_count, result.medium_count, result.high_count, result.critical_count]
            if any(counts[fail_index:]):
                console.print(f"[red]FAILED: Found vulnerabilities of severity {fail_on} or higher[/red]")
                raise typer.Exit(1)
            else:
                console.print(f"[green]PASSED: No vulnerabilities of severity {fail_on} or higher[/green]")


@app.command()
def coverage(
    report_path: Path = typer.Argument(
        ...,
        help="Path to coverage report file.",
        exists=True,
    ),
    format: Optional[str] = typer.Option(
        None,
        "--format",
        "-f",
        help="Coverage format: cobertura, lcov, coverage.py, jacoco, clover",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path for JSON report.",
    ),
    threshold: Optional[float] = typer.Option(
        None,
        "--threshold",
        "-t",
        help="Fail if coverage is below threshold percentage.",
    ),
    show_uncovered: bool = typer.Option(
        False,
        "--show-uncovered",
        help="Show uncovered line numbers for each file.",
    ),
) -> None:
    """Parse and display code coverage reports."""
    import json
    from rich.table import Table

    from codescope.analyzers.coverage import parse_coverage

    console.print()
    console.print("[bold]CodeScope[/bold] - Code Coverage Analysis")
    console.print()

    try:
        report = parse_coverage(report_path, format=format)
    except ValueError as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(1)

    summary = report.summary

    # Summary table
    summary_table = Table(title="Coverage Summary", show_header=False)
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", style="white")

    summary_table.add_row("Format", report.format)
    summary_table.add_row("Files", str(summary.total_files))
    summary_table.add_row("Total Lines", str(summary.total_lines))
    summary_table.add_row("Covered Lines", str(summary.covered_lines))
    summary_table.add_row("Uncovered Lines", str(summary.uncovered_lines))

    pct = summary.line_coverage_percent
    pct_color = "green" if pct >= 80 else "yellow" if pct >= 50 else "red"
    summary_table.add_row("Line Coverage", f"[{pct_color}]{pct:.1f}%[/{pct_color}]")

    if summary.branch_coverage_percent is not None:
        branch_pct = summary.branch_coverage_percent
        branch_color = "green" if branch_pct >= 80 else "yellow" if branch_pct >= 50 else "red"
        summary_table.add_row("Branch Coverage", f"[{branch_color}]{branch_pct:.1f}%[/{branch_color}]")

    rating_color = {"A": "green", "B": "bright_green", "C": "yellow", "D": "orange1", "E": "red"}.get(summary.coverage_rating, "white")
    summary_table.add_row("Rating", f"[{rating_color}]{summary.coverage_rating}[/{rating_color}]")

    console.print(summary_table)
    console.print()

    # Files with low coverage
    low_coverage = report.low_coverage_files
    if low_coverage:
        files_table = Table(title="Files with Low Coverage (<50%)")
        files_table.add_column("File", style="cyan")
        files_table.add_column("Lines", style="white")
        files_table.add_column("Covered", style="green")
        files_table.add_column("Coverage", style="red")

        for file_cov in sorted(low_coverage, key=lambda f: f.line_coverage_percent)[:15]:
            files_table.add_row(
                file_cov.file_path[-60:],
                str(file_cov.total_lines),
                str(file_cov.covered_lines),
                f"{file_cov.line_coverage_percent:.1f}%",
            )

        console.print(files_table)

        if show_uncovered:
            console.print()
            console.print("[bold]Uncovered Lines:[/bold]")
            for file_cov in sorted(low_coverage, key=lambda f: f.line_coverage_percent)[:10]:
                uncovered = file_cov.uncovered_line_numbers
                if uncovered:
                    lines_str = ", ".join(str(n) for n in uncovered[:20])
                    if len(uncovered) > 20:
                        lines_str += f" ... ({len(uncovered) - 20} more)"
                    console.print(f"  [cyan]{file_cov.file_path}[/cyan]: {lines_str}")

    if output:
        output.write_text(json.dumps(report.to_dict(), indent=2))
        console.print(f"\nDetailed report written to: {output}")

    # Check threshold
    if threshold is not None:
        if summary.line_coverage_percent < threshold:
            console.print(f"\n[red]FAILED: Coverage {summary.line_coverage_percent:.1f}% is below threshold {threshold}%[/red]")
            raise typer.Exit(1)
        else:
            console.print(f"\n[green]PASSED: Coverage {summary.line_coverage_percent:.1f}% meets threshold {threshold}%[/green]")


@app.command(name="ai-vet")
def ai_vet(
    path: Path = typer.Argument(
        Path("."),
        help="Path to scan for AI-generated code issues.",
        exists=True,
    ),
    format: str = typer.Option(
        "console",
        "--format",
        "-f",
        help="Output format: console, json",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path.",
    ),
    min_confidence: float = typer.Option(
        0.5,
        "--min-confidence",
        help="Minimum confidence threshold (0.0-1.0).",
    ),
    category: Optional[str] = typer.Option(
        None,
        "--category",
        "-c",
        help="Filter by category: placeholder, hallucination, security, quality, incomplete, overengineered, license_risk",
    ),
    fail_on_risk: Optional[str] = typer.Option(
        None,
        "--fail-on-risk",
        help="Fail if risk level is this or higher: LOW, MEDIUM, HIGH",
    ),
) -> None:
    """Vet code for AI-generated code issues (hallucinations, placeholders, security)."""
    import json as json_mod
    from rich.table import Table
    from rich.panel import Panel

    from codescope.analyzers.aivetting import AIVettingAnalyzer, PatternCategory

    console.print()
    console.print("[bold]CodeScope[/bold] - AI Code Vetting")
    console.print()

    analyzer = AIVettingAnalyzer(min_confidence=min_confidence)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Vetting code for AI patterns...", total=None)

        def on_progress(current: int, total: int) -> None:
            progress.update(task, description=f"Scanning file {current}/{total}...")

        report = analyzer.analyze_directory(path, progress_callback=on_progress)
        progress.update(task, description="Vetting complete")

    findings = report.findings

    # Filter by category if requested
    if category:
        findings = [f for f in findings if f.category == category]

    if format == "json":
        data = {
            "files_scanned": report.files_scanned,
            "total_findings": len(findings),
            "risk_score": round(report.risk_score, 1),
            "risk_level": report.risk_level,
            "summary": report.summary,
            "findings": [
                {
                    "pattern_id": f.pattern_id,
                    "name": f.name,
                    "category": f.category,
                    "severity": f.severity,
                    "message": f.message,
                    "file_path": f.file_path,
                    "start_line": f.start_line,
                    "end_line": f.end_line,
                    "confidence": f.confidence,
                }
                for f in findings
            ],
        }
        output_content = json_mod.dumps(data, indent=2)
        if output:
            output.write_text(output_content)
            console.print(f"Report written to: {output}")
        else:
            print(output_content)
    else:
        # Risk score banner
        risk = report.risk_level
        risk_color = {"NONE": "green", "LOW": "blue", "MEDIUM": "yellow", "HIGH": "red"}.get(risk, "white")
        console.print(Panel(
            f"[bold {risk_color}]Risk Level: {risk}[/bold {risk_color}]  |  "
            f"Score: {report.risk_score:.1f}/100  |  "
            f"Files: {report.files_scanned}  |  "
            f"Findings: {len(findings)}",
            title="AI Code Vetting Results",
        ))
        console.print()

        # Summary by category
        summary_table = Table(title="Findings by Category")
        summary_table.add_column("Category", style="cyan")
        summary_table.add_column("Count", style="white", justify="right")

        category_names = {
            "placeholder": "Placeholder / Stub Code",
            "hallucination": "Hallucinated APIs / Imports",
            "security": "Security Issues",
            "quality": "Code Quality Issues",
            "incomplete": "Incomplete / Truncated Code",
            "overengineered": "Over-Engineered Patterns",
            "license_risk": "License Risk",
        }
        for cat in PatternCategory:
            count = sum(1 for f in findings if f.category == cat.value)
            if count > 0:
                summary_table.add_row(category_names.get(cat.value, cat.value), str(count))

        console.print(summary_table)
        console.print()

        # Findings by severity
        severity_order = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]
        severity_colors = {
            "BLOCKER": "red bold",
            "CRITICAL": "red",
            "MAJOR": "yellow",
            "MINOR": "blue",
            "INFO": "dim",
        }

        for severity in severity_order:
            sev_findings = [f for f in findings if f.severity == severity]
            if not sev_findings:
                continue

            findings_table = Table(title=f"{severity} ({len(sev_findings)})")
            findings_table.add_column("#", style="dim", width=4)
            findings_table.add_column("Pattern", style="cyan", width=28)
            findings_table.add_column("File", style="white", width=40)
            findings_table.add_column("Line", style="dim", width=6)
            findings_table.add_column("Message", style=severity_colors.get(severity, "white"))

            for idx, f in enumerate(sev_findings[:25], 1):
                fpath = f.file_path
                if len(fpath) > 40:
                    fpath = "..." + fpath[-37:]
                findings_table.add_row(
                    str(idx),
                    f.name,
                    fpath,
                    str(f.start_line),
                    f.message[:80],
                )

            console.print(findings_table)
            console.print()

            if len(sev_findings) > 25:
                console.print(f"  [dim]... and {len(sev_findings) - 25} more {severity} findings[/dim]")
                console.print()

        if output:
            data = {
                "files_scanned": report.files_scanned,
                "total_findings": len(findings),
                "risk_score": round(report.risk_score, 1),
                "risk_level": report.risk_level,
                "summary": report.summary,
                "findings": [
                    {
                        "pattern_id": f.pattern_id,
                        "name": f.name,
                        "category": f.category,
                        "severity": f.severity,
                        "message": f.message,
                        "file_path": f.file_path,
                        "start_line": f.start_line,
                        "end_line": f.end_line,
                        "confidence": f.confidence,
                    }
                    for f in findings
                ],
            }
            output.write_text(json_mod.dumps(data, indent=2))
            console.print(f"Detailed report written to: {output}")

    # Check fail condition
    if fail_on_risk:
        risk_levels = ["LOW", "MEDIUM", "HIGH"]
        fail_level = fail_on_risk.upper()
        if fail_level in risk_levels:
            current_index = risk_levels.index(report.risk_level) if report.risk_level in risk_levels else -1
            fail_index = risk_levels.index(fail_level)
            if current_index >= fail_index:
                console.print(f"[red]FAILED: Risk level {report.risk_level} meets or exceeds threshold {fail_level}[/red]")
                raise typer.Exit(1)
            else:
                console.print(f"[green]PASSED: Risk level {report.risk_level} is below threshold {fail_level}[/green]")


@app.command()
def secrets(
    path: Path = typer.Argument(
        Path("."),
        help="Path to scan for secrets.",
        exists=True,
    ),
    format: str = typer.Option(
        "console",
        "--format",
        "-f",
        help="Output format: console, json",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path.",
    ),
    scan_git_history: bool = typer.Option(
        False,
        "--git-history",
        "-g",
        help="Also scan git commit history.",
    ),
    max_commits: int = typer.Option(
        50,
        "--max-commits",
        help="Maximum commits to scan in git history.",
    ),
    fail_on: Optional[str] = typer.Option(
        None,
        "--fail-on",
        help="Fail if secrets of this severity or higher exist (critical, major, minor).",
    ),
) -> None:
    """Scan for hardcoded secrets, API keys, tokens, and credentials."""
    import json as json_mod
    from rich.table import Table
    from rich.panel import Panel

    from codescope.analyzers.secrets.scanner import SecretScanner

    console.print()
    console.print("[bold]CodeScope[/bold] - Secret Scanner")
    console.print()

    scanner = SecretScanner()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scanning for secrets...", total=None)
        findings = scanner.scan_directory(path)

        if scan_git_history:
            progress.update(task, description="Scanning git history...")
            git_findings = scanner.scan_git_history(path, max_commits=max_commits)
            findings.extend(git_findings)

        progress.update(task, description="Scan complete")

    # Aggregate by severity
    by_severity: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_rule[f.rule_name] = by_rule.get(f.rule_name, 0) + 1

    if format == "json":
        data = {
            "total": len(findings),
            "by_severity": by_severity,
            "by_rule": by_rule,
            "findings": [f.to_dict() for f in findings],
        }
        output_content = json_mod.dumps(data, indent=2)
        if output:
            output.write_text(output_content)
            console.print(f"Report written to: {output}")
        else:
            print(output_content)
    else:
        # Console output
        severity_color = {
            "CRITICAL": "red bold",
            "MAJOR": "yellow",
            "MINOR": "blue",
            "INFO": "dim",
        }

        # Summary banner
        total = len(findings)
        critical = by_severity.get("CRITICAL", 0)
        major = by_severity.get("MAJOR", 0)

        status_color = "red" if critical > 0 else "yellow" if major > 0 else "green"
        console.print(Panel(
            f"[bold {status_color}]Secrets Found: {total}[/bold {status_color}]  |  "
            f"[red]Critical: {critical}[/red]  |  "
            f"[yellow]Major: {major}[/yellow]",
            title="Secret Scan Results",
        ))
        console.print()

        if findings:
            # Summary by type
            summary_table = Table(title="Findings by Type")
            summary_table.add_column("Secret Type", style="cyan")
            summary_table.add_column("Count", style="white", justify="right")

            for rule_name, count in sorted(by_rule.items(), key=lambda x: -x[1]):
                summary_table.add_row(rule_name, str(count))

            console.print(summary_table)
            console.print()

            # Detailed findings
            findings_table = Table(title="Secret Findings")
            findings_table.add_column("#", style="dim", width=4)
            findings_table.add_column("Severity", width=10)
            findings_table.add_column("Type", style="cyan", width=25)
            findings_table.add_column("File", style="white", width=40)
            findings_table.add_column("Line", style="dim", width=6)

            for idx, f in enumerate(findings[:50], 1):
                sev_color = severity_color.get(f.severity, "white")
                fpath = f.file
                if len(fpath) > 40:
                    fpath = "..." + fpath[-37:]
                findings_table.add_row(
                    str(idx),
                    f"[{sev_color}]{f.severity}[/{sev_color}]",
                    f.rule_name,
                    fpath,
                    str(f.line),
                )

            console.print(findings_table)

            if len(findings) > 50:
                console.print(f"\n[dim]... and {len(findings) - 50} more findings[/dim]")
        else:
            console.print("[green]No secrets found![/green]")

        if output:
            data = {
                "total": len(findings),
                "by_severity": by_severity,
                "by_rule": by_rule,
                "findings": [f.to_dict() for f in findings],
            }
            output.write_text(json_mod.dumps(data, indent=2))
            console.print(f"\nDetailed report written to: {output}")

    # Check fail condition
    if fail_on:
        severity_levels = ["minor", "major", "critical"]
        fail_level = fail_on.lower()
        if fail_level in severity_levels:
            fail_index = severity_levels.index(fail_level)
            counts = [
                by_severity.get("MINOR", 0),
                by_severity.get("MAJOR", 0),
                by_severity.get("CRITICAL", 0),
            ]
            if any(counts[fail_index:]):
                console.print(f"[red]FAILED: Found secrets of severity {fail_on} or higher[/red]")
                raise typer.Exit(1)
            else:
                console.print(f"[green]PASSED: No secrets of severity {fail_on} or higher[/green]")


@app.command()
def iac(
    path: Path = typer.Argument(
        Path("."),
        help="Path to scan for IaC files.",
        exists=True,
    ),
    format: str = typer.Option(
        "console",
        "--format",
        "-f",
        help="Output format: console, json",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path.",
    ),
    platform: Optional[str] = typer.Option(
        None,
        "--platform",
        "-p",
        help="Filter by platform: terraform, cloudformation, kubernetes, helm, arm, bicep",
    ),
    fail_on: Optional[str] = typer.Option(
        None,
        "--fail-on",
        help="Fail if findings of this severity or higher exist (critical, high, medium, low).",
    ),
) -> None:
    """Scan Infrastructure-as-Code for security misconfigurations."""
    import json as json_mod
    from rich.table import Table
    from rich.panel import Panel

    from codescope.iac import IaCScanner

    console.print()
    console.print("[bold]CodeScope[/bold] - Infrastructure-as-Code Scanner")
    console.print()

    scanner = IaCScanner()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scanning IaC files...", total=None)
        result = scanner.scan(path)
        progress.update(task, description="Scan complete")

    findings = result.findings

    # Filter by platform if specified
    if platform:
        platform_upper = platform.upper()
        findings = [f for f in findings if platform_upper in f.platform.value]

    # Aggregate by severity and platform
    by_severity: dict[str, int] = {}
    by_platform: dict[str, int] = {}
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_platform[f.platform.value] = by_platform.get(f.platform.value, 0) + 1

    if format == "json":
        data = {
            "total": len(findings),
            "by_severity": by_severity,
            "by_platform": by_platform,
            "findings": [f.to_dict() for f in findings],
        }
        output_content = json_mod.dumps(data, indent=2)
        if output:
            output.write_text(output_content)
            console.print(f"Report written to: {output}")
        else:
            print(output_content)
    else:
        # Console output
        severity_color = {
            "CRITICAL": "red bold",
            "HIGH": "red",
            "MEDIUM": "yellow",
            "LOW": "blue",
            "INFO": "dim",
        }

        # Summary banner
        total = len(findings)
        critical = by_severity.get("CRITICAL", 0)
        high = by_severity.get("HIGH", 0)

        status_color = "red" if critical > 0 else "yellow" if high > 0 else "green"
        console.print(Panel(
            f"[bold {status_color}]IaC Findings: {total}[/bold {status_color}]  |  "
            f"[red]Critical: {critical}[/red]  |  "
            f"[red]High: {high}[/red]  |  "
            f"[yellow]Medium: {by_severity.get('MEDIUM', 0)}[/yellow]",
            title="IaC Scan Results",
        ))
        console.print()

        if findings:
            # Summary by platform
            if by_platform:
                platform_table = Table(title="Findings by Platform")
                platform_table.add_column("Platform", style="cyan")
                platform_table.add_column("Count", style="white", justify="right")

                for plat, count in sorted(by_platform.items(), key=lambda x: -x[1]):
                    platform_table.add_row(plat, str(count))

                console.print(platform_table)
                console.print()

            # Detailed findings by severity
            for severity in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
                sev_findings = [f for f in findings if f.severity == severity]
                if not sev_findings:
                    continue

                sev_color = severity_color.get(severity, "white")
                findings_table = Table(title=f"{severity} ({len(sev_findings)})")
                findings_table.add_column("#", style="dim", width=4)
                findings_table.add_column("Rule ID", style="cyan", width=12)
                findings_table.add_column("Title", width=35)
                findings_table.add_column("File", style="white", width=35)
                findings_table.add_column("Line", style="dim", width=6)

                for idx, f in enumerate(sev_findings[:20], 1):
                    fpath = f.file_path
                    if len(fpath) > 35:
                        fpath = "..." + fpath[-32:]
                    findings_table.add_row(
                        str(idx),
                        f.rule_id,
                        f.title[:35],
                        fpath,
                        str(f.line),
                    )

                console.print(findings_table)

                if len(sev_findings) > 20:
                    console.print(f"  [dim]... and {len(sev_findings) - 20} more {severity} findings[/dim]")
                console.print()
        else:
            console.print("[green]No IaC security issues found![/green]")

        if output:
            data = {
                "total": len(findings),
                "by_severity": by_severity,
                "by_platform": by_platform,
                "findings": [f.to_dict() for f in findings],
            }
            output.write_text(json_mod.dumps(data, indent=2))
            console.print(f"Detailed report written to: {output}")

    # Check fail condition
    if fail_on:
        severity_levels = ["low", "medium", "high", "critical"]
        fail_level = fail_on.lower()
        if fail_level in severity_levels:
            fail_index = severity_levels.index(fail_level)
            counts = [
                by_severity.get("LOW", 0),
                by_severity.get("MEDIUM", 0),
                by_severity.get("HIGH", 0),
                by_severity.get("CRITICAL", 0),
            ]
            if any(counts[fail_index:]):
                console.print(f"[red]FAILED: Found IaC issues of severity {fail_on} or higher[/red]")
                raise typer.Exit(1)
            else:
                console.print(f"[green]PASSED: No IaC issues of severity {fail_on} or higher[/green]")


@app.command()
def fix(
    path: Path = typer.Argument(
        Path("."),
        help="Path to analyze for fix suggestions.",
        exists=True,
    ),
    format: str = typer.Option(
        "console",
        "--format",
        "-f",
        help="Output format: console, json",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        "-a",
        help="Apply safe fixes automatically.",
    ),
    rule_id: Optional[str] = typer.Option(
        None,
        "--rule",
        "-r",
        help="Filter by specific rule ID.",
    ),
) -> None:
    """Get fix recommendations and optionally auto-fix issues."""
    import json as json_mod
    from rich.table import Table
    from rich.panel import Panel
    from rich.syntax import Syntax

    from codescope.analyzers import analyze_path as run_analysis
    from codescope.remediation import RemediationEngine
    from codescope.autofix import AutoFixEngine

    console.print()
    console.print("[bold]CodeScope[/bold] - Fix Recommendations")
    console.print()

    # First, run analysis
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Analyzing code...", total=None)
        results = run_analysis(path)
        progress.update(task, description="Generating fix suggestions...")

    # Collect all issues
    all_issues = []
    for file_analysis in results.files:
        all_issues.extend(file_analysis.issues)

    if rule_id:
        all_issues = [i for i in all_issues if rule_id.lower() in i.rule_id.lower()]

    if not all_issues:
        console.print("[green]No issues found that need fixing![/green]")
        return

    # Get remediations
    remediation_engine = RemediationEngine()
    autofix_engine = AutoFixEngine()

    # Generate fix suggestions
    fix_result = autofix_engine.suggest_fixes(all_issues)

    # Match issues with remediations
    issues_with_fixes = []
    for issue in all_issues:
        remediation = remediation_engine.get(issue.rule_id)
        suggestion = next(
            (s for s in fix_result.suggestions if s.rule_id == issue.rule_id and s.start_line == issue.location.start_line),
            None
        )
        issues_with_fixes.append({
            "issue": issue,
            "remediation": remediation,
            "suggestion": suggestion,
        })

    if format == "json":
        data = {
            "total_issues": len(all_issues),
            "fixable_issues": len([i for i in issues_with_fixes if i["suggestion"]]),
            "recommendations": [
                {
                    "rule_id": item["issue"].rule_id,
                    "message": item["issue"].message,
                    "file": str(item["issue"].location.file_path),
                    "line": item["issue"].location.start_line,
                    "remediation": item["remediation"].to_dict() if item["remediation"] else None,
                    "auto_fix": item["suggestion"].to_dict() if item["suggestion"] else None,
                }
                for item in issues_with_fixes
            ],
        }
        output_content = json_mod.dumps(data, indent=2)
        if output:
            output.write_text(output_content)
            console.print(f"Report written to: {output}")
        else:
            print(output_content)
    else:
        # Console output
        fixable_count = len([i for i in issues_with_fixes if i["suggestion"] and i["suggestion"].is_safe_to_apply])
        has_remediation = len([i for i in issues_with_fixes if i["remediation"]])

        console.print(Panel(
            f"[bold]Issues Found: {len(all_issues)}[/bold]  |  "
            f"[green]Auto-Fixable: {fixable_count}[/green]  |  "
            f"[cyan]With Guidance: {has_remediation}[/cyan]",
            title="Fix Recommendations",
        ))
        console.print()

        # Group by severity/type for display
        displayed = 0
        for item in issues_with_fixes[:20]:
            issue = item["issue"]
            remediation = item["remediation"]
            suggestion = item["suggestion"]

            displayed += 1
            console.print(f"[bold cyan]#{displayed}[/bold cyan] [yellow]{issue.rule_id}[/yellow] - {issue.message[:60]}")
            console.print(f"   [dim]File:[/dim] {issue.location.file_path}:{issue.location.start_line}")

            if remediation:
                console.print(f"   [bold]Recommendation:[/bold] {remediation.title}")
                console.print(f"   [dim]{remediation.description[:100]}...[/dim]")

                if remediation.fix_example:
                    console.print("   [bold green]Fix Example:[/bold green]")
                    console.print(Syntax(remediation.fix_example, "python", line_numbers=False, theme="monokai"))

                if remediation.references:
                    console.print(f"   [dim]References: {', '.join(remediation.references[:2])}[/dim]")

            if suggestion and suggestion.is_safe_to_apply:
                console.print(f"   [bold green]✓ Auto-fixable[/bold green] (Confidence: {suggestion.confidence:.0%})")

            console.print()

        if len(issues_with_fixes) > 20:
            console.print(f"[dim]... and {len(issues_with_fixes) - 20} more issues[/dim]")

        # Apply fixes if requested
        if apply and fixable_count > 0:
            console.print()
            console.print("[bold]Applying safe fixes...[/bold]")
            apply_result = autofix_engine.apply_fixes(fix_result.suggestions, safe_only=True)
            console.print(f"[green]Applied {apply_result.applied_count} fixes[/green]")
            if apply_result.skipped_count > 0:
                console.print(f"[yellow]Skipped {apply_result.skipped_count} fixes (not safe to auto-apply)[/yellow]")

        if output:
            data = {
                "total_issues": len(all_issues),
                "fixable_issues": fixable_count,
                "recommendations": [
                    {
                        "rule_id": item["issue"].rule_id,
                        "message": item["issue"].message,
                        "file": str(item["issue"].location.file_path),
                        "line": item["issue"].location.start_line,
                        "remediation": item["remediation"].to_dict() if item["remediation"] else None,
                        "auto_fix": item["suggestion"].to_dict() if item["suggestion"] else None,
                    }
                    for item in issues_with_fixes
                ],
            }
            output.write_text(json_mod.dumps(data, indent=2))
            console.print(f"\nDetailed report written to: {output}")


@app.command()
def server(
    host: str = typer.Option(
        "0.0.0.0",
        "--host",
        "-h",
        help="Host to bind to.",
    ),
    port: int = typer.Option(
        8000,
        "--port",
        "-p",
        help="Port to bind to.",
    ),
    reload: bool = typer.Option(
        False,
        "--reload",
        "-r",
        help="Enable auto-reload for development.",
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        "-w",
        help="Number of worker processes.",
    ),
) -> None:
    """Start the CodeScope REST API server."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Error: uvicorn is required for the server.[/red]")
        console.print("Install it with: pip install uvicorn")
        raise typer.Exit(1)

    console.print()
    console.print("[bold]CodeScope[/bold] - REST API Server")
    console.print()
    console.print(f"Starting server on http://{host}:{port}")
    console.print("API docs available at http://{host}:{port}/api/docs")
    console.print()

    uvicorn.run(
        "codescope.api.app:app",
        host=host,
        port=port,
        reload=reload,
        workers=workers if not reload else 1,
    )


# ── Issue Tracker Commands ──────────────────────────────────────────────


tracker_app = typer.Typer(
    name="tracker",
    help="Manage issue tracker integrations (Jira, Azure Boards).",
)
app.add_typer(tracker_app, name="tracker")


@tracker_app.command(name="list")
def tracker_list() -> None:
    """List configured issue tracker integrations."""
    from rich.table import Table

    from codescope.integrations.issue_trackers import get_configured_trackers
    from codescope.integrations.issue_trackers.registry import get_available_providers

    console.print()
    console.print("[bold]CodeScope[/bold] - Issue Tracker Integrations")
    console.print()

    # Show available providers
    providers = get_available_providers()
    console.print("[dim]Available providers:[/dim]")
    for p in providers:
        console.print(f"  • {p['name']}")
    console.print()

    # Show configured trackers
    trackers = get_configured_trackers()

    if not trackers:
        console.print("[yellow]No issue trackers configured.[/yellow]")
        console.print()
        console.print("Configure trackers using environment variables:")
        console.print("  [cyan]Jira:[/cyan]")
        console.print("    CODESCOPE_JIRA_URL=https://your-domain.atlassian.net")
        console.print("    CODESCOPE_JIRA_PROJECT=PROJ")
        console.print("    CODESCOPE_JIRA_TOKEN=your-api-token")
        console.print("    CODESCOPE_JIRA_USERNAME=your-email@example.com")
        console.print()
        console.print("  [cyan]Azure Boards:[/cyan]")
        console.print("    CODESCOPE_AZURE_BOARDS_ORGANIZATION=your-org")
        console.print("    CODESCOPE_AZURE_BOARDS_PROJECT=your-project")
        console.print("    CODESCOPE_AZURE_BOARDS_TOKEN=your-pat")
        return

    table = Table(title="Configured Issue Trackers")
    table.add_column("Provider", style="cyan")
    table.add_column("Project", style="white")
    table.add_column("URL", style="dim")
    table.add_column("Status", style="green")

    for tracker in trackers:
        is_connected, message = tracker.test_connection()
        status = "[green]Connected[/green]" if is_connected else f"[red]Error: {message}[/red]"
        table.add_row(
            tracker.display_name,
            tracker.config.project_key,
            tracker.config.base_url or tracker.config.organization,
            status,
        )

    console.print(table)


@tracker_app.command(name="test")
def tracker_test(
    provider: str = typer.Argument(
        ...,
        help="Provider name: jira, azure_boards",
    ),
) -> None:
    """Test connection to an issue tracker."""
    from codescope.integrations.issue_trackers import get_issue_tracker

    console.print()
    console.print(f"[bold]Testing connection to {provider}...[/bold]")

    tracker = get_issue_tracker(provider)
    if not tracker:
        console.print(f"[red]Issue tracker '{provider}' is not configured.[/red]")
        console.print()
        console.print("Set the required environment variables:")
        if provider == "jira":
            console.print("  CODESCOPE_JIRA_URL, CODESCOPE_JIRA_PROJECT, CODESCOPE_JIRA_TOKEN")
        elif provider == "azure_boards":
            console.print("  CODESCOPE_AZURE_BOARDS_ORGANIZATION, CODESCOPE_AZURE_BOARDS_PROJECT, CODESCOPE_AZURE_BOARDS_TOKEN")
        raise typer.Exit(1)

    is_connected, message = tracker.test_connection()

    if is_connected:
        console.print(f"[green]Success: {message}[/green]")
    else:
        console.print(f"[red]Failed: {message}[/red]")
        raise typer.Exit(1)


@tracker_app.command(name="create")
def tracker_create_issue(
    provider: str = typer.Argument(
        ...,
        help="Provider name: jira, azure_boards",
    ),
    title: str = typer.Option(
        ...,
        "--title",
        "-t",
        help="Issue title.",
    ),
    description: str = typer.Option(
        "",
        "--description",
        "-d",
        help="Issue description.",
    ),
    issue_type: str = typer.Option(
        "Bug",
        "--type",
        help="Issue type (e.g., Bug, Task, Story).",
    ),
    priority: str = typer.Option(
        "medium",
        "--priority",
        "-p",
        help="Priority: highest, high, medium, low, lowest",
    ),
    labels: Optional[str] = typer.Option(
        None,
        "--labels",
        "-l",
        help="Comma-separated labels.",
    ),
) -> None:
    """Create an issue in the tracker."""
    from codescope.integrations.issue_trackers import get_issue_tracker, IssuePriority
    from codescope.integrations.issue_trackers.base import CreateIssueRequest

    console.print()

    tracker = get_issue_tracker(provider)
    if not tracker:
        console.print(f"[red]Issue tracker '{provider}' is not configured.[/red]")
        raise typer.Exit(1)

    # Parse priority
    try:
        issue_priority = IssuePriority(priority.lower())
    except ValueError:
        issue_priority = IssuePriority.MEDIUM

    # Parse labels
    label_list = []
    if labels:
        label_list = [l.strip() for l in labels.split(",") if l.strip()]

    request = CreateIssueRequest(
        title=title,
        description=description,
        priority=issue_priority,
        issue_type=issue_type,
        labels=label_list,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Creating issue...", total=None)
        issue = tracker.create_issue(request)
        progress.update(task, description="Done")

    if issue:
        console.print(f"[green]Issue created successfully![/green]")
        console.print()
        console.print(f"  [cyan]Key:[/cyan] {issue.key}")
        console.print(f"  [cyan]Title:[/cyan] {issue.title}")
        console.print(f"  [cyan]URL:[/cyan] {issue.url}")
    else:
        console.print("[red]Failed to create issue.[/red]")
        raise typer.Exit(1)


@tracker_app.command(name="search")
def tracker_search(
    provider: str = typer.Argument(
        ...,
        help="Provider name: jira, azure_boards",
    ),
    query: Optional[str] = typer.Option(
        None,
        "--query",
        "-q",
        help="Search query.",
    ),
    status: Optional[str] = typer.Option(
        None,
        "--status",
        "-s",
        help="Filter by status: open, in_progress, resolved, closed",
    ),
    max_results: int = typer.Option(
        20,
        "--max",
        "-n",
        help="Maximum results to return.",
    ),
    format: str = typer.Option(
        "console",
        "--format",
        "-f",
        help="Output format: console, json",
    ),
) -> None:
    """Search for issues in the tracker."""
    import json as json_mod
    from rich.table import Table

    from codescope.integrations.issue_trackers import get_issue_tracker, IssueStatus

    console.print()

    tracker = get_issue_tracker(provider)
    if not tracker:
        console.print(f"[red]Issue tracker '{provider}' is not configured.[/red]")
        raise typer.Exit(1)

    # Parse status
    issue_status = None
    if status:
        try:
            issue_status = IssueStatus(status.lower())
        except ValueError:
            console.print(f"[red]Invalid status: {status}[/red]")
            raise typer.Exit(1)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Searching issues...", total=None)
        issues = tracker.search_issues(
            query=query,
            status=issue_status,
            max_results=max_results,
        )
        progress.update(task, description="Done")

    if format == "json":
        data = {"issues": [i.to_dict() for i in issues]}
        print(json_mod.dumps(data, indent=2, default=str))
    else:
        if not issues:
            console.print("[yellow]No issues found.[/yellow]")
            return

        table = Table(title=f"CodeScope Issues in {tracker.display_name}")
        table.add_column("Key", style="cyan", width=12)
        table.add_column("Title", style="white", width=50)
        table.add_column("Status", width=12)
        table.add_column("Priority", width=10)
        table.add_column("Assignee", style="dim", width=15)

        status_colors = {
            "open": "yellow",
            "in_progress": "blue",
            "resolved": "green",
            "closed": "dim",
            "reopened": "red",
        }

        priority_colors = {
            "highest": "red bold",
            "high": "red",
            "medium": "yellow",
            "low": "blue",
            "lowest": "dim",
        }

        for issue in issues:
            status_color = status_colors.get(issue.status.value, "white")
            priority_color = priority_colors.get(issue.priority.value, "white")

            title = issue.title
            if len(title) > 50:
                title = title[:47] + "..."

            table.add_row(
                issue.key,
                title,
                f"[{status_color}]{issue.status.value}[/{status_color}]",
                f"[{priority_color}]{issue.priority.value}[/{priority_color}]",
                issue.assignee[:15] if issue.assignee else "",
            )

        console.print(table)
        console.print(f"\nTotal: {len(issues)} issues")


@tracker_app.command(name="link")
def tracker_link_issues(
    provider: str = typer.Argument(
        ...,
        help="Provider name: jira, azure_boards",
    ),
    issue_ids: str = typer.Argument(
        ...,
        help="Comma-separated CodeScope issue IDs to create tracker issues for.",
    ),
    issue_type: str = typer.Option(
        "Bug",
        "--type",
        help="Issue type for created issues.",
    ),
    dashboard_url: str = typer.Option(
        "",
        "--dashboard-url",
        help="Base URL for CodeScope dashboard links.",
    ),
) -> None:
    """Create tracker issues for CodeScope analysis issues."""
    from codescope.integrations.issue_trackers import get_issue_tracker

    console.print()
    console.print("[bold]CodeScope[/bold] - Link Issues to Tracker")
    console.print()

    tracker = get_issue_tracker(provider)
    if not tracker:
        console.print(f"[red]Issue tracker '{provider}' is not configured.[/red]")
        raise typer.Exit(1)

    ids = [id.strip() for id in issue_ids.split(",") if id.strip()]

    if not ids:
        console.print("[yellow]No issue IDs provided.[/yellow]")
        raise typer.Exit(1)

    console.print(f"Creating {len(ids)} issues in {tracker.display_name}...")
    console.print()

    # In a full implementation, we would fetch the CodeScope issues from storage
    # and create corresponding tracker issues. For now, show a placeholder.
    console.print("[yellow]Note: Full issue linking requires CodeScope issue storage integration.[/yellow]")
    console.print()
    console.print("Issue IDs to link:")
    for id in ids:
        console.print(f"  • {id}")


if __name__ == "__main__":
    app()
