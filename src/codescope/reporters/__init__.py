"""Report generation module for CodeScope."""

from codescope.reporters.base import Reporter
from codescope.reporters.console import ConsoleReporter
from codescope.reporters.json_reporter import JSONReporter
from codescope.reporters.sarif import SARIFReporter

__all__ = [
    "Reporter",
    "ConsoleReporter",
    "JSONReporter",
    "SARIFReporter",
]


def get_reporter(format_name: str) -> Reporter:
    """Get reporter by format name.

    Args:
        format_name: Format name (console, json, sarif).

    Returns:
        Appropriate reporter instance.
    """
    reporters = {
        "console": ConsoleReporter,
        "json": JSONReporter,
        "sarif": SARIFReporter,
    }

    reporter_class = reporters.get(format_name.lower(), ConsoleReporter)
    return reporter_class()
