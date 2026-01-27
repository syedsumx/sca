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


if __name__ == "__main__":
    app()
