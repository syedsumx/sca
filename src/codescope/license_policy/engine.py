"""License Compliance Policy Engine — allowlists, blocklists, SPDX evaluation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from codescope.analyzers.license import (
    LicenseAnalyzer,
    LicenseComplianceResult,
    LicenseInfo,
    LicenseRisk,
)


# ── Enums & models ──────────────────────────────────────────────────

class LicenseCategory(str, Enum):
    """License classification categories."""
    PERMISSIVE = "permissive"
    WEAK_COPYLEFT = "weak_copyleft"
    STRONG_COPYLEFT = "strong_copyleft"
    PROPRIETARY = "proprietary"
    PUBLIC_DOMAIN = "public_domain"
    UNKNOWN = "unknown"


class ViolationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


# Map SPDX IDs to categories
LICENSE_CATEGORIES: dict[str, LicenseCategory] = {
    # Permissive
    "MIT": LicenseCategory.PERMISSIVE,
    "Apache-2.0": LicenseCategory.PERMISSIVE,
    "BSD-2-Clause": LicenseCategory.PERMISSIVE,
    "BSD-3-Clause": LicenseCategory.PERMISSIVE,
    "ISC": LicenseCategory.PERMISSIVE,
    "Unlicense": LicenseCategory.PUBLIC_DOMAIN,
    "CC0-1.0": LicenseCategory.PUBLIC_DOMAIN,
    "0BSD": LicenseCategory.PERMISSIVE,
    "Zlib": LicenseCategory.PERMISSIVE,
    "BSL-1.0": LicenseCategory.PERMISSIVE,
    "PostgreSQL": LicenseCategory.PERMISSIVE,
    "WTFPL": LicenseCategory.PERMISSIVE,
    # Weak copyleft
    "LGPL-2.1": LicenseCategory.WEAK_COPYLEFT,
    "LGPL-2.1-only": LicenseCategory.WEAK_COPYLEFT,
    "LGPL-2.1-or-later": LicenseCategory.WEAK_COPYLEFT,
    "LGPL-3.0": LicenseCategory.WEAK_COPYLEFT,
    "LGPL-3.0-only": LicenseCategory.WEAK_COPYLEFT,
    "LGPL-3.0-or-later": LicenseCategory.WEAK_COPYLEFT,
    "MPL-2.0": LicenseCategory.WEAK_COPYLEFT,
    "EPL-1.0": LicenseCategory.WEAK_COPYLEFT,
    "EPL-2.0": LicenseCategory.WEAK_COPYLEFT,
    "CDDL-1.0": LicenseCategory.WEAK_COPYLEFT,
    "CDDL-1.1": LicenseCategory.WEAK_COPYLEFT,
    # Strong copyleft
    "GPL-2.0": LicenseCategory.STRONG_COPYLEFT,
    "GPL-2.0-only": LicenseCategory.STRONG_COPYLEFT,
    "GPL-2.0-or-later": LicenseCategory.STRONG_COPYLEFT,
    "GPL-3.0": LicenseCategory.STRONG_COPYLEFT,
    "GPL-3.0-only": LicenseCategory.STRONG_COPYLEFT,
    "GPL-3.0-or-later": LicenseCategory.STRONG_COPYLEFT,
    "AGPL-3.0": LicenseCategory.STRONG_COPYLEFT,
    "AGPL-3.0-only": LicenseCategory.STRONG_COPYLEFT,
    "AGPL-3.0-or-later": LicenseCategory.STRONG_COPYLEFT,
    "SSPL-1.0": LicenseCategory.STRONG_COPYLEFT,
    "EUPL-1.2": LicenseCategory.STRONG_COPYLEFT,
}


@dataclass
class PolicyViolation:
    """A single license policy violation."""
    package_name: str
    license_id: str | None
    license_name: str
    category: LicenseCategory
    violation_type: str  # "blocked_license", "blocked_category", "not_in_allowlist", "unknown_license", "incompatible"
    severity: ViolationSeverity
    message: str
    file_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_name": self.package_name,
            "license_id": self.license_id,
            "license_name": self.license_name,
            "category": self.category.value,
            "violation_type": self.violation_type,
            "severity": self.severity.value,
            "message": self.message,
            "file_path": self.file_path,
        }


@dataclass
class LicensePolicy:
    """A license policy definition."""
    name: str = "default"
    description: str = ""
    # If non-empty, only these licenses are allowed
    allowed_licenses: list[str] = field(default_factory=list)
    # These licenses are explicitly blocked
    blocked_licenses: list[str] = field(default_factory=list)
    # These categories are blocked
    blocked_categories: list[str] = field(default_factory=list)
    # How to handle unknown licenses
    allow_unknown: bool = False
    # Packages exempt from policy checks
    exempt_packages: list[str] = field(default_factory=list)
    # Project's own license (for compatibility checks)
    project_license: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "allowed_licenses": self.allowed_licenses,
            "blocked_licenses": self.blocked_licenses,
            "blocked_categories": self.blocked_categories,
            "allow_unknown": self.allow_unknown,
            "exempt_packages": self.exempt_packages,
            "project_license": self.project_license,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LicensePolicy":
        return cls(
            name=data.get("name", "default"),
            description=data.get("description", ""),
            allowed_licenses=data.get("allowed_licenses", []),
            blocked_licenses=data.get("blocked_licenses", []),
            blocked_categories=data.get("blocked_categories", []),
            allow_unknown=data.get("allow_unknown", False),
            exempt_packages=data.get("exempt_packages", []),
            project_license=data.get("project_license"),
        )

    @classmethod
    def commercial_strict(cls) -> "LicensePolicy":
        """Strict policy for proprietary / commercial projects."""
        return cls(
            name="commercial-strict",
            description="Strict policy for proprietary software — only permissive licenses allowed",
            allowed_licenses=[
                "MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause",
                "ISC", "Unlicense", "CC0-1.0", "0BSD", "Zlib",
                "BSL-1.0", "PostgreSQL", "WTFPL",
            ],
            blocked_categories=["strong_copyleft"],
            allow_unknown=False,
        )

    @classmethod
    def commercial_moderate(cls) -> "LicensePolicy":
        """Moderate policy — permissive + weak copyleft allowed."""
        return cls(
            name="commercial-moderate",
            description="Moderate policy — permissive and weak copyleft licenses allowed",
            blocked_licenses=["AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later", "SSPL-1.0"],
            blocked_categories=["strong_copyleft"],
            allow_unknown=False,
        )

    @classmethod
    def open_source_gpl(cls) -> "LicensePolicy":
        """GPL-compatible open source policy."""
        return cls(
            name="open-source-gpl",
            description="GPL-compatible open source project policy",
            blocked_licenses=["SSPL-1.0"],
            allow_unknown=True,
            project_license="GPL-3.0",
        )

    @classmethod
    def permissive_only(cls) -> "LicensePolicy":
        """Only permissive licenses allowed."""
        return cls(
            name="permissive-only",
            description="Only permissive and public domain licenses allowed",
            blocked_categories=["strong_copyleft", "weak_copyleft", "proprietary"],
            allow_unknown=False,
        )


PRESET_POLICIES = {
    "commercial-strict": LicensePolicy.commercial_strict,
    "commercial-moderate": LicensePolicy.commercial_moderate,
    "open-source-gpl": LicensePolicy.open_source_gpl,
    "permissive-only": LicensePolicy.permissive_only,
}


@dataclass
class PolicyResult:
    """Result of evaluating a license policy."""
    policy: LicensePolicy
    violations: list[PolicyViolation] = field(default_factory=list)
    approved_packages: list[dict[str, Any]] = field(default_factory=list)
    scan_result: LicenseComplianceResult | None = None

    @property
    def is_compliant(self) -> bool:
        return not any(v.severity == ViolationSeverity.ERROR for v in self.violations)

    @property
    def error_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == ViolationSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for v in self.violations if v.severity == ViolationSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "is_compliant": self.is_compliant,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "violations": [v.to_dict() for v in self.violations],
            "approved_packages": self.approved_packages,
            "summary": {
                "total_packages": len(self.approved_packages) + len(self.violations),
                "approved": len(self.approved_packages),
                "violations": len(self.violations),
            },
        }


# ── SPDX expression evaluator ───────────────────────────────────────

class SPDXEvaluator:
    """Evaluate SPDX license expressions (e.g. 'MIT OR Apache-2.0')."""

    @staticmethod
    def parse_expression(expr: str) -> list[str]:
        """Parse an SPDX expression into individual license IDs.

        Handles: MIT, MIT OR Apache-2.0, (MIT AND BSD-3-Clause), GPL-2.0-or-later WITH Classpath-exception-2.0
        Returns all license IDs found in the expression.
        """
        if not expr:
            return []
        # Remove parentheses, WITH exceptions
        cleaned = re.sub(r"\bWITH\s+[\w.-]+", "", expr)
        cleaned = cleaned.replace("(", " ").replace(")", " ")
        # Split on OR / AND operators
        tokens = re.split(r"\s+(?:OR|AND)\s+", cleaned)
        # Clean and filter
        ids = []
        for token in tokens:
            token = token.strip()
            if token and token not in ("OR", "AND", "WITH"):
                ids.append(token)
        return ids

    @staticmethod
    def is_allowed(expr: str, allowed_set: set[str]) -> bool:
        """Check if all licenses in an SPDX expression are in the allowed set."""
        ids = SPDXEvaluator.parse_expression(expr)
        return all(lid in allowed_set for lid in ids)

    @staticmethod
    def categorize(spdx_id: str) -> LicenseCategory:
        """Categorize a license by its SPDX ID."""
        return LICENSE_CATEGORIES.get(spdx_id, LicenseCategory.UNKNOWN)

    @staticmethod
    def get_all_categories() -> dict[str, list[str]]:
        """Get all licenses grouped by category."""
        result: dict[str, list[str]] = {}
        for spdx_id, cat in LICENSE_CATEGORIES.items():
            result.setdefault(cat.value, []).append(spdx_id)
        return result


# ── Policy engine ────────────────────────────────────────────────────

class LicensePolicyEngine:
    """Evaluate dependency licenses against a policy."""

    def __init__(self, policy: LicensePolicy | None = None):
        self.policy = policy or LicensePolicy()
        self._evaluator = SPDXEvaluator()

    @staticmethod
    def available_presets() -> list[dict[str, str]]:
        """List available preset policies."""
        result = []
        for name, builder in PRESET_POLICIES.items():
            p = builder()
            result.append({"name": p.name, "description": p.description})
        return result

    def evaluate(self, path: Path) -> PolicyResult:
        """Scan a project and evaluate against the policy."""
        analyzer = LicenseAnalyzer(project_license=self.policy.project_license)
        scan = analyzer.analyze(path)

        result = PolicyResult(policy=self.policy, scan_result=scan)
        self._check_dependencies(scan.dependency_licenses, result)
        return result

    def evaluate_licenses(self, licenses: list[LicenseInfo]) -> PolicyResult:
        """Evaluate a list of LicenseInfo objects against the policy."""
        result = PolicyResult(policy=self.policy)
        self._check_dependencies(licenses, result)
        return result

    def check_single(self, spdx_id: str, package_name: str = "unknown") -> PolicyViolation | None:
        """Quick check: is this license allowed by the policy?"""
        if package_name in self.policy.exempt_packages:
            return None

        ids = self._evaluator.parse_expression(spdx_id)
        for lid in ids:
            violation = self._check_license_id(lid, package_name, spdx_id)
            if violation:
                return violation
        return None

    # ── internal ─────────────────────────────────────────────────

    def _check_dependencies(self, licenses: list[LicenseInfo], result: PolicyResult) -> None:
        for lic in licenses:
            pkg = lic.package_name or "unknown"

            # Skip exempt packages
            if pkg in self.policy.exempt_packages:
                result.approved_packages.append({
                    "package": pkg,
                    "license": lic.spdx_id or lic.name,
                    "reason": "exempt",
                })
                continue

            # Unknown license handling
            if lic.risk == LicenseRisk.UNKNOWN or not lic.spdx_id:
                if not self.policy.allow_unknown:
                    result.violations.append(PolicyViolation(
                        package_name=pkg,
                        license_id=lic.spdx_id,
                        license_name=lic.name,
                        category=LicenseCategory.UNKNOWN,
                        violation_type="unknown_license",
                        severity=ViolationSeverity.WARNING,
                        message=f"Package '{pkg}' has unknown license '{lic.name}' — verify manually",
                        file_path=lic.file_path,
                    ))
                else:
                    result.approved_packages.append({
                        "package": pkg,
                        "license": lic.name,
                        "reason": "unknown_allowed",
                    })
                continue

            # Parse SPDX expression (handles compound expressions)
            ids = self._evaluator.parse_expression(lic.spdx_id)
            violation_found = False

            for lid in ids:
                v = self._check_license_id(lid, pkg, lic.spdx_id, lic.file_path)
                if v:
                    result.violations.append(v)
                    violation_found = True
                    break

            if not violation_found:
                result.approved_packages.append({
                    "package": pkg,
                    "license": lic.spdx_id,
                    "category": self._evaluator.categorize(ids[0]).value if ids else "unknown",
                })

    def _check_license_id(
        self, lid: str, pkg: str, full_expr: str, file_path: str = ""
    ) -> PolicyViolation | None:
        category = self._evaluator.categorize(lid)

        # Check blocked licenses
        if lid in self.policy.blocked_licenses:
            return PolicyViolation(
                package_name=pkg,
                license_id=lid,
                license_name=lid,
                category=category,
                violation_type="blocked_license",
                severity=ViolationSeverity.ERROR,
                message=f"Package '{pkg}' uses blocked license '{lid}'",
                file_path=file_path,
            )

        # Check blocked categories
        if category.value in self.policy.blocked_categories:
            return PolicyViolation(
                package_name=pkg,
                license_id=lid,
                license_name=lid,
                category=category,
                violation_type="blocked_category",
                severity=ViolationSeverity.ERROR,
                message=f"Package '{pkg}' uses license '{lid}' (category: {category.value}) which is blocked by policy",
                file_path=file_path,
            )

        # Check allowlist (if defined)
        if self.policy.allowed_licenses:
            if lid not in self.policy.allowed_licenses:
                return PolicyViolation(
                    package_name=pkg,
                    license_id=lid,
                    license_name=lid,
                    category=category,
                    violation_type="not_in_allowlist",
                    severity=ViolationSeverity.ERROR,
                    message=f"Package '{pkg}' uses license '{lid}' which is not in the allowed list",
                    file_path=file_path,
                )

        return None
