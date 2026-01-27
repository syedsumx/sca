"""Report generation module for CodeScope."""

from codescope.reporters.base import Reporter
from codescope.reporters.console import ConsoleReporter
from codescope.reporters.json_reporter import JSONReporter
from codescope.reporters.sarif import SARIFReporter
from codescope.reporters.html_reporter import HTMLReporter
from codescope.reporters.markdown_reporter import MarkdownReporter
from codescope.reporters.gitlab_reporter import GitLabReporter, SonarQubeReporter

__all__ = [
    "Reporter",
    "ConsoleReporter",
    "JSONReporter",
    "SARIFReporter",
    "HTMLReporter",
    "MarkdownReporter",
    "GitLabReporter",
    "SonarQubeReporter",
    "get_reporter",
]


def get_reporter(format_name: str) -> Reporter:
    """Get reporter by format name.

    Args:
        format_name: Format name (console, json, sarif, html, markdown, gitlab, sonarqube).

    Returns:
        Appropriate reporter instance.
    """
    reporters = {
        "console": ConsoleReporter,
        "json": JSONReporter,
        "sarif": SARIFReporter,
        "html": HTMLReporter,
        "markdown": MarkdownReporter,
        "md": MarkdownReporter,
        "gitlab": GitLabReporter,
        "sonarqube": SonarQubeReporter,
        "sonar": SonarQubeReporter,
    }

    reporter_class = reporters.get(format_name.lower(), ConsoleReporter)
    return reporter_class()
