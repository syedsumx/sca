"""License compliance analyzer."""

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class LicenseRisk(str, Enum):
    """Risk level for a license."""
    LOW = "low"           # Permissive (MIT, BSD, Apache)
    MEDIUM = "medium"     # Weak copyleft (LGPL, MPL)
    HIGH = "high"         # Strong copyleft (GPL, AGPL)
    UNKNOWN = "unknown"   # License not recognized


@dataclass
class LicenseInfo:
    """Information about a detected license."""

    name: str
    spdx_id: Optional[str]
    file_path: str
    package_name: Optional[str]
    risk: LicenseRisk
    is_compatible: bool = True
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "spdx_id": self.spdx_id,
            "file_path": self.file_path,
            "package_name": self.package_name,
            "risk": self.risk.value,
            "is_compatible": self.is_compatible,
            "notes": self.notes,
        }


@dataclass
class LicenseComplianceResult:
    """Result of license compliance analysis."""

    project_license: Optional[LicenseInfo] = None
    dependency_licenses: list[LicenseInfo] = field(default_factory=list)
    compatibility_issues: list[str] = field(default_factory=list)
    high_risk_count: int = 0
    medium_risk_count: int = 0
    low_risk_count: int = 0
    unknown_count: int = 0

    @property
    def is_compliant(self) -> bool:
        return len(self.compatibility_issues) == 0

    def to_dict(self) -> dict:
        return {
            "is_compliant": self.is_compliant,
            "project_license": self.project_license.to_dict() if self.project_license else None,
            "dependency_licenses": [lic.to_dict() for lic in self.dependency_licenses],
            "compatibility_issues": self.compatibility_issues,
            "summary": {
                "high_risk": self.high_risk_count,
                "medium_risk": self.medium_risk_count,
                "low_risk": self.low_risk_count,
                "unknown": self.unknown_count,
                "total": len(self.dependency_licenses),
            },
        }


class LicenseAnalyzer:
    """Analyzer for license compliance."""

    # License database with SPDX IDs and risk levels
    LICENSE_DATABASE = {
        # Permissive (Low Risk)
        "MIT": {"spdx": "MIT", "risk": LicenseRisk.LOW, "patterns": [r"\bMIT\s+License\b", r"\bMIT\b"]},
        "Apache-2.0": {"spdx": "Apache-2.0", "risk": LicenseRisk.LOW, "patterns": [r"Apache\s+License.*2\.0", r"Apache-2\.0"]},
        "BSD-2-Clause": {"spdx": "BSD-2-Clause", "risk": LicenseRisk.LOW, "patterns": [r"BSD\s+2-Clause", r"Simplified\s+BSD"]},
        "BSD-3-Clause": {"spdx": "BSD-3-Clause", "risk": LicenseRisk.LOW, "patterns": [r"BSD\s+3-Clause", r"New\s+BSD", r"Modified\s+BSD"]},
        "ISC": {"spdx": "ISC", "risk": LicenseRisk.LOW, "patterns": [r"\bISC\s+License\b", r"\bISC\b"]},
        "Unlicense": {"spdx": "Unlicense", "risk": LicenseRisk.LOW, "patterns": [r"Unlicense", r"Public\s+Domain"]},
        "CC0-1.0": {"spdx": "CC0-1.0", "risk": LicenseRisk.LOW, "patterns": [r"CC0", r"Creative\s+Commons\s+Zero"]},

        # Weak Copyleft (Medium Risk)
        "LGPL-2.1": {"spdx": "LGPL-2.1", "risk": LicenseRisk.MEDIUM, "patterns": [r"LGPL.*2\.1", r"Lesser\s+GPL.*2\.1"]},
        "LGPL-3.0": {"spdx": "LGPL-3.0", "risk": LicenseRisk.MEDIUM, "patterns": [r"LGPL.*3\.0", r"Lesser\s+GPL.*3"]},
        "MPL-2.0": {"spdx": "MPL-2.0", "risk": LicenseRisk.MEDIUM, "patterns": [r"Mozilla\s+Public\s+License.*2\.0", r"MPL-2\.0"]},
        "EPL-1.0": {"spdx": "EPL-1.0", "risk": LicenseRisk.MEDIUM, "patterns": [r"Eclipse\s+Public\s+License.*1\.0", r"EPL-1\.0"]},
        "EPL-2.0": {"spdx": "EPL-2.0", "risk": LicenseRisk.MEDIUM, "patterns": [r"Eclipse\s+Public\s+License.*2\.0", r"EPL-2\.0"]},

        # Strong Copyleft (High Risk)
        "GPL-2.0": {"spdx": "GPL-2.0", "risk": LicenseRisk.HIGH, "patterns": [r"GNU\s+General\s+Public\s+License.*2", r"GPL-2\.0", r"GPLv2"]},
        "GPL-3.0": {"spdx": "GPL-3.0", "risk": LicenseRisk.HIGH, "patterns": [r"GNU\s+General\s+Public\s+License.*3", r"GPL-3\.0", r"GPLv3"]},
        "AGPL-3.0": {"spdx": "AGPL-3.0", "risk": LicenseRisk.HIGH, "patterns": [r"GNU\s+Affero.*3", r"AGPL-3\.0", r"AGPLv3"]},
    }

    # Compatibility matrix (which licenses can be combined)
    COMPATIBILITY = {
        "MIT": {"MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Apache-2.0", "LGPL-2.1", "LGPL-3.0", "GPL-2.0", "GPL-3.0"},
        "BSD-3-Clause": {"MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Apache-2.0", "LGPL-2.1", "LGPL-3.0", "GPL-2.0", "GPL-3.0"},
        "Apache-2.0": {"MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Apache-2.0", "LGPL-3.0", "GPL-3.0"},
        "GPL-3.0": {"MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Apache-2.0", "LGPL-3.0", "GPL-3.0", "AGPL-3.0"},
        "GPL-2.0": {"MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "LGPL-2.1", "GPL-2.0"},
        "LGPL-3.0": {"MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Apache-2.0", "LGPL-3.0"},
        "LGPL-2.1": {"MIT", "BSD-2-Clause", "BSD-3-Clause", "ISC", "LGPL-2.1"},
    }

    def __init__(self, project_license: Optional[str] = None):
        """Initialize analyzer with optional project license."""
        self.project_license = project_license

    def analyze(self, path: Path) -> LicenseComplianceResult:
        """Analyze licenses in a project."""
        result = LicenseComplianceResult()

        # Find project license
        result.project_license = self._find_project_license(path)

        # Analyze dependency licenses
        result.dependency_licenses = self._analyze_dependencies(path)

        # Calculate risk counts
        for lic in result.dependency_licenses:
            if lic.risk == LicenseRisk.HIGH:
                result.high_risk_count += 1
            elif lic.risk == LicenseRisk.MEDIUM:
                result.medium_risk_count += 1
            elif lic.risk == LicenseRisk.LOW:
                result.low_risk_count += 1
            else:
                result.unknown_count += 1

        # Check compatibility
        if result.project_license:
            result.compatibility_issues = self._check_compatibility(
                result.project_license, result.dependency_licenses
            )

        return result

    def _find_project_license(self, path: Path) -> Optional[LicenseInfo]:
        """Find the project's license."""
        license_files = [
            "LICENSE", "LICENSE.txt", "LICENSE.md",
            "LICENCE", "LICENCE.txt", "LICENCE.md",
            "COPYING", "COPYING.txt",
        ]

        for license_file in license_files:
            license_path = path / license_file
            if license_path.exists():
                content = license_path.read_text(encoding='utf-8', errors='ignore')
                detected = self._detect_license(content)

                if detected:
                    return LicenseInfo(
                        name=detected["name"],
                        spdx_id=detected["spdx"],
                        file_path=str(license_path),
                        package_name=None,
                        risk=detected["risk"],
                    )

        return None

    def _analyze_dependencies(self, path: Path) -> list[LicenseInfo]:
        """Analyze dependency licenses."""
        licenses = []

        # Python packages (from pip)
        licenses.extend(self._analyze_python_packages(path))

        # Node.js packages
        licenses.extend(self._analyze_npm_packages(path))

        # Go modules
        licenses.extend(self._analyze_go_modules(path))

        return licenses

    def _analyze_python_packages(self, path: Path) -> list[LicenseInfo]:
        """Analyze Python package licenses from metadata."""
        licenses = []

        # Check requirements.txt for package names
        req_file = path / "requirements.txt"
        if req_file.exists():
            content = req_file.read_text(encoding='utf-8', errors='ignore')

            for line in content.split('\n'):
                line = line.strip()
                if not line or line.startswith('#'):
                    continue

                # Extract package name
                match = re.match(r'^([a-zA-Z0-9_-]+)', line)
                if match:
                    pkg_name = match.group(1)

                    # Look for license in common locations
                    license_info = self._get_python_package_license(pkg_name, path)
                    if license_info:
                        licenses.append(license_info)

        return licenses

    def _get_python_package_license(self, pkg_name: str, path: Path) -> Optional[LicenseInfo]:
        """Get license for a Python package."""
        # This would typically query PyPI or read from installed packages
        # For now, return unknown license
        return LicenseInfo(
            name="Unknown",
            spdx_id=None,
            file_path="requirements.txt",
            package_name=pkg_name,
            risk=LicenseRisk.UNKNOWN,
            notes="License not detected - verify manually",
        )

    def _analyze_npm_packages(self, path: Path) -> list[LicenseInfo]:
        """Analyze NPM package licenses from package.json and package-lock.json."""
        licenses = []

        package_lock = path / "package-lock.json"
        if package_lock.exists():
            try:
                content = json.loads(package_lock.read_text(encoding='utf-8'))

                # Get packages from lockfile
                packages = content.get("packages", content.get("dependencies", {}))

                for pkg_path, pkg_info in packages.items():
                    if not pkg_path or pkg_path == "":
                        continue

                    pkg_name = pkg_path.replace("node_modules/", "")
                    license_str = pkg_info.get("license", "")

                    if license_str:
                        detected = self._detect_license_from_spdx(license_str)
                        if detected:
                            licenses.append(LicenseInfo(
                                name=detected["name"],
                                spdx_id=detected["spdx"],
                                file_path="package-lock.json",
                                package_name=pkg_name,
                                risk=detected["risk"],
                            ))
                        else:
                            licenses.append(LicenseInfo(
                                name=license_str,
                                spdx_id=None,
                                file_path="package-lock.json",
                                package_name=pkg_name,
                                risk=LicenseRisk.UNKNOWN,
                            ))

            except Exception:
                pass

        return licenses

    def _analyze_go_modules(self, path: Path) -> list[LicenseInfo]:
        """Analyze Go module licenses."""
        licenses = []

        go_sum = path / "go.sum"
        if go_sum.exists():
            content = go_sum.read_text(encoding='utf-8', errors='ignore')

            seen = set()
            for line in content.split('\n'):
                parts = line.strip().split()
                if len(parts) >= 2:
                    pkg_name = parts[0]
                    if pkg_name not in seen:
                        seen.add(pkg_name)
                        licenses.append(LicenseInfo(
                            name="Unknown",
                            spdx_id=None,
                            file_path="go.sum",
                            package_name=pkg_name,
                            risk=LicenseRisk.UNKNOWN,
                            notes="License not detected - verify manually",
                        ))

        return licenses

    def _detect_license(self, content: str) -> Optional[dict]:
        """Detect license from content."""
        content_lower = content.lower()

        for name, info in self.LICENSE_DATABASE.items():
            for pattern in info["patterns"]:
                if re.search(pattern, content, re.IGNORECASE):
                    return {
                        "name": name,
                        "spdx": info["spdx"],
                        "risk": info["risk"],
                    }

        return None

    def _detect_license_from_spdx(self, spdx_expr: str) -> Optional[dict]:
        """Detect license from SPDX expression."""
        # Handle simple SPDX IDs
        spdx_upper = spdx_expr.upper().strip()

        for name, info in self.LICENSE_DATABASE.items():
            if info["spdx"].upper() == spdx_upper or name.upper() == spdx_upper:
                return {
                    "name": name,
                    "spdx": info["spdx"],
                    "risk": info["risk"],
                }

        return None

    def _check_compatibility(
        self, project_license: LicenseInfo, dep_licenses: list[LicenseInfo]
    ) -> list[str]:
        """Check license compatibility."""
        issues = []

        if not project_license.spdx_id:
            return issues

        compatible = self.COMPATIBILITY.get(project_license.spdx_id, set())

        for dep in dep_licenses:
            if dep.spdx_id and dep.spdx_id not in compatible:
                dep.is_compatible = False
                issues.append(
                    f"License '{dep.spdx_id}' of '{dep.package_name}' "
                    f"may not be compatible with project license '{project_license.spdx_id}'"
                )

        return issues


def analyze_licenses(path: Path, project_license: Optional[str] = None) -> LicenseComplianceResult:
    """Convenience function to analyze licenses."""
    analyzer = LicenseAnalyzer(project_license=project_license)
    return analyzer.analyze(path)
