"""Streaming analysis for large projects with memory constraints."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Generator, Iterator, Optional
import gc
import sys

from codescope.core.models import Issue, FileMetrics


@dataclass
class StreamConfig:
    """Configuration for streaming analysis."""

    batch_size: int = 100  # Files per batch
    max_memory_mb: int = 1024  # Trigger GC above this
    checkpoint_interval: int = 500  # Save progress every N files
    checkpoint_file: Optional[Path] = None


@dataclass
class StreamingResult:
    """Streaming result that processes files lazily."""

    files: list[Path]
    analyzer: Callable[[Path], tuple[list[Issue], FileMetrics]]
    config: StreamConfig = field(default_factory=StreamConfig)
    _processed_count: int = field(default=0, init=False)
    _total_issues: int = field(default=0, init=False)

    def __iter__(self) -> Iterator[tuple[Path, list[Issue], FileMetrics]]:
        """Iterate over analysis results.

        Yields:
            Tuple of (file_path, issues, metrics) for each file
        """
        for file_path in self.files:
            try:
                issues, metrics = self.analyzer(file_path)
                self._processed_count += 1
                self._total_issues += len(issues)

                yield file_path, issues, metrics

                # Check memory and run GC if needed
                if self._processed_count % self.config.batch_size == 0:
                    self._maybe_gc()

            except Exception as e:
                # Skip files that fail to analyze
                yield file_path, [], FileMetrics(file_path=str(file_path))

    def batches(
        self, batch_size: Optional[int] = None
    ) -> Generator[list[tuple[Path, list[Issue], FileMetrics]], None, None]:
        """Process files in batches.

        Args:
            batch_size: Files per batch (uses config default if not specified)

        Yields:
            List of (file_path, issues, metrics) tuples
        """
        batch_size = batch_size or self.config.batch_size
        batch: list[tuple[Path, list[Issue], FileMetrics]] = []

        for result in self:
            batch.append(result)

            if len(batch) >= batch_size:
                yield batch
                batch = []
                self._maybe_gc()

        # Yield remaining items
        if batch:
            yield batch

    @property
    def processed(self) -> int:
        """Number of files processed so far."""
        return self._processed_count

    @property
    def total_files(self) -> int:
        """Total number of files to process."""
        return len(self.files)

    @property
    def progress(self) -> float:
        """Progress as percentage (0-100)."""
        if not self.files:
            return 100.0
        return (self._processed_count / len(self.files)) * 100

    def _maybe_gc(self) -> None:
        """Run garbage collection if memory usage is high."""
        # Get memory usage in MB
        memory_mb = sys.getsizeof(gc.get_objects()) / (1024 * 1024)

        if memory_mb > self.config.max_memory_mb:
            gc.collect()


class StreamingAnalyzer:
    """Analyzer that processes files in a memory-efficient streaming manner."""

    def __init__(
        self,
        config: Optional[StreamConfig] = None,
    ):
        """Initialize streaming analyzer."""
        self.config = config or StreamConfig()

    def analyze_stream(
        self,
        files: list[Path],
        file_analyzer: Callable[[Path], tuple[list[Issue], FileMetrics]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> StreamingResult:
        """Create a streaming analysis result.

        Args:
            files: Files to analyze
            file_analyzer: Function to analyze a single file
            progress_callback: Optional callback(processed, total)

        Returns:
            StreamingResult that lazily processes files
        """
        return StreamingResult(
            files=files,
            analyzer=file_analyzer,
            config=self.config,
        )

    def collect_all(
        self,
        streaming_result: StreamingResult,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> tuple[list[Issue], list[FileMetrics]]:
        """Collect all results from a streaming result.

        Args:
            streaming_result: Streaming result to collect
            progress_callback: Optional callback(processed, total)

        Returns:
            Tuple of (all_issues, all_metrics)
        """
        all_issues: list[Issue] = []
        all_metrics: list[FileMetrics] = []

        for file_path, issues, metrics in streaming_result:
            all_issues.extend(issues)
            all_metrics.append(metrics)

            if progress_callback:
                progress_callback(
                    streaming_result.processed,
                    streaming_result.total_files
                )

        return all_issues, all_metrics

    def write_results_incrementally(
        self,
        streaming_result: StreamingResult,
        output_file: Path,
        format: str = 'jsonl',
    ) -> None:
        """Write results incrementally to avoid memory issues.

        Args:
            streaming_result: Streaming result to process
            output_file: File to write results to
            format: Output format ('jsonl' for JSON Lines)
        """
        import json

        resolved_output = Path(output_file).resolve()
        with open(resolved_output, 'w', encoding='utf-8') as out:
            for file_path, issues, metrics in streaming_result:
                for issue in issues:
                    record = json.dumps({
                        'type': 'issue',
                        'file': str(file_path),
                        'issue': {
                            'id': issue.id,
                            'rule_id': issue.rule_id,
                            'message': issue.message,
                            'severity': issue.severity.value,
                            'line': issue.location.start_line,
                        }
                    })
                    print(record, file=out)

                # Write metrics
                record = json.dumps({
                    'type': 'metrics',
                    'file': str(file_path),
                    'metrics': {
                        'lines_of_code': metrics.lines_of_code,
                        'complexity': metrics.complexity,
                        'issues': metrics.issues_count,
                    }
                })
                print(record, file=out)


def chunked_file_iterator(
    directory: Path,
    patterns: list[str],
    chunk_size: int = 100,
) -> Generator[list[Path], None, None]:
    """Iterate over files in a directory in chunks.

    Args:
        directory: Directory to scan
        patterns: Glob patterns to match (e.g., ['*.py', '*.js'])
        chunk_size: Number of files per chunk

    Yields:
        Lists of file paths
    """
    chunk: list[Path] = []

    for pattern in patterns:
        for file_path in directory.rglob(pattern):
            if not file_path.is_file():
                continue

            chunk.append(file_path)

            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []

    if chunk:
        yield chunk
