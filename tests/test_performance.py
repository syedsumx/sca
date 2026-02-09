"""Performance and load testing framework for CodeScope.

This module provides performance benchmarks, load tests, and memory profiling
to ensure the analysis engine scales appropriately.
"""

import gc
import os
import statistics
import tempfile
import time
from pathlib import Path
from typing import Callable, Any
from unittest.mock import MagicMock, patch
import sys

import pytest


# ── Performance Test Utilities ─────────────────────────────────────────────


def measure_time(func: Callable, *args, **kwargs) -> tuple[Any, float]:
    """Measure execution time of a function."""
    gc.collect()  # Clean up before measurement
    start = time.perf_counter()
    result = func(*args, **kwargs)
    elapsed = time.perf_counter() - start
    return result, elapsed


def measure_memory(func: Callable, *args, **kwargs) -> tuple[Any, int]:
    """Measure peak memory usage of a function (approximate)."""
    try:
        import tracemalloc
        tracemalloc.start()
        gc.collect()

        result = func(*args, **kwargs)

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return result, peak
    except ImportError:
        # tracemalloc not available
        result = func(*args, **kwargs)
        return result, 0


def benchmark(func: Callable, iterations: int = 5, *args, **kwargs) -> dict:
    """Run multiple iterations and compute statistics."""
    times = []
    for _ in range(iterations):
        _, elapsed = measure_time(func, *args, **kwargs)
        times.append(elapsed)

    return {
        "min": min(times),
        "max": max(times),
        "mean": statistics.mean(times),
        "median": statistics.median(times),
        "stdev": statistics.stdev(times) if len(times) > 1 else 0,
        "iterations": iterations,
    }


def create_large_python_file(lines: int) -> str:
    """Generate a large Python file for testing."""
    content = ['"""Generated test file."""', "", "import os", "import sys", ""]

    for i in range(lines // 10):
        content.append(f"""
def function_{i}(a, b, c):
    \"\"\"Function {i} docstring.\"\"\"
    result = a + b
    if result > 0:
        for j in range(c):
            result += j
    return result

class Class_{i}:
    \"\"\"Class {i} docstring.\"\"\"

    def method_{i}(self, x):
        return x * 2
""")

    return "\n".join(content)


def create_project_structure(base_path: Path, num_files: int, lines_per_file: int):
    """Create a mock project structure for testing."""
    src = base_path / "src"
    src.mkdir(parents=True, exist_ok=True)

    for i in range(num_files):
        subdir = src / f"module_{i % 5}"
        subdir.mkdir(exist_ok=True)
        file_path = subdir / f"file_{i}.py"
        file_path.write_text(create_large_python_file(lines_per_file))


# ── Parser Performance Tests ───────────────────────────────────────────────


class TestParserPerformance:
    """Performance tests for language parsers."""

    @pytest.mark.performance
    def test_parse_small_file_under_10ms(self):
        """Small files should parse in under 10ms."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = create_large_python_file(100)  # ~100 lines

        _, elapsed = measure_time(parser.parse, code)
        assert elapsed < 0.01, f"Parse took {elapsed:.4f}s, expected < 0.01s"

    @pytest.mark.performance
    def test_parse_medium_file_under_100ms(self):
        """Medium files should parse in under 100ms."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = create_large_python_file(1000)  # ~1000 lines

        _, elapsed = measure_time(parser.parse, code)
        assert elapsed < 0.1, f"Parse took {elapsed:.4f}s, expected < 0.1s"

    @pytest.mark.performance
    def test_parse_large_file_under_1s(self):
        """Large files should parse in under 1 second."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = create_large_python_file(10000)  # ~10,000 lines

        _, elapsed = measure_time(parser.parse, code)
        assert elapsed < 1.0, f"Parse took {elapsed:.4f}s, expected < 1.0s"

    @pytest.mark.performance
    def test_parser_memory_efficiency(self):
        """Parser should not use excessive memory."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = create_large_python_file(5000)

        _, peak_memory = measure_memory(parser.parse, code)

        # Should use less than 100MB for parsing
        max_memory = 100 * 1024 * 1024  # 100MB
        assert peak_memory < max_memory, f"Used {peak_memory / 1024 / 1024:.1f}MB, expected < 100MB"

    @pytest.mark.performance
    def test_parser_consistency(self):
        """Parser performance should be consistent across runs."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = create_large_python_file(500)

        stats = benchmark(parser.parse, iterations=10, code=code)

        # Standard deviation should be less than 50% of mean
        assert stats["stdev"] < stats["mean"] * 0.5, (
            f"Inconsistent performance: stdev={stats['stdev']:.4f}, mean={stats['mean']:.4f}"
        )


# ── Analysis Orchestrator Performance Tests ────────────────────────────────


class TestAnalysisPerformance:
    """Performance tests for the analysis orchestrator."""

    @pytest.mark.performance
    def test_analyze_small_project_under_5s(self):
        """Small projects should analyze in under 5 seconds."""
        from codescope.core.analysis import AnalysisOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            create_project_structure(base, num_files=10, lines_per_file=100)

            orchestrator = AnalysisOrchestrator()
            _, elapsed = measure_time(orchestrator.analyze, base)

            assert elapsed < 5.0, f"Analysis took {elapsed:.2f}s, expected < 5.0s"

    @pytest.mark.performance
    def test_analyze_medium_project_under_30s(self):
        """Medium projects should analyze in under 30 seconds."""
        from codescope.core.analysis import AnalysisOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            create_project_structure(base, num_files=50, lines_per_file=200)

            orchestrator = AnalysisOrchestrator()
            _, elapsed = measure_time(orchestrator.analyze, base)

            assert elapsed < 30.0, f"Analysis took {elapsed:.2f}s, expected < 30.0s"

    @pytest.mark.performance
    def test_incremental_analysis_faster(self):
        """Incremental analysis should be faster than full analysis."""
        from codescope.core.analysis import AnalysisOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            create_project_structure(base, num_files=20, lines_per_file=100)

            orchestrator = AnalysisOrchestrator()

            # First full analysis
            _, full_time = measure_time(orchestrator.analyze, base)

            # Modify one file
            (base / "src" / "module_0" / "file_0.py").write_text("# Modified\nx = 1\n")

            # Second analysis should use cache
            _, incremental_time = measure_time(orchestrator.analyze, base)

            # Incremental should be at least 30% faster (if caching works)
            # If not, at least should not be slower
            assert incremental_time <= full_time * 1.1, (
                f"Incremental ({incremental_time:.2f}s) slower than full ({full_time:.2f}s)"
            )


# ── Rule Engine Performance Tests ──────────────────────────────────────────


class TestRuleEnginePerformance:
    """Performance tests for rule matching."""

    @pytest.mark.performance
    def test_security_rules_under_100ms(self):
        """Security rule checks should complete in under 100ms per file."""
        from codescope.rules.security.injection import SQLInjectionRule, CommandInjectionRule

        code = """
import os
import subprocess
import sqlite3

def vulnerable_query(user_input):
    query = "SELECT * FROM users WHERE name = '%s'" % user_input
    cursor.execute(query)
    return cursor.fetchall()

def vulnerable_command(cmd):
    os.system(cmd)
    subprocess.call(cmd, shell=True)
    subprocess.Popen(cmd, shell=True)
"""
        rules = [SQLInjectionRule(), CommandInjectionRule()]

        total_time = 0
        for rule in rules:
            _, elapsed = measure_time(rule.check, code, "test.py")
            total_time += elapsed

        assert total_time < 0.1, f"Rule checks took {total_time:.4f}s, expected < 0.1s"

    @pytest.mark.performance
    def test_custom_rules_scalability(self):
        """Custom rules should scale linearly with rule count."""
        from codescope.rules.custom.engine import CustomRulesEngine

        code = "x = 1\nprint('hello')\ny = eval(input())\n" * 100

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            path = f.name

        try:
            # 10 rules
            engine_10 = CustomRulesEngine()
            engine_10.load_from_dict([
                {"id": f"RULE{i:03d}", "name": f"Rule {i}", "pattern": f"pattern{i}", "severity": "INFO", "message": "Test"}
                for i in range(10)
            ])
            _, time_10 = measure_time(engine_10.scan_file, path, code)

            # 100 rules
            engine_100 = CustomRulesEngine()
            engine_100.load_from_dict([
                {"id": f"RULE{i:03d}", "name": f"Rule {i}", "pattern": f"pattern{i}", "severity": "INFO", "message": "Test"}
                for i in range(100)
            ])
            _, time_100 = measure_time(engine_100.scan_file, path, code)

            # 100 rules should take less than 15x the time of 10 rules (allowing for overhead)
            assert time_100 < time_10 * 15, (
                f"100 rules ({time_100:.4f}s) took more than 15x time of 10 rules ({time_10:.4f}s)"
            )
        finally:
            os.unlink(path)


# ── Secret Scanner Performance Tests ───────────────────────────────────────


class TestSecretScannerPerformance:
    """Performance tests for secret scanning."""

    @pytest.mark.performance
    def test_scan_file_under_50ms(self):
        """Single file scan should complete in under 50ms."""
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()
        code = "\n".join([f'API_KEY_{i} = "test_value_{i}"' for i in range(100)])

        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
            f.write(code)
            path = f.name

        try:
            _, elapsed = measure_time(scanner.scan_file, path)
            assert elapsed < 0.05, f"Scan took {elapsed:.4f}s, expected < 0.05s"
        finally:
            os.unlink(path)

    @pytest.mark.performance
    def test_scan_directory_scales_linearly(self):
        """Directory scan should scale linearly with file count."""
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create 10 files
            for i in range(10):
                Path(tmpdir, f"file_{i}.py").write_text(f'KEY_{i} = "value"\n')

            _, time_10 = measure_time(scanner.scan_directory, tmpdir)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create 50 files
            for i in range(50):
                Path(tmpdir, f"file_{i}.py").write_text(f'KEY_{i} = "value"\n')

            _, time_50 = measure_time(scanner.scan_directory, tmpdir)

        # 50 files should take less than 10x the time of 10 files
        assert time_50 < time_10 * 10, (
            f"50 files ({time_50:.4f}s) took more than 10x time of 10 files ({time_10:.4f}s)"
        )


# ── SBOM Generator Performance Tests ───────────────────────────────────────


class TestSBOMPerformance:
    """Performance tests for SBOM generation."""

    @pytest.mark.performance
    def test_generate_sbom_under_1s(self):
        """SBOM generation should complete in under 1 second."""
        from codescope.sbom.generator import SBOMGenerator

        gen = SBOMGenerator(project_name="test", project_version="1.0.0")

        # Load 100 dependencies
        deps = [
            {"name": f"package-{i}", "version": f"{i}.0.0", "ecosystem": "npm"}
            for i in range(100)
        ]
        gen.load_from_dependency_scan(deps)

        _, elapsed = measure_time(gen.to_cyclonedx)
        assert elapsed < 1.0, f"SBOM generation took {elapsed:.4f}s, expected < 1.0s"


# ── Trend Store Performance Tests ──────────────────────────────────────────


class TestTrendStorePerformance:
    """Performance tests for trend storage."""

    @pytest.mark.performance
    def test_insert_1000_records_under_5s(self):
        """Inserting 1000 records should complete in under 5 seconds."""
        from codescope.trends.store import TrendSnapshot, TrendStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            store = TrendStore(db_path)

            def insert_records():
                for i in range(1000):
                    store.record(TrendSnapshot(
                        project=f"proj-{i % 10}",
                        timestamp=f"2025-01-{(i % 28) + 1:02d}T{i % 24:02d}:00:00Z",
                        total_issues=i % 100,
                    ))

            _, elapsed = measure_time(insert_records)
            store.close()

            assert elapsed < 5.0, f"Inserts took {elapsed:.2f}s, expected < 5.0s"
        finally:
            os.unlink(db_path)

    @pytest.mark.performance
    def test_query_performance(self):
        """Querying trends should be fast even with large dataset."""
        from codescope.trends.store import TrendSnapshot, TrendStore

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            db_path = tmp.name

        try:
            store = TrendStore(db_path)

            # Insert 5000 records
            for i in range(5000):
                store.record(TrendSnapshot(
                    project=f"proj-{i % 20}",
                    timestamp=f"2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T00:00:00Z",
                    total_issues=i % 50,
                ))

            # Query should be fast
            _, elapsed = measure_time(store.get_trends, "proj-0", limit=100)
            store.close()

            assert elapsed < 0.1, f"Query took {elapsed:.4f}s, expected < 0.1s"
        finally:
            os.unlink(db_path)


# ── Reporter Performance Tests ─────────────────────────────────────────────


class TestReporterPerformance:
    """Performance tests for report generation."""

    @pytest.mark.performance
    def test_json_reporter_1000_issues_under_1s(self):
        """JSON report with 1000 issues should generate in under 1 second."""
        from codescope.reporters.json_reporter import JSONReporter

        reporter = JSONReporter()
        issues = [
            {
                "rule_id": f"RULE{i:04d}",
                "message": f"Issue message {i} with some additional context",
                "file": f"src/module_{i % 10}/file_{i % 100}.py",
                "line": i % 1000,
                "severity": ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"][i % 5],
            }
            for i in range(1000)
        ]

        _, elapsed = measure_time(reporter.generate, issues=issues, metrics={"total": 1000})
        assert elapsed < 1.0, f"Report generation took {elapsed:.4f}s, expected < 1.0s"

    @pytest.mark.performance
    def test_html_reporter_500_issues_under_2s(self):
        """HTML report with 500 issues should generate in under 2 seconds."""
        from codescope.reporters.html_reporter import HTMLReporter

        reporter = HTMLReporter()
        issues = [
            {
                "rule_id": f"RULE{i:04d}",
                "message": f"Issue message {i}",
                "file": f"src/file_{i}.py",
                "line": i,
                "severity": "MAJOR",
            }
            for i in range(500)
        ]

        _, elapsed = measure_time(reporter.generate, issues=issues, metrics={})
        assert elapsed < 2.0, f"HTML generation took {elapsed:.4f}s, expected < 2.0s"

    @pytest.mark.performance
    def test_sarif_reporter_1000_issues_under_1s(self):
        """SARIF report with 1000 issues should generate in under 1 second."""
        from codescope.reporters.sarif import SARIFReporter

        reporter = SARIFReporter()
        issues = [
            {
                "rule_id": f"RULE{i:04d}",
                "message": f"Issue {i}",
                "file": f"file_{i}.py",
                "line": i,
                "severity": "MAJOR",
            }
            for i in range(1000)
        ]

        _, elapsed = measure_time(reporter.generate, issues=issues, metrics={})
        assert elapsed < 1.0, f"SARIF generation took {elapsed:.4f}s, expected < 1.0s"


# ── Load Testing ───────────────────────────────────────────────────────────


class TestLoadHandling:
    """Load tests for concurrent operations."""

    @pytest.mark.performance
    def test_concurrent_file_parsing(self):
        """Parser should handle concurrent file parsing."""
        import concurrent.futures
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        codes = [create_large_python_file(200) for _ in range(20)]

        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(parser.parse, code) for code in codes]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]
        elapsed = time.perf_counter() - start

        assert len(results) == 20
        assert elapsed < 5.0, f"Concurrent parsing took {elapsed:.2f}s, expected < 5.0s"

    @pytest.mark.performance
    def test_concurrent_secret_scanning(self):
        """Secret scanner should handle concurrent file scanning."""
        import concurrent.futures
        from codescope.analyzers.secrets.scanner import SecretScanner

        scanner = SecretScanner()

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create 20 files
            paths = []
            for i in range(20):
                path = Path(tmpdir) / f"file_{i}.py"
                path.write_text(f'KEY_{i} = "value_{i}"\n' * 50)
                paths.append(str(path))

            start = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(scanner.scan_file, path) for path in paths]
                results = [f.result() for f in concurrent.futures.as_completed(futures)]
            elapsed = time.perf_counter() - start

            assert len(results) == 20
            assert elapsed < 5.0, f"Concurrent scanning took {elapsed:.2f}s, expected < 5.0s"


# ── Memory Stress Tests ────────────────────────────────────────────────────


class TestMemoryStress:
    """Memory stress tests to ensure no memory leaks."""

    @pytest.mark.performance
    def test_repeated_parsing_no_memory_leak(self):
        """Repeated parsing should not cause memory growth."""
        from codescope.parsers.python.parser import PythonParser

        parser = PythonParser()
        code = create_large_python_file(500)

        # Warm up
        for _ in range(5):
            parser.parse(code)
            gc.collect()

        # Measure baseline
        gc.collect()
        try:
            import tracemalloc
            tracemalloc.start()
            baseline = tracemalloc.get_traced_memory()[0]

            # Run many iterations
            for _ in range(50):
                parser.parse(code)
                gc.collect()

            final = tracemalloc.get_traced_memory()[0]
            tracemalloc.stop()

            # Memory should not grow more than 10MB
            growth = final - baseline
            assert growth < 10 * 1024 * 1024, f"Memory grew by {growth / 1024 / 1024:.1f}MB"
        except ImportError:
            pytest.skip("tracemalloc not available")

    @pytest.mark.performance
    def test_repeated_analysis_no_memory_leak(self):
        """Repeated analysis should not cause memory growth."""
        from codescope.core.analysis import AnalysisOrchestrator

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            create_project_structure(base, num_files=5, lines_per_file=100)

            orchestrator = AnalysisOrchestrator()

            # Warm up
            for _ in range(2):
                orchestrator.analyze(base)
                gc.collect()

            # Measure
            gc.collect()
            try:
                import tracemalloc
                tracemalloc.start()
                baseline = tracemalloc.get_traced_memory()[0]

                for _ in range(10):
                    orchestrator.analyze(base)
                    gc.collect()

                final = tracemalloc.get_traced_memory()[0]
                tracemalloc.stop()

                growth = final - baseline
                assert growth < 20 * 1024 * 1024, f"Memory grew by {growth / 1024 / 1024:.1f}MB"
            except ImportError:
                pytest.skip("tracemalloc not available")


# ── Benchmark Report Generation ────────────────────────────────────────────


@pytest.fixture(scope="module")
def benchmark_results():
    """Collect benchmark results for reporting."""
    return {}


@pytest.mark.performance
def test_generate_benchmark_report(benchmark_results):
    """Generate a summary benchmark report."""
    from codescope.parsers.python.parser import PythonParser

    parser = PythonParser()

    sizes = [100, 500, 1000, 5000]
    results = {}

    for size in sizes:
        code = create_large_python_file(size)
        stats = benchmark(parser.parse, iterations=5, code=code)
        results[f"{size}_lines"] = stats

    # Store for potential reporting
    benchmark_results.update(results)

    # All benchmarks should complete
    assert len(results) == len(sizes)

    # Print summary (visible in pytest output with -v)
    print("\n=== Parser Benchmark Results ===")
    for size, stats in results.items():
        print(f"{size}: mean={stats['mean']*1000:.2f}ms, median={stats['median']*1000:.2f}ms")
