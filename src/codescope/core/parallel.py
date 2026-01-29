"""Parallel processing utilities for CodeScope."""

import multiprocessing
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar
import os

T = TypeVar('T')


@dataclass
class ParallelConfig:
    """Configuration for parallel processing."""

    max_workers: Optional[int] = None
    use_processes: bool = True  # False for threads
    chunk_size: int = 10
    timeout: Optional[float] = None

    @property
    def workers(self) -> int:
        """Get actual number of workers to use."""
        if self.max_workers:
            return self.max_workers
        # Use CPU count - 1 to leave one core free
        return max(1, (os.cpu_count() or 4) - 1)


class ParallelProcessor:
    """Process items in parallel using process or thread pools."""

    def __init__(self, config: Optional[ParallelConfig] = None):
        """Initialize parallel processor."""
        self.config = config or ParallelConfig()

    def run_parallel(
        self,
        func: Callable[[T], Any],
        items: list[T],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> list[Any]:
        """Apply a function to items in parallel.

        Args:
            func: Function to apply to each item
            items: Items to process
            progress_callback: Optional callback(completed, total)

        Returns:
            List of results in the same order as items
        """
        if not items:
            return []

        # For small batches, don't bother with parallelism
        if len(items) <= 2:
            results = []
            for i, item in enumerate(items):
                results.append(func(item))
                if progress_callback:
                    progress_callback(i + 1, len(items))
            return results

        # Choose executor type
        executor_class = ProcessPoolExecutor if self.config.use_processes else ThreadPoolExecutor

        results = [None] * len(items)
        completed = 0

        with executor_class(max_workers=self.config.workers) as executor:
            # Submit all tasks
            future_to_index = {
                executor.submit(func, item): i
                for i, item in enumerate(items)
            }

            # Collect results as they complete
            for future in as_completed(future_to_index, timeout=self.config.timeout):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except Exception as e:
                    results[index] = e

                completed += 1
                if progress_callback:
                    progress_callback(completed, len(items))

        return results

    def map_batched(
        self,
        func: Callable[[list[T]], list[Any]],
        items: list[T],
        batch_size: Optional[int] = None,
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> list[Any]:
        """Map a function over batches of items.

        Args:
            func: Function that takes a list and returns a list
            items: Items to process
            batch_size: Size of each batch
            progress_callback: Optional callback(completed, total)

        Returns:
            Flattened list of results
        """
        if not items:
            return []

        batch_size = batch_size or self.config.chunk_size
        batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]

        batch_results = self.run_parallel(func, batches, progress_callback)

        # Flatten results
        results = []
        for batch_result in batch_results:
            if isinstance(batch_result, Exception):
                continue
            results.extend(batch_result)

        return results


def parallel_analyze_files(
    files: list[Path],
    analyze_func: Callable[[Path], Any],
    max_workers: Optional[int] = None,
    progress_callback: Optional[Callable[[int, int], None]] = None,
) -> dict[Path, Any]:
    """Analyze files in parallel.

    Args:
        files: Files to analyze
        analyze_func: Function to analyze a single file
        max_workers: Maximum number of worker processes
        progress_callback: Optional callback(completed, total)

    Returns:
        Dict mapping file paths to results
    """
    config = ParallelConfig(
        max_workers=max_workers,
        use_processes=True,
    )

    processor = ParallelProcessor(config)

    def process_file(file_path: Path) -> tuple[Path, Any]:
        try:
            result = analyze_func(file_path)
            return (file_path, result)
        except Exception as e:
            return (file_path, e)

    results = processor.map(process_file, files, progress_callback)

    return {path: result for path, result in results}
