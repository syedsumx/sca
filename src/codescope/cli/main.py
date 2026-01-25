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


if __name__ == "__main__":
    app()
