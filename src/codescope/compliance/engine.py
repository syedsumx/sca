"""Compliance & Policy Engine — maps findings to regulatory frameworks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from codescope.core.models import AnalysisResults, Issue


# ── Data models ──────────────────────────────────────────────────────

class ComplianceStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    PARTIAL = "PARTIAL"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass
class ComplianceRequirement:
    """A single requirement within a compliance framework."""

    req_id: str
    title: str
    description: str
    cwe_ids: list[int] = field(default_factory=list)
    owasp_ids: list[str] = field(default_factory=list)
    rule_tags: list[str] = field(default_factory=list)
    status: ComplianceStatus = ComplianceStatus.PASS
    matched_issues: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "req_id": self.req_id,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "matched_issues_count": len(self.matched_issues),
            "matched_issues": self.matched_issues,
        }


@dataclass
class ComplianceFramework:
    """A compliance framework (e.g. OWASP Top 10, CWE Top 25)."""

    framework_id: str
    name: str
    version: str
    description: str
    requirements: list[ComplianceRequirement] = field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.requirements if r.status == ComplianceStatus.PASS)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.requirements if r.status == ComplianceStatus.FAIL)

    @property
    def partial_count(self) -> int:
        return sum(1 for r in self.requirements if r.status == ComplianceStatus.PARTIAL)

    @property
    def compliance_percent(self) -> float:
        applicable = [r for r in self.requirements if r.status != ComplianceStatus.NOT_APPLICABLE]
        if not applicable:
            return 100.0
        passed = sum(1 for r in applicable if r.status == ComplianceStatus.PASS)
        return round((passed / len(applicable)) * 100, 1)

    @property
    def overall_status(self) -> ComplianceStatus:
        if self.fail_count == 0 and self.partial_count == 0:
            return ComplianceStatus.PASS
        if self.fail_count > 0:
            return ComplianceStatus.FAIL
        return ComplianceStatus.PARTIAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "framework_id": self.framework_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "overall_status": self.overall_status.value,
            "compliance_percent": self.compliance_percent,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "partial_count": self.partial_count,
            "requirements": [r.to_dict() for r in self.requirements],
        }


@dataclass
class ComplianceReport:
    """Full compliance report across multiple frameworks."""

    frameworks: list[ComplianceFramework] = field(default_factory=list)
    total_issues_mapped: int = 0

    @property
    def overall_status(self) -> ComplianceStatus:
        if any(f.overall_status == ComplianceStatus.FAIL for f in self.frameworks):
            return ComplianceStatus.FAIL
        if any(f.overall_status == ComplianceStatus.PARTIAL for f in self.frameworks):
            return ComplianceStatus.PARTIAL
        return ComplianceStatus.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status.value,
            "total_issues_mapped": self.total_issues_mapped,
            "frameworks": [f.to_dict() for f in self.frameworks],
        }


# ── Framework definitions ────────────────────────────────────────────

def _owasp_top10_2021() -> ComplianceFramework:
    return ComplianceFramework(
        framework_id="owasp-top10-2021",
        name="OWASP Top 10",
        version="2021",
        description="OWASP Top 10 Web Application Security Risks (2021)",
        requirements=[
            ComplianceRequirement(
                req_id="A01:2021",
                title="Broken Access Control",
                description="Restrictions on authenticated users are not properly enforced.",
                cwe_ids=[22, 23, 35, 59, 200, 201, 219, 264, 275, 276, 284, 285, 352, 359, 377, 402, 425, 441, 497, 538, 540, 548, 552, 566, 601, 639, 651, 668, 706, 862, 863, 913, 922, 1275],
                owasp_ids=["A01"],
                rule_tags=["access-control", "path-traversal", "authorization"],
            ),
            ComplianceRequirement(
                req_id="A02:2021",
                title="Cryptographic Failures",
                description="Failures related to cryptography leading to sensitive data exposure.",
                cwe_ids=[261, 296, 310, 319, 321, 322, 323, 324, 325, 326, 327, 328, 329, 330, 331, 335, 336, 337, 338, 340, 347, 523, 720, 757, 759, 760, 780, 818, 916],
                owasp_ids=["A02"],
                rule_tags=["crypto", "weak-hash", "hardcoded-secret", "insecure-random"],
            ),
            ComplianceRequirement(
                req_id="A03:2021",
                title="Injection",
                description="User-supplied data is not validated, filtered, or sanitized.",
                cwe_ids=[20, 74, 75, 77, 78, 79, 80, 83, 87, 88, 89, 90, 91, 93, 94, 95, 96, 97, 98, 99, 100, 113, 116, 138, 184, 470, 471, 564, 610, 643, 644, 652, 917],
                owasp_ids=["A03"],
                rule_tags=["injection", "sql-injection", "xss", "command-injection"],
            ),
            ComplianceRequirement(
                req_id="A04:2021",
                title="Insecure Design",
                description="Missing or ineffective control design.",
                cwe_ids=[73, 183, 209, 213, 235, 256, 257, 266, 269, 280, 311, 312, 313, 316, 419, 430, 434, 444, 451, 472, 501, 522, 525, 539, 579, 598, 602, 642, 646, 650, 653, 656, 657, 799, 807, 840, 841, 927, 1021, 1173],
                owasp_ids=["A04"],
                rule_tags=["insecure-design"],
            ),
            ComplianceRequirement(
                req_id="A05:2021",
                title="Security Misconfiguration",
                description="Missing appropriate security hardening or improperly configured permissions.",
                cwe_ids=[2, 11, 13, 15, 16, 260, 315, 520, 526, 537, 541, 547, 611, 614, 756, 776, 942, 1004, 1032, 1174],
                owasp_ids=["A05"],
                rule_tags=["misconfiguration", "xxe", "debug"],
            ),
            ComplianceRequirement(
                req_id="A06:2021",
                title="Vulnerable and Outdated Components",
                description="Using components with known vulnerabilities.",
                cwe_ids=[1035, 1104],
                owasp_ids=["A06"],
                rule_tags=["dependency", "outdated", "cve"],
            ),
            ComplianceRequirement(
                req_id="A07:2021",
                title="Identification and Authentication Failures",
                description="Confirmation of the user's identity, authentication, and session management.",
                cwe_ids=[255, 259, 287, 288, 290, 294, 295, 297, 300, 302, 304, 306, 307, 346, 384, 521, 613, 620, 640, 798, 940, 1216],
                owasp_ids=["A07"],
                rule_tags=["authentication", "session", "hardcoded-credential"],
            ),
            ComplianceRequirement(
                req_id="A08:2021",
                title="Software and Data Integrity Failures",
                description="Code and infrastructure that does not protect against integrity violations.",
                cwe_ids=[345, 353, 426, 494, 502, 565, 784, 829, 830, 915],
                owasp_ids=["A08"],
                rule_tags=["deserialization", "integrity"],
            ),
            ComplianceRequirement(
                req_id="A09:2021",
                title="Security Logging and Monitoring Failures",
                description="Insufficient logging, detection, monitoring, and active response.",
                cwe_ids=[117, 223, 532, 778],
                owasp_ids=["A09"],
                rule_tags=["logging", "monitoring"],
            ),
            ComplianceRequirement(
                req_id="A10:2021",
                title="Server-Side Request Forgery (SSRF)",
                description="Fetching a remote resource without validating the user-supplied URL.",
                cwe_ids=[918],
                owasp_ids=["A10"],
                rule_tags=["ssrf"],
            ),
        ],
    )


def _cwe_top25_2023() -> ComplianceFramework:
    return ComplianceFramework(
        framework_id="cwe-top25-2023",
        name="CWE Top 25",
        version="2023",
        description="CWE Top 25 Most Dangerous Software Weaknesses (2023)",
        requirements=[
            ComplianceRequirement(req_id="CWE-787", title="Out-of-bounds Write", description="Writing data past the end of the intended buffer.", cwe_ids=[787]),
            ComplianceRequirement(req_id="CWE-79", title="Cross-site Scripting (XSS)", description="Improper neutralization of input during web page generation.", cwe_ids=[79], rule_tags=["xss"]),
            ComplianceRequirement(req_id="CWE-89", title="SQL Injection", description="Improper neutralization of special elements in SQL commands.", cwe_ids=[89], rule_tags=["sql-injection"]),
            ComplianceRequirement(req_id="CWE-416", title="Use After Free", description="Referencing memory after it has been freed.", cwe_ids=[416]),
            ComplianceRequirement(req_id="CWE-78", title="OS Command Injection", description="Improper neutralization of special elements in OS commands.", cwe_ids=[78], rule_tags=["command-injection"]),
            ComplianceRequirement(req_id="CWE-20", title="Improper Input Validation", description="Not validating or incorrectly validating input.", cwe_ids=[20], rule_tags=["input-validation"]),
            ComplianceRequirement(req_id="CWE-125", title="Out-of-bounds Read", description="Reading data past the end of the intended buffer.", cwe_ids=[125]),
            ComplianceRequirement(req_id="CWE-22", title="Path Traversal", description="Improper limitation of a pathname to a restricted directory.", cwe_ids=[22], rule_tags=["path-traversal"]),
            ComplianceRequirement(req_id="CWE-352", title="Cross-Site Request Forgery", description="Forcing an end user to execute unwanted actions on an authenticated web app.", cwe_ids=[352], rule_tags=["csrf"]),
            ComplianceRequirement(req_id="CWE-434", title="Unrestricted Upload of File", description="Uploading dangerous file types without restriction.", cwe_ids=[434], rule_tags=["file-upload"]),
            ComplianceRequirement(req_id="CWE-862", title="Missing Authorization", description="Missing authorization check.", cwe_ids=[862], rule_tags=["authorization"]),
            ComplianceRequirement(req_id="CWE-476", title="NULL Pointer Dereference", description="Dereferencing a pointer that is NULL.", cwe_ids=[476]),
            ComplianceRequirement(req_id="CWE-287", title="Improper Authentication", description="Failing to prove that a claimed identity is correct.", cwe_ids=[287], rule_tags=["authentication"]),
            ComplianceRequirement(req_id="CWE-190", title="Integer Overflow", description="A result that exceeds the integer type's maximum value.", cwe_ids=[190]),
            ComplianceRequirement(req_id="CWE-502", title="Deserialization of Untrusted Data", description="Deserializing untrusted data without verification.", cwe_ids=[502], rule_tags=["deserialization"]),
            ComplianceRequirement(req_id="CWE-77", title="Command Injection", description="Constructing commands using externally-influenced input.", cwe_ids=[77], rule_tags=["command-injection"]),
            ComplianceRequirement(req_id="CWE-119", title="Buffer Overflow", description="Operations on a memory buffer outside its boundaries.", cwe_ids=[119]),
            ComplianceRequirement(req_id="CWE-798", title="Use of Hard-coded Credentials", description="Using hard-coded credentials for authentication.", cwe_ids=[798], rule_tags=["hardcoded-credential"]),
            ComplianceRequirement(req_id="CWE-918", title="Server-Side Request Forgery", description="Fetching a URL from a user-provided input without validation.", cwe_ids=[918], rule_tags=["ssrf"]),
            ComplianceRequirement(req_id="CWE-306", title="Missing Authentication for Critical Function", description="Not performing authentication for critical functionality.", cwe_ids=[306], rule_tags=["authentication"]),
            ComplianceRequirement(req_id="CWE-362", title="Race Condition", description="Concurrent execution using shared resource with improper synchronization.", cwe_ids=[362]),
            ComplianceRequirement(req_id="CWE-269", title="Improper Privilege Management", description="Not properly managing privileges.", cwe_ids=[269], rule_tags=["privilege"]),
            ComplianceRequirement(req_id="CWE-94", title="Code Injection", description="Improper control of generation of code.", cwe_ids=[94], rule_tags=["injection"]),
            ComplianceRequirement(req_id="CWE-863", title="Incorrect Authorization", description="Performing an authorization check incorrectly.", cwe_ids=[863], rule_tags=["authorization"]),
            ComplianceRequirement(req_id="CWE-276", title="Incorrect Default Permissions", description="Setting insecure default permissions during installation.", cwe_ids=[276], rule_tags=["permissions"]),
        ],
    )


def _sans_top25() -> ComplianceFramework:
    return ComplianceFramework(
        framework_id="sans-top25",
        name="SANS Top 25",
        version="2023",
        description="SANS Top 25 Software Errors",
        requirements=[
            ComplianceRequirement(req_id="SANS-01", title="SQL Injection", description="SQL injection attacks.", cwe_ids=[89], rule_tags=["sql-injection"]),
            ComplianceRequirement(req_id="SANS-02", title="OS Command Injection", description="OS command injection.", cwe_ids=[78], rule_tags=["command-injection"]),
            ComplianceRequirement(req_id="SANS-03", title="Buffer Overflow", description="Classic buffer overflow.", cwe_ids=[120, 119]),
            ComplianceRequirement(req_id="SANS-04", title="Cross-Site Scripting", description="XSS vulnerabilities.", cwe_ids=[79], rule_tags=["xss"]),
            ComplianceRequirement(req_id="SANS-05", title="Missing Authentication", description="Missing authentication for critical functions.", cwe_ids=[306], rule_tags=["authentication"]),
            ComplianceRequirement(req_id="SANS-06", title="Missing Authorization", description="Missing authorization.", cwe_ids=[862], rule_tags=["authorization"]),
            ComplianceRequirement(req_id="SANS-07", title="Hard-coded Credentials", description="Use of hard-coded credentials.", cwe_ids=[798], rule_tags=["hardcoded-credential"]),
            ComplianceRequirement(req_id="SANS-08", title="Path Traversal", description="Path traversal attacks.", cwe_ids=[22], rule_tags=["path-traversal"]),
            ComplianceRequirement(req_id="SANS-09", title="Unrestricted Upload", description="Dangerous file upload.", cwe_ids=[434], rule_tags=["file-upload"]),
            ComplianceRequirement(req_id="SANS-10", title="CSRF", description="Cross-site request forgery.", cwe_ids=[352], rule_tags=["csrf"]),
        ],
    )


def _pci_dss_v4() -> ComplianceFramework:
    return ComplianceFramework(
        framework_id="pci-dss-v4",
        name="PCI DSS",
        version="4.0",
        description="Payment Card Industry Data Security Standard v4.0",
        requirements=[
            ComplianceRequirement(req_id="PCI-6.2.4", title="Software Engineering Techniques", description="Prevent common software attacks (injection, buffer overflow, etc.).", cwe_ids=[89, 78, 79, 22, 119, 787, 120], rule_tags=["injection", "sql-injection", "xss", "command-injection", "path-traversal"]),
            ComplianceRequirement(req_id="PCI-6.2.4.1", title="Injection Attacks", description="Code is reviewed to ensure it is not vulnerable to injection attacks.", cwe_ids=[89, 78, 77, 94], rule_tags=["injection", "sql-injection", "command-injection"]),
            ComplianceRequirement(req_id="PCI-6.2.4.2", title="Buffer Overflow", description="Code is reviewed for buffer overflows.", cwe_ids=[119, 120, 787, 125]),
            ComplianceRequirement(req_id="PCI-6.2.4.3", title="Insecure Cryptography", description="Code is reviewed for improper cryptographic usage.", cwe_ids=[326, 327, 328, 330, 338], rule_tags=["crypto", "weak-hash", "insecure-random"]),
            ComplianceRequirement(req_id="PCI-6.2.4.4", title="Insecure Communication", description="Code is reviewed for insecure communications.", cwe_ids=[319, 295], rule_tags=["insecure-tls"]),
            ComplianceRequirement(req_id="PCI-6.2.4.5", title="Improper Access Control", description="Code is reviewed for access control issues.", cwe_ids=[284, 285, 862, 863], rule_tags=["access-control", "authorization"]),
            ComplianceRequirement(req_id="PCI-6.3.2", title="Software Inventory", description="Maintain an inventory of bespoke and custom software.", rule_tags=["sbom", "dependency"]),
            ComplianceRequirement(req_id="PCI-6.3.3", title="Vulnerability Management", description="Patch known vulnerabilities in components.", cwe_ids=[1035, 1104], rule_tags=["dependency", "cve"]),
            ComplianceRequirement(req_id="PCI-8.3.6", title="Password Complexity", description="Passwords meet minimum complexity requirements.", cwe_ids=[521], rule_tags=["authentication", "password"]),
            ComplianceRequirement(req_id="PCI-8.6.2", title="Hard-coded Passwords", description="No hard-coded passwords or accounts.", cwe_ids=[798, 259], rule_tags=["hardcoded-credential", "hardcoded-secret"]),
        ],
    )


def _hipaa() -> ComplianceFramework:
    return ComplianceFramework(
        framework_id="hipaa",
        name="HIPAA",
        version="2023",
        description="Health Insurance Portability and Accountability Act — Technical Safeguards",
        requirements=[
            ComplianceRequirement(req_id="HIPAA-164.312(a)(1)", title="Access Control", description="Implement technical policies and procedures for electronic information systems.", cwe_ids=[284, 285, 862, 863, 269], rule_tags=["access-control", "authorization"]),
            ComplianceRequirement(req_id="HIPAA-164.312(a)(2)(iv)", title="Encryption and Decryption", description="Implement mechanisms to encrypt and decrypt ePHI.", cwe_ids=[311, 312, 319, 326, 327], rule_tags=["crypto", "insecure-tls"]),
            ComplianceRequirement(req_id="HIPAA-164.312(c)(1)", title="Integrity", description="Implement policies to protect ePHI from improper alteration or destruction.", cwe_ids=[345, 354], rule_tags=["integrity"]),
            ComplianceRequirement(req_id="HIPAA-164.312(d)", title="Authentication", description="Verify persons seeking access to ePHI are who they claim to be.", cwe_ids=[287, 306, 798], rule_tags=["authentication", "hardcoded-credential"]),
            ComplianceRequirement(req_id="HIPAA-164.312(e)(1)", title="Transmission Security", description="Implement measures to guard against unauthorized access to ePHI during transmission.", cwe_ids=[319, 295], rule_tags=["insecure-tls"]),
            ComplianceRequirement(req_id="HIPAA-164.312(b)", title="Audit Controls", description="Implement mechanisms to record and examine activity in systems containing ePHI.", cwe_ids=[778, 223], rule_tags=["logging", "monitoring"]),
        ],
    )


def _soc2() -> ComplianceFramework:
    return ComplianceFramework(
        framework_id="soc2",
        name="SOC 2",
        version="Type II",
        description="Service Organization Control 2 — Trust Services Criteria",
        requirements=[
            ComplianceRequirement(req_id="CC6.1", title="Logical and Physical Access", description="Implement logical access security over protected information assets.", cwe_ids=[284, 285, 862, 863, 269, 287, 306], rule_tags=["access-control", "authorization", "authentication"]),
            ComplianceRequirement(req_id="CC6.6", title="Security of External Connections", description="Restrict transmission, movement, and removal of information.", cwe_ids=[319, 295, 918], rule_tags=["insecure-tls", "ssrf"]),
            ComplianceRequirement(req_id="CC6.7", title="Data Movement Restriction", description="Restrict the transmission of data to authorized users.", cwe_ids=[200, 359, 532], rule_tags=["data-exposure"]),
            ComplianceRequirement(req_id="CC7.1", title="Detection of Changes", description="Detect unauthorized changes to infrastructure and software.", cwe_ids=[345, 353], rule_tags=["integrity"]),
            ComplianceRequirement(req_id="CC7.2", title="Monitoring for Anomalies", description="Monitor system components for anomalies.", cwe_ids=[778, 223, 117], rule_tags=["logging", "monitoring"]),
            ComplianceRequirement(req_id="CC8.1", title="Change Management", description="Authorize, design, develop, configure, document, test, approve, and implement changes.", rule_tags=["dependency", "sbom"]),
            ComplianceRequirement(req_id="CC3.2", title="Risk Assessment", description="Identify and assess risks.", cwe_ids=[1035, 1104], rule_tags=["dependency", "cve"]),
        ],
    )


# ── Registry ─────────────────────────────────────────────────────────

FRAMEWORK_BUILDERS: dict[str, Any] = {
    "owasp-top10-2021": _owasp_top10_2021,
    "cwe-top25-2023": _cwe_top25_2023,
    "sans-top25": _sans_top25,
    "pci-dss-v4": _pci_dss_v4,
    "hipaa": _hipaa,
    "soc2": _soc2,
}


# ── Engine ───────────────────────────────────────────────────────────

class ComplianceEngine:
    """Evaluate analysis results against compliance frameworks."""

    def __init__(self, framework_ids: list[str] | None = None):
        self.framework_ids = framework_ids or list(FRAMEWORK_BUILDERS.keys())

    @staticmethod
    def available_frameworks() -> list[dict[str, str]]:
        """Return list of available frameworks."""
        result = []
        for fid, builder in FRAMEWORK_BUILDERS.items():
            fw = builder()
            result.append({
                "id": fw.framework_id,
                "name": fw.name,
                "version": fw.version,
                "description": fw.description,
                "requirements_count": len(fw.requirements),
            })
        return result

    def evaluate(self, results: AnalysisResults) -> ComplianceReport:
        """Evaluate analysis results against selected frameworks."""
        issues = results.all_issues
        report = ComplianceReport()
        mapped_issue_ids: set[str] = set()

        for fid in self.framework_ids:
            builder = FRAMEWORK_BUILDERS.get(fid)
            if not builder:
                continue
            framework = builder()
            self._evaluate_framework(framework, issues, mapped_issue_ids)
            report.frameworks.append(framework)

        report.total_issues_mapped = len(mapped_issue_ids)
        return report

    def evaluate_issues(self, issues: list[dict[str, Any]]) -> ComplianceReport:
        """Evaluate a flat list of issue dicts against selected frameworks."""
        report = ComplianceReport()
        mapped_ids: set[str] = set()

        for fid in self.framework_ids:
            builder = FRAMEWORK_BUILDERS.get(fid)
            if not builder:
                continue
            framework = builder()
            self._evaluate_framework_dicts(framework, issues, mapped_ids)
            report.frameworks.append(framework)

        report.total_issues_mapped = len(mapped_ids)
        return report

    # ── internal ─────────────────────────────────────────────────

    def _evaluate_framework(
        self,
        framework: ComplianceFramework,
        issues: list[Issue],
        mapped_ids: set[str],
    ) -> None:
        for req in framework.requirements:
            matching: list[dict[str, Any]] = []
            for issue in issues:
                if self._issue_matches_requirement(issue, req):
                    matching.append({"rule_id": issue.rule_id, "message": issue.message, "file": str(issue.location.file_path), "line": issue.location.start_line})
                    mapped_ids.add(str(issue.id))

            if matching:
                req.status = ComplianceStatus.FAIL
                req.matched_issues = matching
            else:
                req.status = ComplianceStatus.PASS

    def _evaluate_framework_dicts(
        self,
        framework: ComplianceFramework,
        issues: list[dict[str, Any]],
        mapped_ids: set[str],
    ) -> None:
        for req in framework.requirements:
            matching: list[dict[str, Any]] = []
            for issue in issues:
                if self._dict_matches_requirement(issue, req):
                    matching.append({"rule_id": issue.get("rule_id", ""), "message": issue.get("message", "")})
                    mapped_ids.add(issue.get("id", str(id(issue))))

            if matching:
                req.status = ComplianceStatus.FAIL
                req.matched_issues = matching
            else:
                req.status = ComplianceStatus.PASS

    @staticmethod
    def _issue_matches_requirement(issue: Issue, req: ComplianceRequirement) -> bool:
        # Match by CWE
        if req.cwe_ids and issue.cwe_ids:
            if set(issue.cwe_ids) & set(req.cwe_ids):
                return True
        # Match by OWASP category
        if req.owasp_ids and issue.owasp_categories:
            for owasp_cat in issue.owasp_categories:
                for req_owasp in req.owasp_ids:
                    if req_owasp in owasp_cat:
                        return True
        # Match by rule tags
        if req.rule_tags and issue.tags:
            if set(issue.tags) & set(req.rule_tags):
                return True
        return False

    @staticmethod
    def _dict_matches_requirement(issue: dict[str, Any], req: ComplianceRequirement) -> bool:
        cwe_ids = issue.get("cwe_ids", [])
        if req.cwe_ids and cwe_ids:
            if set(cwe_ids) & set(req.cwe_ids):
                return True
        owasp = issue.get("owasp_categories", [])
        if req.owasp_ids and owasp:
            for owasp_cat in owasp:
                for req_owasp in req.owasp_ids:
                    if req_owasp in owasp_cat:
                        return True
        tags = issue.get("tags", [])
        if req.rule_tags and tags:
            if set(tags) & set(req.rule_tags):
                return True
        return False
