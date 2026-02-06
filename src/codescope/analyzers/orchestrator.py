"""Analysis orchestrator - coordinates the analysis process."""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Lock
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
        parallel: bool = True,
        max_workers: int | None = None,
    ):
        """Initialize the orchestrator.

        Args:
            config: Analysis configuration. Loads from file if not provided.
            progress_callback: Optional callback(file_path, current, total) for progress.
            parallel: Enable parallel file analysis (default: True).
            max_workers: Maximum number of worker threads. Defaults to CPU count.
        """
        self.config = config or load_config()
        self.progress_callback = progress_callback
        self.metrics_calculator = MetricsCalculator()
        self.parallel = parallel
        self.max_workers = max_workers or min(32, (os.cpu_count() or 1) + 4)
        self._progress_lock = Lock()
        self._files_processed = 0

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
            self._files_processed = 0

            if self.parallel and total_files > 1:
                # Parallel analysis for multiple files
                results.files = self._analyze_files_parallel(
                    files_to_analyze, context, total_files
                )
            else:
                # Sequential analysis for single file or when parallel is disabled
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

    def _analyze_files_parallel(
        self,
        files: list[Path],
        context: AnalysisContext,
        total_files: int,
    ) -> list[FileAnalysis]:
        """Analyze multiple files in parallel.

        Args:
            files: List of file paths to analyze.
            context: Analysis context.
            total_files: Total number of files for progress tracking.

        Returns:
            List of FileAnalysis results.
        """
        file_analyses: list[FileAnalysis] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            # Submit all files for analysis
            future_to_file = {
                executor.submit(self._analyze_file, file_path, context): file_path
                for file_path in files
            }

            # Collect results as they complete
            for future in as_completed(future_to_file):
                file_path = future_to_file[future]

                # Update progress
                with self._progress_lock:
                    self._files_processed += 1
                    if self.progress_callback:
                        self.progress_callback(
                            str(file_path),
                            self._files_processed,
                            total_files,
                        )

                try:
                    file_analysis = future.result()
                    if file_analysis:
                        file_analyses.append(file_analysis)
                except Exception:
                    # Individual file analysis failed, continue with others
                    pass

        return file_analyses

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
    parallel: bool = True,
    max_workers: int | None = None,
) -> AnalysisResults:
    """Convenience function to analyze a path.

    Args:
        path: Path to analyze (file or directory).
        config: Optional configuration.
        progress_callback: Optional progress callback.
        parallel: Enable parallel file analysis (default: True).
        max_workers: Maximum number of worker threads.

    Returns:
        AnalysisResults with findings.
    """
    orchestrator = AnalysisOrchestrator(
        config, progress_callback, parallel=parallel, max_workers=max_workers
    )
    return orchestrator.analyze(Path(path))
