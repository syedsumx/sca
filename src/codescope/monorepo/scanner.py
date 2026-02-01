"""Monorepo scanner — orchestrates per-project scanning."""

from __future__ import annotations

import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from codescope.analyzers.orchestrator import analyze_path
from codescope.core.config import Config, load_config
from codescope.monorepo.detector import MonorepoDetector, SubProject, WorkspaceInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result data classes
# ---------------------------------------------------------------------------

@dataclass
class SubProjectResult:
    """Result of scanning a single sub-project."""

    project: SubProject
    issues: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    status: str = "completed"  # "completed" | "failed" | "skipped"
    error: str | None = None


@dataclass
class MonorepoResult:
    """Aggregated result for a full monorepo scan."""

    workspace: WorkspaceInfo
    project_results: list[SubProjectResult] = field(default_factory=list)
    aggregate_metrics: dict[str, Any] = field(default_factory=dict)

    # -- properties ----------------------------------------------------------

    @property
    def total_issues(self) -> int:
        """Total number of issues across all sub-projects."""
        return sum(len(r.issues) for r in self.project_results)

    @property
    def total_duration_ms(self) -> float:
        """Total wall-clock time spent scanning (sum of per-project durations)."""
        return sum(r.duration_ms for r in self.project_results)

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise the full result to a plain dictionary."""
        return {
            "workspace": self.workspace.to_dict(),
            "project_results": [
                {
                    "project": {
                        "name": r.project.name,
                        "path": str(r.project.path),
                        "language": r.project.language,
                    },
                    "issues": r.issues,
                    "metrics": r.metrics,
                    "duration_ms": r.duration_ms,
                    "status": r.status,
                    "error": r.error,
                }
                for r in self.project_results
            ],
            "aggregate_metrics": self.aggregate_metrics,
            "total_issues": self.total_issues,
            "total_duration_ms": self.total_duration_ms,
        }


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

class MonorepoScanner:
    """Scan all (or selected) sub-projects in a monorepo."""

    def __init__(self, root_path: Path, config: Config | None = None) -> None:
        self.root_path = root_path.resolve()
        self.config = config or load_config()
        self._detector = MonorepoDetector()

    # -- public API ----------------------------------------------------------

    def scan(
        self,
        project_names: list[str] | None = None,
        parallel: bool = False,
        progress_callback: Callable[[str, int, int], None] | None = None,
    ) -> MonorepoResult:
        """Scan the monorepo.

        Args:
            project_names: If given, only scan these sub-projects (by name).
            parallel: When ``True``, scan sub-projects concurrently using
                threads.
            progress_callback: Optional ``callback(project_name, current, total)``
                invoked before each project is scanned.

        Returns:
            A :class:`MonorepoResult` with per-project and aggregate data.
        """
        workspace = self._detector.detect(self.root_path)
        projects = workspace.projects

        if project_names is not None:
            name_set = set(project_names)
            projects = [p for p in projects if p.name in name_set]

        total = len(projects)

        if parallel and total > 1:
            results = self._scan_parallel(projects, progress_callback, total)
        else:
            results = self._scan_sequential(projects, progress_callback, total)

        aggregate = self._aggregate_metrics(results)
        return MonorepoResult(
            workspace=workspace,
            project_results=results,
            aggregate_metrics=aggregate,
        )

    def scan_changed(self, base_branch: str = "main") -> MonorepoResult:
        """Scan only sub-projects that have changed relative to *base_branch*.

        Uses ``git diff`` to determine which files have changed and maps
        them back to sub-projects.
        """
        workspace = self._detector.detect(self.root_path)
        changed_files = self._get_changed_files(base_branch)
        changed_projects = self._identify_changed_projects(workspace, changed_files)

        results: list[SubProjectResult] = []
        total = len(changed_projects)
        for idx, project in enumerate(changed_projects):
            logger.info(
                "Scanning changed project %d/%d: %s", idx + 1, total, project.name,
            )
            results.append(self._scan_project(project))

        aggregate = self._aggregate_metrics(results)
        return MonorepoResult(
            workspace=workspace,
            project_results=results,
            aggregate_metrics=aggregate,
        )

    # -- internal scanning ---------------------------------------------------

    def _scan_sequential(
        self,
        projects: list[SubProject],
        progress_callback: Callable[[str, int, int], None] | None,
        total: int,
    ) -> list[SubProjectResult]:
        results: list[SubProjectResult] = []
        for idx, project in enumerate(projects):
            if progress_callback:
                progress_callback(project.name, idx + 1, total)
            results.append(self._scan_project(project))
        return results

    def _scan_parallel(
        self,
        projects: list[SubProject],
        progress_callback: Callable[[str, int, int], None] | None,
        total: int,
    ) -> list[SubProjectResult]:
        results: list[SubProjectResult] = []
        with ThreadPoolExecutor() as executor:
            future_to_project = {
                executor.submit(self._scan_project, p): p for p in projects
            }
            done_count = 0
            for future in as_completed(future_to_project):
                done_count += 1
                project = future_to_project[future]
                if progress_callback:
                    progress_callback(project.name, done_count, total)
                results.append(future.result())
        return results

    def _scan_project(self, project: SubProject) -> SubProjectResult:
        """Run the analysis orchestrator on a single sub-project."""
        abs_path = self.root_path / project.path
        start = time.monotonic()
        try:
            analysis_results = analyze_path(abs_path, config=self.config)
            elapsed_ms = (time.monotonic() - start) * 1000.0

            # Serialise issues to plain dicts.
            issues: list[dict[str, Any]] = []
            for file_analysis in analysis_results.files:
                for issue in file_analysis.issues:
                    issues.append(
                        issue.to_dict() if hasattr(issue, "to_dict") else {
                            "rule": getattr(issue, "rule_id", ""),
                            "message": getattr(issue, "message", str(issue)),
                            "file": str(getattr(issue, "file_path", file_analysis.file_path)),
                            "line": getattr(issue, "line", 0),
                            "severity": getattr(issue, "severity", "unknown"),
                        }
                    )

            metrics: dict[str, Any] = {}
            if hasattr(analysis_results, "metrics") and analysis_results.metrics:
                metrics = (
                    analysis_results.metrics.to_dict()
                    if hasattr(analysis_results.metrics, "to_dict")
                    else dict(analysis_results.metrics)
                )

            return SubProjectResult(
                project=project,
                issues=issues,
                metrics=metrics,
                duration_ms=elapsed_ms,
                status="completed",
            )
        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            logger.warning("Failed to scan project %s: %s", project.name, exc)
            return SubProjectResult(
                project=project,
                duration_ms=elapsed_ms,
                status="failed",
                error=str(exc),
            )

    # -- git helpers ---------------------------------------------------------

    def _get_changed_files(self, base_branch: str) -> list[Path]:
        """Return a list of file paths changed since *base_branch*."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{base_branch}...HEAD"],
                cwd=self.root_path,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            return [Path(line) for line in result.stdout.strip().splitlines() if line]
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            logger.warning("git diff failed; falling back to empty changeset.")
            return []

    @staticmethod
    def _identify_changed_projects(
        workspace: WorkspaceInfo,
        changed_files: list[Path],
    ) -> list[SubProject]:
        """Map changed files to the sub-projects that own them."""
        changed: list[SubProject] = []
        seen: set[str] = set()
        for project in workspace.projects:
            if project.name in seen:
                continue
            proj_prefix = str(project.path)
            for f in changed_files:
                if str(f).startswith(proj_prefix):
                    changed.append(project)
                    seen.add(project.name)
                    break
        return changed

    # -- aggregation ---------------------------------------------------------

    @staticmethod
    def _aggregate_metrics(results: list[SubProjectResult]) -> dict[str, Any]:
        """Aggregate metrics across all scanned sub-projects."""
        total_issues = sum(len(r.issues) for r in results)
        total_vulnerabilities = 0
        total_bugs = 0
        total_code_smells = 0

        for r in results:
            total_vulnerabilities += r.metrics.get("vulnerabilities", 0)
            total_bugs += r.metrics.get("bugs", 0)
            total_code_smells += r.metrics.get("code_smells", 0)

        completed = sum(1 for r in results if r.status == "completed")
        failed = sum(1 for r in results if r.status == "failed")
        skipped = sum(1 for r in results if r.status == "skipped")

        return {
            "total_issues": total_issues,
            "vulnerabilities": total_vulnerabilities,
            "bugs": total_bugs,
            "code_smells": total_code_smells,
            "projects_scanned": completed,
            "projects_failed": failed,
            "projects_skipped": skipped,
        }
