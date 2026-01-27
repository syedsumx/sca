"""Dependency vulnerability scanner."""

import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codescope.analyzers.dependencies.parsers import (
    ParsedDependency,
    ALL_PARSERS,
    get_parser_for_file,
)


@dataclass
class Vulnerability:
    """A security vulnerability affecting a dependency."""

    id: str  # CVE or GHSA ID
    severity: str  # CRITICAL, HIGH, MEDIUM, LOW
    title: str
    description: str = ""
    fixed_version: str | None = None
    references: list[str] = field(default_factory=list)
    cvss_score: float | None = None
    cwe_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "title": self.title,
            "description": self.description,
            "fixed_version": self.fixed_version,
            "references": self.references,
            "cvss_score": self.cvss_score,
            "cwe_ids": self.cwe_ids,
        }


@dataclass
class Dependency:
    """A project dependency with vulnerability information."""

    name: str
    version: str | None
    ecosystem: str
    is_dev: bool = False
    source_file: str = ""
    line_number: int = 0
    vulnerabilities: list[Vulnerability] = field(default_factory=list)

    @property
    def is_vulnerable(self) -> bool:
        return len(self.vulnerabilities) > 0

    @property
    def highest_severity(self) -> str | None:
        if not self.vulnerabilities:
            return None
        severity_order = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
        for sev in severity_order:
            if any(v.severity == sev for v in self.vulnerabilities):
                return sev
        return self.vulnerabilities[0].severity

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "ecosystem": self.ecosystem,
            "is_dev": self.is_dev,
            "source_file": self.source_file,
            "line_number": self.line_number,
            "is_vulnerable": self.is_vulnerable,
            "highest_severity": self.highest_severity,
            "vulnerabilities": [v.to_dict() for v in self.vulnerabilities],
        }


@dataclass
class DependencyScanResult:
    """Results of a dependency vulnerability scan."""

    dependencies: list[Dependency] = field(default_factory=list)
    files_scanned: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_dependencies(self) -> int:
        return len(self.dependencies)

    @property
    def vulnerable_dependencies(self) -> list[Dependency]:
        return [d for d in self.dependencies if d.is_vulnerable]

    @property
    def vulnerability_count(self) -> int:
        return sum(len(d.vulnerabilities) for d in self.dependencies)

    @property
    def critical_count(self) -> int:
        return sum(1 for d in self.dependencies for v in d.vulnerabilities if v.severity == "CRITICAL")

    @property
    def high_count(self) -> int:
        return sum(1 for d in self.dependencies for v in d.vulnerabilities if v.severity == "HIGH")

    @property
    def medium_count(self) -> int:
        return sum(1 for d in self.dependencies for v in d.vulnerabilities if v.severity == "MEDIUM")

    @property
    def low_count(self) -> int:
        return sum(1 for d in self.dependencies for v in d.vulnerabilities if v.severity == "LOW")

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_dependencies": self.total_dependencies,
            "vulnerable_count": len(self.vulnerable_dependencies),
            "vulnerability_count": self.vulnerability_count,
            "critical_count": self.critical_count,
            "high_count": self.high_count,
            "medium_count": self.medium_count,
            "low_count": self.low_count,
            "files_scanned": self.files_scanned,
            "dependencies": [d.to_dict() for d in self.dependencies],
            "errors": self.errors,
        }


class DependencyScanner:
    """Scans dependencies for known vulnerabilities using OSV API."""

    OSV_API_URL = "https://api.osv.dev/v1/query"

    def __init__(self, check_vulnerabilities: bool = True):
        """Initialize the scanner.

        Args:
            check_vulnerabilities: Whether to check for vulnerabilities online.
        """
        self.check_vulnerabilities = check_vulnerabilities

    def scan(self, path: Path) -> DependencyScanResult:
        """Scan a directory for dependencies and vulnerabilities.

        Args:
            path: Directory to scan.

        Returns:
            DependencyScanResult with findings.
        """
        result = DependencyScanResult()

        # Find all dependency files
        dep_files = self._find_dependency_files(path)

        for dep_file in dep_files:
            parser = get_parser_for_file(dep_file)
            if not parser:
                continue

            try:
                result.files_scanned.append(str(dep_file))
                parsed_deps = parser.parse(dep_file)

                for parsed in parsed_deps:
                    dep = Dependency(
                        name=parsed.name,
                        version=parsed.version,
                        ecosystem=parsed.ecosystem,
                        is_dev=parsed.is_dev,
                        source_file=parsed.source_file,
                        line_number=parsed.line_number,
                    )

                    # Check for vulnerabilities
                    if self.check_vulnerabilities and dep.version:
                        vulns = self._check_vulnerabilities(dep)
                        dep.vulnerabilities = vulns

                    result.dependencies.append(dep)

            except Exception as e:
                result.errors.append(f"Error parsing {dep_file}: {str(e)}")

        return result

    def _find_dependency_files(self, path: Path) -> list[Path]:
        """Find all dependency manifest files."""
        files = []

        if path.is_file():
            if get_parser_for_file(path):
                files.append(path)
        else:
            # Search for known dependency files
            for parser in ALL_PARSERS:
                for pattern in parser.file_patterns:
                    if "*" in pattern:
                        files.extend(path.rglob(pattern))
                    else:
                        found = path.rglob(pattern)
                        files.extend(found)

        # Filter out common exclusions
        exclusions = ["node_modules", "venv", ".venv", "__pycache__", ".git", "vendor"]
        files = [f for f in files if not any(excl in str(f) for excl in exclusions)]

        return list(set(files))

    def _check_vulnerabilities(self, dep: Dependency) -> list[Vulnerability]:
        """Check OSV database for vulnerabilities.

        Args:
            dep: Dependency to check.

        Returns:
            List of vulnerabilities.
        """
        vulnerabilities = []

        # Map ecosystem names to OSV ecosystem names
        ecosystem_map = {
            "pypi": "PyPI",
            "npm": "npm",
            "go": "Go",
            "maven": "Maven",
            "rubygems": "RubyGems",
            "packagist": "Packagist",
            "crates.io": "crates.io",
        }

        osv_ecosystem = ecosystem_map.get(dep.ecosystem, dep.ecosystem)

        query = {
            "package": {
                "name": dep.name,
                "ecosystem": osv_ecosystem,
            }
        }

        if dep.version:
            query["version"] = dep.version

        try:
            data = json.dumps(query).encode("utf-8")
            req = urllib.request.Request(
                self.OSV_API_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))

            for vuln_data in result.get("vulns", []):
                vuln = self._parse_osv_vulnerability(vuln_data)
                if vuln:
                    vulnerabilities.append(vuln)

        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
            # Network error or invalid response - skip vulnerability check
            pass

        return vulnerabilities

    def _parse_osv_vulnerability(self, data: dict) -> Vulnerability | None:
        """Parse OSV vulnerability data."""
        vuln_id = data.get("id", "")
        if not vuln_id:
            return None

        # Determine severity
        severity = "MEDIUM"
        if "severity" in data:
            for sev in data["severity"]:
                if sev.get("type") == "CVSS_V3":
                    score = float(sev.get("score", "0").split("/")[0])
                    if score >= 9.0:
                        severity = "CRITICAL"
                    elif score >= 7.0:
                        severity = "HIGH"
                    elif score >= 4.0:
                        severity = "MEDIUM"
                    else:
                        severity = "LOW"
                    break

        # Get fixed version
        fixed_version = None
        for affected in data.get("affected", []):
            for range_data in affected.get("ranges", []):
                for event in range_data.get("events", []):
                    if "fixed" in event:
                        fixed_version = event["fixed"]
                        break

        # Get references
        references = [ref.get("url", "") for ref in data.get("references", []) if ref.get("url")]

        # Get CWE IDs
        cwe_ids = []
        for alias in data.get("aliases", []):
            if alias.startswith("CWE-"):
                cwe_ids.append(alias)

        return Vulnerability(
            id=vuln_id,
            severity=severity,
            title=data.get("summary", vuln_id),
            description=data.get("details", ""),
            fixed_version=fixed_version,
            references=references[:5],  # Limit to 5 references
            cwe_ids=cwe_ids,
        )


def scan_dependencies(
    path: Path | str,
    check_vulnerabilities: bool = True,
) -> DependencyScanResult:
    """Convenience function to scan dependencies.

    Args:
        path: Directory or file to scan.
        check_vulnerabilities: Whether to check for vulnerabilities online.

    Returns:
        DependencyScanResult with findings.
    """
    scanner = DependencyScanner(check_vulnerabilities=check_vulnerabilities)
    return scanner.scan(Path(path))
