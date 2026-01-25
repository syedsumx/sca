"""Analysis orchestrator - coordinates the analysis process."""

from datetime import datetime
from pathlib import Path
from typing import Callable

from codescope.core.config import Config, load_config
from codescope.core.context import AnalysisContext
from codescope.core.enums import AnalysisStatus
from codescope.core.models import AnalysisResults, FileAnalysis
from codescope.metrics.calculator import MetricsCalculator
from codescope.parsers import get_parser
from codescope.parsers.models import ParsedFile
from codescope.rules import get_rules


class AnalysisOrchestrator:
    """Orchestrates the code analysis process."""

    def __init__(
        self,
        config: Config | None = None,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ):
        """Initialize the orchestrator.

        Args:
            config: Analysis configuration. Loads from file if not provided.
            progress_callback: Optional callback(file_path, current, total) for progress.
        """
        self.config = config or load_config()
        self.progress_callback = progress_callback
        self.metrics_calculator = MetricsCalculator()

    def analyze(self, path: Path) -> AnalysisResults:
        """Run analysis on a path (file or directory).

        Args:
            path: Path to analyze.

        Returns:
            AnalysisResults with all findings.
        """
        path = path.resolve()

        # Initialize context
        context = AnalysisContext(
            project_root=path if path.is_dir() else path.parent,
            config=self.config,
        )

        results = AnalysisResults(project_path=path)
        results.started_at = datetime.now()
        results.status = AnalysisStatus.RUNNING

        try:
            if path.is_file():
                files_to_analyze = [path]
            else:
                files_to_analyze = context.get_files_to_analyze()

            total_files = len(files_to_analyze)

            for i, file_path in enumerate(files_to_analyze):
                if self.progress_callback:
                    self.progress_callback(str(file_path), i + 1, total_files)

                file_analysis = self._analyze_file(file_path, context)
                if file_analysis:
                    results.files.append(file_analysis)

            # Calculate aggregate metrics
            results.calculate_metrics()
            results.status = AnalysisStatus.COMPLETED

        except Exception as e:
            results.status = AnalysisStatus.FAILED
            results.error_message = str(e)

        results.completed_at = datetime.now()
        return results

    def _analyze_file(self, file_path: Path, context: AnalysisContext) -> FileAnalysis | None:
        """Analyze a single file.

        Args:
            file_path: Path to the file.
            context: Analysis context.

        Returns:
            FileAnalysis or None if file couldn't be analyzed.
        """
        # Get parser for this file
        parser = get_parser(file_path)
        if not parser:
            return None

        try:
            # Parse the file
            parsed_file = parser.parse_file(file_path)

            # Create file analysis
            file_analysis = FileAnalysis(
                file_path=file_path,
                language=parsed_file.language,
            )

            # Run rules
            rules = get_rules(parsed_file.language)
            for rule in rules:
                # Check if rule is enabled
                if not context.is_rule_enabled(rule.id):
                    continue

                # Apply configured parameters
                params = context.get_rule_params(rule.id)
                if params:
                    rule.params.update(params)

                # Run the rule
                try:
                    result = rule.check(parsed_file)
                    file_analysis.issues.extend(result.issues)
                except Exception as e:
                    # Log error but continue with other rules
                    pass

            # Calculate metrics
            file_analysis.metrics = self.metrics_calculator.calculate_file_metrics(parsed_file)

            return file_analysis

        except Exception as e:
            # Could not analyze this file
            return None


def analyze_path(
    path: str | Path,
    config: Config | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
) -> AnalysisResults:
    """Convenience function to analyze a path.

    Args:
        path: Path to analyze (file or directory).
        config: Optional configuration.
        progress_callback: Optional progress callback.

    Returns:
        AnalysisResults with findings.
    """
    orchestrator = AnalysisOrchestrator(config, progress_callback)
    return orchestrator.analyze(Path(path))
