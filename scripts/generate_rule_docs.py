#!/usr/bin/env python3
"""Auto-generate rule documentation from code.

This script scans all registered rules and generates comprehensive
markdown documentation including:
- Rule catalog with filtering
- CWE and OWASP mappings
- Language-specific rule lists
- Severity and effort summaries

Usage:
    python scripts/generate_rule_docs.py
    python scripts/generate_rule_docs.py --output docs/RULES.md
    python scripts/generate_rule_docs.py --format html --output docs/rules.html
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from codescope.rules.registry import RuleRegistry
from codescope.core.enums import Severity, IssueType


def get_severity_badge(severity: Severity) -> str:
    """Get markdown badge for severity level."""
    colors = {
        Severity.BLOCKER: "red",
        Severity.CRITICAL: "orange",
        Severity.MAJOR: "yellow",
        Severity.MINOR: "blue",
        Severity.INFO: "gray",
    }
    color = colors.get(severity, "gray")
    return f"![{severity.value}](https://img.shields.io/badge/-{severity.value}-{color})"


def get_type_icon(issue_type: IssueType) -> str:
    """Get icon for issue type."""
    icons = {
        IssueType.BUG: "🐛",
        IssueType.VULNERABILITY: "🔓",
        IssueType.CODE_SMELL: "👃",
        IssueType.SECURITY_HOTSPOT: "🔥",
    }
    return icons.get(issue_type, "📋")


def generate_rule_entry(rule: Any) -> str:
    """Generate markdown entry for a single rule."""
    lines = []

    # Rule header
    lines.append(f"### {rule.id}")
    lines.append("")
    lines.append(f"**{rule.name}** {get_type_icon(rule.issue_type)}")
    lines.append("")

    # Metadata table
    lines.append("| Property | Value |")
    lines.append("|----------|-------|")
    lines.append(f"| Severity | {get_severity_badge(rule.severity)} |")
    lines.append(f"| Type | {rule.issue_type.value} |")
    lines.append(f"| Languages | {', '.join(rule.languages)} |")
    lines.append(f"| Effort | {rule.effort_minutes} minutes |")

    if rule.cwe_ids:
        cwe_links = [f"[CWE-{cwe}](https://cwe.mitre.org/data/definitions/{cwe}.html)" for cwe in rule.cwe_ids]
        lines.append(f"| CWE | {', '.join(cwe_links)} |")

    if rule.owasp_categories:
        lines.append(f"| OWASP | {', '.join(rule.owasp_categories)} |")

    if rule.tags:
        lines.append(f"| Tags | {', '.join(f'`{tag}`' for tag in rule.tags)} |")

    lines.append("")

    # Description
    lines.append("**Description:**")
    lines.append("")
    lines.append(rule.description)
    lines.append("")

    # Parameters if any
    if hasattr(rule, 'default_params') and rule.default_params:
        lines.append("**Parameters:**")
        lines.append("")
        lines.append("| Parameter | Default |")
        lines.append("|-----------|---------|")
        for param, value in rule.default_params.items():
            lines.append(f"| `{param}` | `{value}` |")
        lines.append("")

    lines.append("---")
    lines.append("")

    return "\n".join(lines)


def generate_markdown_docs(rules: dict[str, Any]) -> str:
    """Generate complete markdown documentation."""
    lines = []

    # Header
    lines.append("# CodeScope Rule Catalog")
    lines.append("")
    lines.append(f"*Auto-generated on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
    lines.append("")
    lines.append("This document contains all rules available in CodeScope, organized by language and category.")
    lines.append("")

    # Table of Contents
    lines.append("## Table of Contents")
    lines.append("")
    lines.append("- [Summary](#summary)")
    lines.append("- [Rules by Language](#rules-by-language)")
    lines.append("- [Rules by Severity](#rules-by-severity)")
    lines.append("- [Security Rules (OWASP/CWE)](#security-rules)")
    lines.append("- [All Rules](#all-rules)")
    lines.append("")

    # Summary statistics
    lines.append("## Summary")
    lines.append("")

    # Count by severity
    severity_counts = defaultdict(int)
    type_counts = defaultdict(int)
    language_counts = defaultdict(int)
    total_effort = 0

    for rule in rules.values():
        severity_counts[rule.severity] += 1
        type_counts[rule.issue_type] += 1
        total_effort += rule.effort_minutes
        for lang in rule.languages:
            language_counts[lang] += 1

    lines.append(f"**Total Rules:** {len(rules)}")
    lines.append("")
    lines.append(f"**Total Remediation Effort:** {total_effort} minutes ({total_effort // 60} hours)")
    lines.append("")

    lines.append("### By Severity")
    lines.append("")
    lines.append("| Severity | Count |")
    lines.append("|----------|-------|")
    for severity in Severity:
        count = severity_counts.get(severity, 0)
        lines.append(f"| {get_severity_badge(severity)} | {count} |")
    lines.append("")

    lines.append("### By Type")
    lines.append("")
    lines.append("| Type | Count |")
    lines.append("|------|-------|")
    for issue_type in IssueType:
        count = type_counts.get(issue_type, 0)
        lines.append(f"| {get_type_icon(issue_type)} {issue_type.value} | {count} |")
    lines.append("")

    lines.append("### By Language")
    lines.append("")
    lines.append("| Language | Rules |")
    lines.append("|----------|-------|")
    for lang, count in sorted(language_counts.items()):
        lines.append(f"| {lang} | {count} |")
    lines.append("")

    # Rules by language
    lines.append("## Rules by Language")
    lines.append("")

    rules_by_language = defaultdict(list)
    for rule in rules.values():
        for lang in rule.languages:
            rules_by_language[lang].append(rule)

    for lang in sorted(rules_by_language.keys()):
        lang_rules = rules_by_language[lang]
        lines.append(f"### {lang.title()}")
        lines.append("")
        lines.append(f"*{len(lang_rules)} rules*")
        lines.append("")
        lines.append("| Rule ID | Name | Severity | Type |")
        lines.append("|---------|------|----------|------|")
        for rule in sorted(lang_rules, key=lambda r: r.id):
            lines.append(f"| [{rule.id}](#{rule.id.replace(':', '').lower()}) | {rule.name} | {rule.severity.value} | {get_type_icon(rule.issue_type)} {rule.issue_type.value} |")
        lines.append("")

    # Rules by severity
    lines.append("## Rules by Severity")
    lines.append("")

    rules_by_severity = defaultdict(list)
    for rule in rules.values():
        rules_by_severity[rule.severity].append(rule)

    for severity in Severity:
        sev_rules = rules_by_severity.get(severity, [])
        if sev_rules:
            lines.append(f"### {severity.value}")
            lines.append("")
            lines.append(f"*{len(sev_rules)} rules*")
            lines.append("")
            for rule in sorted(sev_rules, key=lambda r: r.id):
                lines.append(f"- [{rule.id}](#{rule.id.replace(':', '').lower()}) - {rule.name}")
            lines.append("")

    # Security rules with CWE/OWASP
    lines.append("## Security Rules")
    lines.append("")
    lines.append("Rules mapped to CWE and OWASP categories for compliance reporting.")
    lines.append("")

    # CWE mapping
    lines.append("### CWE Mapping")
    lines.append("")

    cwe_mapping = defaultdict(list)
    for rule in rules.values():
        for cwe in rule.cwe_ids:
            cwe_mapping[cwe].append(rule)

    if cwe_mapping:
        lines.append("| CWE | Description | Rules |")
        lines.append("|-----|-------------|-------|")

        cwe_descriptions = {
            22: "Path Traversal",
            78: "OS Command Injection",
            79: "Cross-site Scripting (XSS)",
            89: "SQL Injection",
            90: "LDAP Injection",
            94: "Code Injection",
            95: "Eval Injection",
            117: "Log Injection",
            200: "Information Exposure",
            295: "Improper Certificate Validation",
            311: "Missing Encryption",
            312: "Cleartext Storage",
            326: "Weak Encryption",
            327: "Broken Crypto Algorithm",
            328: "Weak Hash",
            330: "Insufficient Randomness",
            338: "Weak PRNG",
            352: "CSRF",
            400: "Uncontrolled Resource Consumption",
            434: "Unrestricted Upload",
            502: "Deserialization",
            601: "Open Redirect",
            611: "XXE",
            614: "Sensitive Cookie without Secure",
            798: "Hard-coded Credentials",
            918: "SSRF",
        }

        for cwe in sorted(cwe_mapping.keys()):
            desc = cwe_descriptions.get(cwe, "See CWE database")
            rule_ids = ", ".join(f"`{r.id}`" for r in cwe_mapping[cwe])
            lines.append(f"| [CWE-{cwe}](https://cwe.mitre.org/data/definitions/{cwe}.html) | {desc} | {rule_ids} |")
        lines.append("")

    # OWASP mapping
    lines.append("### OWASP Top 10 Mapping")
    lines.append("")

    owasp_mapping = defaultdict(list)
    for rule in rules.values():
        for owasp in rule.owasp_categories:
            owasp_mapping[owasp].append(rule)

    if owasp_mapping:
        owasp_descriptions = {
            "A01:2021": "Broken Access Control",
            "A02:2021": "Cryptographic Failures",
            "A03:2021": "Injection",
            "A04:2021": "Insecure Design",
            "A05:2021": "Security Misconfiguration",
            "A06:2021": "Vulnerable Components",
            "A07:2021": "Authentication Failures",
            "A08:2021": "Software/Data Integrity Failures",
            "A09:2021": "Logging/Monitoring Failures",
            "A10:2021": "SSRF",
        }

        lines.append("| OWASP | Category | Rules |")
        lines.append("|-------|----------|-------|")
        for owasp in sorted(owasp_mapping.keys()):
            desc = owasp_descriptions.get(owasp, owasp)
            rule_ids = ", ".join(f"`{r.id}`" for r in owasp_mapping[owasp])
            lines.append(f"| {owasp} | {desc} | {rule_ids} |")
        lines.append("")

    # All rules detailed
    lines.append("## All Rules")
    lines.append("")

    for rule_id in sorted(rules.keys()):
        rule = rules[rule_id]
        lines.append(generate_rule_entry(rule))

    return "\n".join(lines)


def generate_html_docs(rules: dict[str, Any]) -> str:
    """Generate HTML documentation."""
    # For brevity, we'll generate basic HTML wrapping the markdown
    import html

    md_content = generate_markdown_docs(rules)

    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CodeScope Rule Catalog</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5/github-markdown.min.css">
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        .markdown-body { max-width: 980px; margin: 0 auto; padding: 45px; }
        @media (max-width: 767px) { .markdown-body { padding: 15px; } }
    </style>
</head>
<body>
    <article class="markdown-body" id="content"></article>
    <script>
        const markdown = {markdown_content};
        document.getElementById('content').innerHTML = marked.parse(markdown);
    </script>
</body>
</html>"""

    import json
    return html_template.replace("{markdown_content}", json.dumps(md_content))


def main():
    parser = argparse.ArgumentParser(description="Generate rule documentation from code")
    parser.add_argument(
        "--output", "-o",
        default="docs/RULES.md",
        help="Output file path (default: docs/RULES.md)"
    )
    parser.add_argument(
        "--format", "-f",
        choices=["markdown", "html"],
        default="markdown",
        help="Output format (default: markdown)"
    )
    args = parser.parse_args()

    # Initialize registry and get all rules
    print("Loading rules from registry...")
    registry = RuleRegistry()
    rules = registry.get_all_rules()

    print(f"Found {len(rules)} rules")

    # Generate documentation
    if args.format == "markdown":
        content = generate_markdown_docs(rules)
    else:
        content = generate_html_docs(rules)

    # Write output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)

    print(f"Documentation written to {output_path}")


if __name__ == "__main__":
    main()
