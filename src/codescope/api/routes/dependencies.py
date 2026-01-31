"""Dependencies API routes."""

from pathlib import Path
from fastapi import APIRouter, HTTPException

from codescope.api.models import (
    DependencyScanResponse,
    DependencyResponse,
    VulnerabilityResponse,
)
from codescope.analyzers.dependencies.scanner import DependencyScanner
from codescope.api.routes.analysis import _analyses

router = APIRouter()


@router.get("/analyses/{analysis_id}/dependencies", response_model=DependencyScanResponse)
async def get_dependencies(analysis_id: str):
    """Get dependency scan for an analysis."""
    if analysis_id not in _analyses:
        raise HTTPException(status_code=404, detail="Analysis not found")

    data = _analyses[analysis_id]
    path = Path(data.get("path", "."))

    scanner = DependencyScanner()
    result = scanner.scan(path)

    return DependencyScanResponse(
        total_dependencies=result.total_dependencies,
        direct_dependencies=result.direct_dependencies,
        dev_dependencies=result.dev_dependencies,
        vulnerable_count=result.vulnerable_count,
        dependencies=[
            DependencyResponse(
                name=d.name,
                version=d.version,
                ecosystem=d.ecosystem,
                source_file=d.source_file,
                is_dev=d.is_dev,
            )
            for d in result.dependencies
        ],
        vulnerabilities=[
            VulnerabilityResponse(
                id=v.id,
                severity=v.severity,
                summary=v.summary,
                details=v.details,
                affected_package=v.affected_package,
                affected_versions=v.affected_versions,
                fixed_version=v.fixed_version,
                references=v.references,
                cvss_score=v.cvss_score,
            )
            for v in result.vulnerabilities
        ],
    )


@router.post("/dependencies/scan", response_model=DependencyScanResponse)
async def scan_dependencies(
    path: str,
    check_vulnerabilities: bool = True,
):
    """Scan dependencies in a project path."""
    project_path = Path(path)
    if not project_path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")

    scanner = DependencyScanner(check_vulnerabilities=check_vulnerabilities)
    result = scanner.scan(project_path)

    return DependencyScanResponse(
        total_dependencies=result.total_dependencies,
        direct_dependencies=result.direct_dependencies,
        dev_dependencies=result.dev_dependencies,
        vulnerable_count=result.vulnerable_count,
        dependencies=[
            DependencyResponse(
                name=d.name,
                version=d.version,
                ecosystem=d.ecosystem,
                source_file=d.source_file,
                is_dev=d.is_dev,
            )
            for d in result.dependencies
        ],
        vulnerabilities=[
            VulnerabilityResponse(
                id=v.id,
                severity=v.severity,
                summary=v.summary,
                details=v.details,
                affected_package=v.affected_package,
                affected_versions=v.affected_versions,
                fixed_version=v.fixed_version,
                references=v.references,
                cvss_score=v.cvss_score,
            )
            for v in result.vulnerabilities
        ],
    )


@router.get("/dependencies/vulnerabilities")
async def check_vulnerabilities(
    package: str,
    version: str,
    ecosystem: str = "pypi",
):
    """Check a specific package for vulnerabilities."""
    scanner = DependencyScanner()

    # Create a temporary dependency to check
    from codescope.analyzers.dependencies.scanner import Dependency
    dep = Dependency(
        name=package,
        version=version,
        ecosystem=ecosystem,
        source_file="",
    )

    vulns = scanner._check_osv([dep])

    return {
        "package": package,
        "version": version,
        "ecosystem": ecosystem,
        "vulnerabilities": [
            {
                "id": v.id,
                "severity": v.severity.value,
                "summary": v.summary,
                "fixed_version": v.fixed_version,
            }
            for v in vulns
        ],
    }
