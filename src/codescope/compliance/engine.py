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


def _gdpr() -> ComplianceFramework:
    """GDPR - General Data Protection Regulation (EU)."""
    return ComplianceFramework(
        framework_id="gdpr",
        name="GDPR",
        version="2018",
        description="General Data Protection Regulation — EU Data Privacy Requirements",
        requirements=[
            ComplianceRequirement(
                req_id="GDPR-Art.5(1)(f)",
                title="Integrity and Confidentiality",
                description="Personal data must be processed securely with appropriate protection against unauthorized access, loss, or damage.",
                cwe_ids=[284, 285, 311, 312, 319, 326, 327, 522, 523],
                rule_tags=["access-control", "crypto", "insecure-tls", "data-exposure"],
            ),
            ComplianceRequirement(
                req_id="GDPR-Art.25",
                title="Data Protection by Design and Default",
                description="Implement appropriate technical measures to ensure data protection principles are embedded.",
                cwe_ids=[200, 209, 359, 497, 532, 538],
                rule_tags=["data-exposure", "logging", "debug"],
            ),
            ComplianceRequirement(
                req_id="GDPR-Art.32(1)(a)",
                title="Encryption of Personal Data",
                description="Pseudonymization and encryption of personal data.",
                cwe_ids=[311, 312, 319, 326, 327, 328, 330],
                rule_tags=["crypto", "weak-hash", "insecure-tls"],
            ),
            ComplianceRequirement(
                req_id="GDPR-Art.32(1)(b)",
                title="Confidentiality and Integrity",
                description="Ensure ongoing confidentiality, integrity, availability and resilience of processing systems.",
                cwe_ids=[287, 306, 798, 862, 863],
                rule_tags=["authentication", "authorization", "hardcoded-credential"],
            ),
            ComplianceRequirement(
                req_id="GDPR-Art.32(2)",
                title="Security Risk Assessment",
                description="Assess appropriate security level based on risks of accidental or unlawful destruction, loss, alteration, or unauthorized disclosure.",
                cwe_ids=[89, 78, 79, 94, 502],
                rule_tags=["injection", "sql-injection", "xss", "command-injection", "deserialization"],
            ),
            ComplianceRequirement(
                req_id="GDPR-Art.33",
                title="Breach Notification",
                description="Ability to detect, report, and investigate personal data breaches.",
                cwe_ids=[117, 223, 778],
                rule_tags=["logging", "monitoring"],
            ),
            ComplianceRequirement(
                req_id="GDPR-Art.17",
                title="Right to Erasure",
                description="Ensure mechanisms exist for complete data deletion on request.",
                cwe_ids=[212, 226, 459],
                rule_tags=["data-exposure"],
            ),
        ],
    )


def _nist_800_53() -> ComplianceFramework:
    """NIST 800-53 - Security and Privacy Controls."""
    return ComplianceFramework(
        framework_id="nist-800-53",
        name="NIST 800-53",
        version="Rev. 5",
        description="NIST Special Publication 800-53 — Security and Privacy Controls for Information Systems",
        requirements=[
            # Access Control Family (AC)
            ComplianceRequirement(
                req_id="AC-3",
                title="Access Enforcement",
                description="Enforce approved authorizations for logical access to information and system resources.",
                cwe_ids=[284, 285, 732, 862, 863],
                rule_tags=["access-control", "authorization"],
            ),
            ComplianceRequirement(
                req_id="AC-6",
                title="Least Privilege",
                description="Employ the principle of least privilege, allowing only authorized accesses.",
                cwe_ids=[250, 266, 269, 272, 274],
                rule_tags=["privilege", "access-control"],
            ),
            ComplianceRequirement(
                req_id="AC-17",
                title="Remote Access",
                description="Authorize, monitor, and control remote access methods.",
                cwe_ids=[319, 295, 300],
                rule_tags=["insecure-tls", "authentication"],
            ),
            # Audit and Accountability (AU)
            ComplianceRequirement(
                req_id="AU-2",
                title="Event Logging",
                description="Identify events to be logged and logging requirements.",
                cwe_ids=[117, 223, 778],
                rule_tags=["logging", "monitoring"],
            ),
            ComplianceRequirement(
                req_id="AU-9",
                title="Protection of Audit Information",
                description="Protect audit information and tools from unauthorized access and modification.",
                cwe_ids=[532, 117],
                rule_tags=["logging", "data-exposure"],
            ),
            # Identification and Authentication (IA)
            ComplianceRequirement(
                req_id="IA-2",
                title="Identification and Authentication",
                description="Uniquely identify and authenticate organizational users.",
                cwe_ids=[287, 288, 290, 306, 307],
                rule_tags=["authentication"],
            ),
            ComplianceRequirement(
                req_id="IA-5",
                title="Authenticator Management",
                description="Manage system authenticators with appropriate protections.",
                cwe_ids=[259, 260, 261, 521, 522, 798],
                rule_tags=["hardcoded-credential", "hardcoded-secret", "authentication"],
            ),
            # System and Communications Protection (SC)
            ComplianceRequirement(
                req_id="SC-8",
                title="Transmission Confidentiality and Integrity",
                description="Protect the confidentiality and integrity of transmitted information.",
                cwe_ids=[311, 319, 523],
                rule_tags=["insecure-tls", "crypto"],
            ),
            ComplianceRequirement(
                req_id="SC-12",
                title="Cryptographic Key Management",
                description="Establish and manage cryptographic keys.",
                cwe_ids=[320, 321, 322, 323, 324, 325],
                rule_tags=["crypto", "hardcoded-secret"],
            ),
            ComplianceRequirement(
                req_id="SC-13",
                title="Cryptographic Protection",
                description="Implement cryptographic mechanisms in accordance with applicable laws.",
                cwe_ids=[326, 327, 328, 329, 330, 338, 916],
                rule_tags=["crypto", "weak-hash", "insecure-random"],
            ),
            ComplianceRequirement(
                req_id="SC-28",
                title="Protection of Information at Rest",
                description="Protect the confidentiality and integrity of information at rest.",
                cwe_ids=[311, 312, 313, 314, 315, 316],
                rule_tags=["crypto", "data-exposure"],
            ),
            # System and Information Integrity (SI)
            ComplianceRequirement(
                req_id="SI-2",
                title="Flaw Remediation",
                description="Identify, report, and correct system flaws.",
                cwe_ids=[1035, 1104],
                rule_tags=["dependency", "cve"],
            ),
            ComplianceRequirement(
                req_id="SI-3",
                title="Malicious Code Protection",
                description="Detect and eradicate malicious code.",
                cwe_ids=[94, 95, 96, 502],
                rule_tags=["injection", "deserialization"],
            ),
            ComplianceRequirement(
                req_id="SI-10",
                title="Information Input Validation",
                description="Check validity of information inputs.",
                cwe_ids=[20, 74, 79, 89, 94],
                rule_tags=["input-validation", "injection", "sql-injection", "xss"],
            ),
            ComplianceRequirement(
                req_id="SI-11",
                title="Error Handling",
                description="Generate error messages with security context.",
                cwe_ids=[209, 210, 211],
                rule_tags=["error-handling", "data-exposure"],
            ),
        ],
    )


def _iso_27001() -> ComplianceFramework:
    """ISO 27001 - Information Security Management System."""
    return ComplianceFramework(
        framework_id="iso-27001",
        name="ISO 27001",
        version="2022",
        description="ISO/IEC 27001 — Information Security Management System Requirements",
        requirements=[
            # A.5 - Organizational Controls
            ComplianceRequirement(
                req_id="A.5.15",
                title="Access Control",
                description="Rules to control physical and logical access to information.",
                cwe_ids=[284, 285, 732, 862, 863],
                rule_tags=["access-control", "authorization"],
            ),
            ComplianceRequirement(
                req_id="A.5.17",
                title="Authentication Information",
                description="Allocation and management of authentication information.",
                cwe_ids=[259, 260, 521, 522, 798],
                rule_tags=["authentication", "hardcoded-credential", "hardcoded-secret"],
            ),
            ComplianceRequirement(
                req_id="A.5.33",
                title="Protection of Records",
                description="Records shall be protected from loss, destruction, and falsification.",
                cwe_ids=[117, 223, 778],
                rule_tags=["logging", "integrity"],
            ),
            # A.8 - Technological Controls
            ComplianceRequirement(
                req_id="A.8.2",
                title="Privileged Access Rights",
                description="Restrict and manage allocation of privileged access rights.",
                cwe_ids=[250, 266, 269, 272],
                rule_tags=["privilege", "access-control"],
            ),
            ComplianceRequirement(
                req_id="A.8.3",
                title="Information Access Restriction",
                description="Access to information and application system functions shall be restricted.",
                cwe_ids=[284, 285, 639, 862, 863],
                rule_tags=["access-control", "authorization"],
            ),
            ComplianceRequirement(
                req_id="A.8.5",
                title="Secure Authentication",
                description="Secure authentication technologies and procedures shall be implemented.",
                cwe_ids=[287, 288, 290, 294, 306, 307],
                rule_tags=["authentication"],
            ),
            ComplianceRequirement(
                req_id="A.8.7",
                title="Protection Against Malware",
                description="Implement controls against malware.",
                cwe_ids=[94, 95, 96, 434, 502],
                rule_tags=["injection", "deserialization", "file-upload"],
            ),
            ComplianceRequirement(
                req_id="A.8.9",
                title="Configuration Management",
                description="Configurations of hardware, software, and networks shall be established and managed.",
                cwe_ids=[16, 1004, 1032, 756],
                rule_tags=["misconfiguration"],
            ),
            ComplianceRequirement(
                req_id="A.8.12",
                title="Data Leakage Prevention",
                description="Detect and prevent unauthorized disclosure of information.",
                cwe_ids=[200, 209, 359, 497, 532, 538],
                rule_tags=["data-exposure", "logging"],
            ),
            ComplianceRequirement(
                req_id="A.8.15",
                title="Logging",
                description="Produce, store, protect, and analyze logs for security events.",
                cwe_ids=[117, 223, 532, 778],
                rule_tags=["logging", "monitoring"],
            ),
            ComplianceRequirement(
                req_id="A.8.20",
                title="Networks Security",
                description="Secure network infrastructure and services.",
                cwe_ids=[295, 300, 319, 918],
                rule_tags=["insecure-tls", "ssrf"],
            ),
            ComplianceRequirement(
                req_id="A.8.24",
                title="Use of Cryptography",
                description="Define and implement rules for effective use of cryptography.",
                cwe_ids=[326, 327, 328, 329, 330, 338],
                rule_tags=["crypto", "weak-hash", "insecure-random"],
            ),
            ComplianceRequirement(
                req_id="A.8.25",
                title="Secure Development Life Cycle",
                description="Rules for secure development of software and systems.",
                cwe_ids=[89, 78, 79, 94, 502],
                rule_tags=["injection", "sql-injection", "xss", "command-injection"],
            ),
            ComplianceRequirement(
                req_id="A.8.26",
                title="Application Security Requirements",
                description="Information security requirements shall be identified and specified.",
                cwe_ids=[20, 22, 352, 434],
                rule_tags=["input-validation", "path-traversal", "csrf", "file-upload"],
            ),
            ComplianceRequirement(
                req_id="A.8.28",
                title="Secure Coding",
                description="Secure coding principles shall be applied to software development.",
                cwe_ids=[89, 78, 79, 94, 119, 125, 787],
                rule_tags=["injection", "sql-injection", "xss", "command-injection"],
            ),
            ComplianceRequirement(
                req_id="A.8.31",
                title="Separation of Development, Test and Production",
                description="Development, testing, and production environments shall be separated.",
                cwe_ids=[489, 540],
                rule_tags=["debug", "misconfiguration"],
            ),
        ],
    )


def _ccpa() -> ComplianceFramework:
    """CCPA - California Consumer Privacy Act."""
    return ComplianceFramework(
        framework_id="ccpa",
        name="CCPA",
        version="2020",
        description="California Consumer Privacy Act — Consumer Data Privacy Requirements",
        requirements=[
            ComplianceRequirement(
                req_id="CCPA-1798.100",
                title="Right to Know",
                description="Consumers have the right to know what personal information is collected.",
                cwe_ids=[200, 359, 497],
                rule_tags=["data-exposure"],
            ),
            ComplianceRequirement(
                req_id="CCPA-1798.105",
                title="Right to Delete",
                description="Consumers can request deletion of personal information.",
                cwe_ids=[212, 226, 459],
                rule_tags=["data-exposure"],
            ),
            ComplianceRequirement(
                req_id="CCPA-1798.110",
                title="Right to Access",
                description="Consumers have the right to access their personal information.",
                cwe_ids=[284, 285, 862, 863],
                rule_tags=["access-control", "authorization"],
            ),
            ComplianceRequirement(
                req_id="CCPA-1798.150",
                title="Security Breach Liability",
                description="Implement reasonable security measures to protect personal information.",
                cwe_ids=[89, 78, 79, 287, 306, 311, 319, 326, 327, 798],
                rule_tags=["injection", "sql-injection", "xss", "authentication", "crypto", "insecure-tls", "hardcoded-credential"],
            ),
            ComplianceRequirement(
                req_id="CCPA-1798.81.5",
                title="Reasonable Security",
                description="Implement and maintain reasonable security procedures.",
                cwe_ids=[284, 285, 287, 306, 311, 319, 326, 327, 522],
                rule_tags=["access-control", "authentication", "crypto", "insecure-tls"],
            ),
        ],
    )


def _nist_csf() -> ComplianceFramework:
    """NIST Cybersecurity Framework."""
    return ComplianceFramework(
        framework_id="nist-csf",
        name="NIST CSF",
        version="2.0",
        description="NIST Cybersecurity Framework — Core Functions for Managing Cybersecurity Risk",
        requirements=[
            # Identify (ID)
            ComplianceRequirement(
                req_id="ID.AM-1",
                title="Asset Management",
                description="Physical devices and systems within the organization are inventoried.",
                rule_tags=["sbom", "dependency"],
            ),
            ComplianceRequirement(
                req_id="ID.RA-1",
                title="Risk Assessment",
                description="Asset vulnerabilities are identified and documented.",
                cwe_ids=[1035, 1104],
                rule_tags=["dependency", "cve"],
            ),
            # Protect (PR)
            ComplianceRequirement(
                req_id="PR.AA-1",
                title="Identity Management and Access Control",
                description="Identities and credentials are issued, managed, and verified.",
                cwe_ids=[259, 287, 306, 521, 522, 798],
                rule_tags=["authentication", "hardcoded-credential"],
            ),
            ComplianceRequirement(
                req_id="PR.AA-3",
                title="Access Control",
                description="Access permissions and authorizations are managed.",
                cwe_ids=[284, 285, 732, 862, 863],
                rule_tags=["access-control", "authorization"],
            ),
            ComplianceRequirement(
                req_id="PR.AA-5",
                title="Least Privilege",
                description="Access permissions use the principle of least privilege.",
                cwe_ids=[250, 266, 269, 272, 274],
                rule_tags=["privilege", "access-control"],
            ),
            ComplianceRequirement(
                req_id="PR.DS-1",
                title="Data-at-Rest Protection",
                description="Data-at-rest is protected.",
                cwe_ids=[311, 312, 313, 316],
                rule_tags=["crypto", "data-exposure"],
            ),
            ComplianceRequirement(
                req_id="PR.DS-2",
                title="Data-in-Transit Protection",
                description="Data-in-transit is protected.",
                cwe_ids=[295, 300, 319, 523],
                rule_tags=["insecure-tls", "crypto"],
            ),
            ComplianceRequirement(
                req_id="PR.DS-10",
                title="Data Integrity",
                description="Integrity checking mechanisms are used for software, firmware, and data.",
                cwe_ids=[345, 353, 354],
                rule_tags=["integrity"],
            ),
            ComplianceRequirement(
                req_id="PR.PS-1",
                title="Configuration Management",
                description="Security configuration standards are established and maintained.",
                cwe_ids=[16, 756, 1004, 1032],
                rule_tags=["misconfiguration"],
            ),
            ComplianceRequirement(
                req_id="PR.PS-6",
                title="Secure Software Development",
                description="Software is developed using secure coding practices.",
                cwe_ids=[20, 78, 79, 89, 94, 119, 502],
                rule_tags=["injection", "sql-injection", "xss", "command-injection", "input-validation"],
            ),
            # Detect (DE)
            ComplianceRequirement(
                req_id="DE.CM-1",
                title="Security Continuous Monitoring",
                description="Networks are monitored to detect potential cybersecurity events.",
                cwe_ids=[117, 223, 778],
                rule_tags=["logging", "monitoring"],
            ),
            ComplianceRequirement(
                req_id="DE.CM-6",
                title="Personnel Activity Monitoring",
                description="Personnel activity is monitored for anomalies.",
                cwe_ids=[532, 778],
                rule_tags=["logging", "monitoring"],
            ),
            # Respond (RS)
            ComplianceRequirement(
                req_id="RS.AN-3",
                title="Incident Analysis",
                description="Forensics are performed to support incident analysis.",
                cwe_ids=[117, 223, 778],
                rule_tags=["logging", "monitoring"],
            ),
        ],
    )


def _fedramp() -> ComplianceFramework:
    """FedRAMP - Federal Risk and Authorization Management Program."""
    return ComplianceFramework(
        framework_id="fedramp",
        name="FedRAMP",
        version="Rev. 5",
        description="Federal Risk and Authorization Management Program — Cloud Security Requirements",
        requirements=[
            ComplianceRequirement(
                req_id="AC-2",
                title="Account Management",
                description="Manage system accounts including establishing, activating, modifying, and disabling.",
                cwe_ids=[284, 285, 732, 798],
                rule_tags=["access-control", "hardcoded-credential"],
            ),
            ComplianceRequirement(
                req_id="AC-3",
                title="Access Enforcement",
                description="Enforce approved authorizations for logical access.",
                cwe_ids=[284, 285, 862, 863],
                rule_tags=["access-control", "authorization"],
            ),
            ComplianceRequirement(
                req_id="AC-6",
                title="Least Privilege",
                description="Employ least privilege principle.",
                cwe_ids=[250, 266, 269, 272],
                rule_tags=["privilege"],
            ),
            ComplianceRequirement(
                req_id="AU-2",
                title="Audit Events",
                description="Define auditable events and generate audit records.",
                cwe_ids=[117, 223, 778],
                rule_tags=["logging", "monitoring"],
            ),
            ComplianceRequirement(
                req_id="IA-2",
                title="Identification and Authentication",
                description="Uniquely identify and authenticate users.",
                cwe_ids=[287, 288, 306, 307],
                rule_tags=["authentication"],
            ),
            ComplianceRequirement(
                req_id="IA-5",
                title="Authenticator Management",
                description="Manage authenticators including passwords, tokens, and certificates.",
                cwe_ids=[259, 521, 522, 798],
                rule_tags=["hardcoded-credential", "hardcoded-secret", "authentication"],
            ),
            ComplianceRequirement(
                req_id="SC-8",
                title="Transmission Confidentiality",
                description="Protect transmitted information using encryption.",
                cwe_ids=[295, 300, 319, 523],
                rule_tags=["insecure-tls", "crypto"],
            ),
            ComplianceRequirement(
                req_id="SC-13",
                title="Cryptographic Protection",
                description="Implement FIPS-validated cryptography.",
                cwe_ids=[326, 327, 328, 329, 330, 338],
                rule_tags=["crypto", "weak-hash", "insecure-random"],
            ),
            ComplianceRequirement(
                req_id="SC-28",
                title="Protection of Information at Rest",
                description="Protect information at rest using encryption.",
                cwe_ids=[311, 312, 313, 316],
                rule_tags=["crypto", "data-exposure"],
            ),
            ComplianceRequirement(
                req_id="SI-2",
                title="Flaw Remediation",
                description="Identify, report, and correct system flaws.",
                cwe_ids=[1035, 1104],
                rule_tags=["dependency", "cve"],
            ),
            ComplianceRequirement(
                req_id="SI-10",
                title="Information Input Validation",
                description="Check the validity of all inputs.",
                cwe_ids=[20, 74, 78, 79, 89, 94],
                rule_tags=["input-validation", "injection", "sql-injection", "xss", "command-injection"],
            ),
        ],
    )


def _cis_controls() -> ComplianceFramework:
    """CIS Controls - Center for Internet Security Controls."""
    return ComplianceFramework(
        framework_id="cis-controls",
        name="CIS Controls",
        version="8.0",
        description="CIS Critical Security Controls — Best Practice Guidelines for Cyber Defense",
        requirements=[
            ComplianceRequirement(
                req_id="CIS-1",
                title="Inventory and Control of Enterprise Assets",
                description="Actively manage all enterprise assets connected to the infrastructure.",
                rule_tags=["sbom", "dependency"],
            ),
            ComplianceRequirement(
                req_id="CIS-2",
                title="Inventory and Control of Software Assets",
                description="Actively manage all software on the network.",
                cwe_ids=[1035, 1104],
                rule_tags=["sbom", "dependency", "cve"],
            ),
            ComplianceRequirement(
                req_id="CIS-3",
                title="Data Protection",
                description="Develop processes and technical controls to identify, classify, and protect data.",
                cwe_ids=[200, 311, 312, 319, 359, 532],
                rule_tags=["crypto", "insecure-tls", "data-exposure"],
            ),
            ComplianceRequirement(
                req_id="CIS-4",
                title="Secure Configuration of Enterprise Assets",
                description="Establish and maintain secure configurations.",
                cwe_ids=[16, 756, 1004, 1032],
                rule_tags=["misconfiguration"],
            ),
            ComplianceRequirement(
                req_id="CIS-5",
                title="Account Management",
                description="Use processes and tools to assign and manage credentials.",
                cwe_ids=[259, 260, 521, 522, 798],
                rule_tags=["authentication", "hardcoded-credential", "hardcoded-secret"],
            ),
            ComplianceRequirement(
                req_id="CIS-6",
                title="Access Control Management",
                description="Use processes and tools for access control management.",
                cwe_ids=[284, 285, 732, 862, 863],
                rule_tags=["access-control", "authorization"],
            ),
            ComplianceRequirement(
                req_id="CIS-7",
                title="Continuous Vulnerability Management",
                description="Continuously assess and track vulnerabilities.",
                cwe_ids=[1035, 1104],
                rule_tags=["dependency", "cve"],
            ),
            ComplianceRequirement(
                req_id="CIS-8",
                title="Audit Log Management",
                description="Collect, alert, review, and retain audit logs.",
                cwe_ids=[117, 223, 778],
                rule_tags=["logging", "monitoring"],
            ),
            ComplianceRequirement(
                req_id="CIS-10",
                title="Malware Defenses",
                description="Prevent or control installation and execution of malicious software.",
                cwe_ids=[94, 95, 96, 434, 502],
                rule_tags=["injection", "deserialization", "file-upload"],
            ),
            ComplianceRequirement(
                req_id="CIS-16",
                title="Application Software Security",
                description="Manage security life cycle of in-house developed and acquired software.",
                cwe_ids=[20, 78, 79, 89, 94, 119, 125, 787],
                rule_tags=["injection", "sql-injection", "xss", "command-injection", "input-validation"],
            ),
            ComplianceRequirement(
                req_id="CIS-16.1",
                title="Secure Software Development Process",
                description="Establish and maintain a secure application development process.",
                cwe_ids=[89, 78, 79, 94, 502],
                rule_tags=["injection", "sql-injection", "xss", "command-injection"],
            ),
            ComplianceRequirement(
                req_id="CIS-16.2",
                title="Software Component Analysis",
                description="Establish and maintain a process for third-party software components.",
                cwe_ids=[1035, 1104],
                rule_tags=["dependency", "cve", "sbom"],
            ),
            ComplianceRequirement(
                req_id="CIS-16.4",
                title="Static Application Security Testing",
                description="Perform static analysis as part of the development process.",
                cwe_ids=[89, 78, 79, 94],
                rule_tags=["injection", "sql-injection", "xss", "command-injection"],
            ),
        ],
    )


def _owasp_asvs() -> ComplianceFramework:
    """OWASP ASVS - Application Security Verification Standard."""
    return ComplianceFramework(
        framework_id="owasp-asvs",
        name="OWASP ASVS",
        version="4.0",
        description="OWASP Application Security Verification Standard — Comprehensive Security Requirements",
        requirements=[
            # V1 - Architecture, Design and Threat Modeling
            ComplianceRequirement(
                req_id="V1.2",
                title="Authentication Architecture",
                description="Verify authentication is implemented consistently across the application.",
                cwe_ids=[287, 306, 307],
                rule_tags=["authentication"],
            ),
            # V2 - Authentication
            ComplianceRequirement(
                req_id="V2.1",
                title="Password Security",
                description="Verify passwords are stored using approved hashing algorithms.",
                cwe_ids=[259, 260, 261, 521, 916],
                rule_tags=["authentication", "weak-hash", "hardcoded-credential"],
            ),
            ComplianceRequirement(
                req_id="V2.5",
                title="Credential Recovery",
                description="Verify credential recovery mechanisms are secure.",
                cwe_ids=[287, 620, 640],
                rule_tags=["authentication"],
            ),
            # V3 - Session Management
            ComplianceRequirement(
                req_id="V3.2",
                title="Session Binding",
                description="Verify sessions are properly bound to the user and context.",
                cwe_ids=[384, 613],
                rule_tags=["session"],
            ),
            ComplianceRequirement(
                req_id="V3.5",
                title="Token-based Session Management",
                description="Verify token-based session management is secure.",
                cwe_ids=[287, 346, 352],
                rule_tags=["session", "csrf"],
            ),
            # V4 - Access Control
            ComplianceRequirement(
                req_id="V4.1",
                title="General Access Control",
                description="Verify access control is enforced on the server side.",
                cwe_ids=[284, 285, 639, 862, 863],
                rule_tags=["access-control", "authorization"],
            ),
            ComplianceRequirement(
                req_id="V4.2",
                title="Operation Level Access Control",
                description="Verify users can only access functions they have permissions for.",
                cwe_ids=[284, 285, 862, 863],
                rule_tags=["access-control", "authorization"],
            ),
            # V5 - Validation, Sanitization and Encoding
            ComplianceRequirement(
                req_id="V5.1",
                title="Input Validation",
                description="Verify input validation is performed on the server side.",
                cwe_ids=[20, 74, 89, 116],
                rule_tags=["input-validation", "injection"],
            ),
            ComplianceRequirement(
                req_id="V5.2",
                title="Sanitization and Sandboxing",
                description="Verify untrusted HTML is properly sanitized.",
                cwe_ids=[79, 80, 83],
                rule_tags=["xss"],
            ),
            ComplianceRequirement(
                req_id="V5.3",
                title="Output Encoding",
                description="Verify output encoding prevents injection attacks.",
                cwe_ids=[79, 80, 89, 94],
                rule_tags=["xss", "sql-injection", "injection"],
            ),
            ComplianceRequirement(
                req_id="V5.5",
                title="Deserialization Prevention",
                description="Verify deserialization of untrusted data is avoided.",
                cwe_ids=[502],
                rule_tags=["deserialization"],
            ),
            # V6 - Stored Cryptography
            ComplianceRequirement(
                req_id="V6.1",
                title="Data Classification",
                description="Verify regulated data is stored encrypted.",
                cwe_ids=[311, 312, 313],
                rule_tags=["crypto", "data-exposure"],
            ),
            ComplianceRequirement(
                req_id="V6.2",
                title="Algorithms",
                description="Verify strong cryptographic algorithms are used.",
                cwe_ids=[326, 327, 328, 916],
                rule_tags=["crypto", "weak-hash"],
            ),
            ComplianceRequirement(
                req_id="V6.3",
                title="Random Values",
                description="Verify cryptographically secure random values are used.",
                cwe_ids=[330, 338],
                rule_tags=["insecure-random"],
            ),
            ComplianceRequirement(
                req_id="V6.4",
                title="Secret Management",
                description="Verify secrets are stored and managed securely.",
                cwe_ids=[259, 260, 321, 798],
                rule_tags=["hardcoded-secret", "hardcoded-credential"],
            ),
            # V7 - Error Handling and Logging
            ComplianceRequirement(
                req_id="V7.1",
                title="Log Content",
                description="Verify sensitive data is not logged.",
                cwe_ids=[117, 532],
                rule_tags=["logging", "data-exposure"],
            ),
            ComplianceRequirement(
                req_id="V7.4",
                title="Error Handling",
                description="Verify error handling does not expose sensitive information.",
                cwe_ids=[209, 210, 211],
                rule_tags=["error-handling", "data-exposure"],
            ),
            # V8 - Data Protection
            ComplianceRequirement(
                req_id="V8.1",
                title="General Data Protection",
                description="Verify sensitive data is protected from unauthorized access.",
                cwe_ids=[200, 359, 497, 538],
                rule_tags=["data-exposure"],
            ),
            # V9 - Communications
            ComplianceRequirement(
                req_id="V9.1",
                title="Client Communications Security",
                description="Verify TLS is used for all client connections.",
                cwe_ids=[295, 300, 319, 523],
                rule_tags=["insecure-tls"],
            ),
            ComplianceRequirement(
                req_id="V9.2",
                title="Server Communications Security",
                description="Verify server-to-server communications use TLS.",
                cwe_ids=[295, 300, 319],
                rule_tags=["insecure-tls"],
            ),
            # V10 - Malicious Code
            ComplianceRequirement(
                req_id="V10.2",
                title="Malicious Code Search",
                description="Verify application source code does not contain malicious code.",
                cwe_ids=[506, 507, 511],
                rule_tags=["backdoor"],
            ),
            # V11 - Business Logic
            ComplianceRequirement(
                req_id="V11.1",
                title="Business Logic Security",
                description="Verify business logic flows are processed in sequential step order.",
                cwe_ids=[362, 367],
                rule_tags=["race-condition"],
            ),
            # V12 - Files and Resources
            ComplianceRequirement(
                req_id="V12.1",
                title="File Upload",
                description="Verify file upload functionality does not introduce vulnerabilities.",
                cwe_ids=[22, 400, 434],
                rule_tags=["file-upload", "path-traversal"],
            ),
            ComplianceRequirement(
                req_id="V12.3",
                title="File Execution",
                description="Verify user-submitted files are not executed.",
                cwe_ids=[78, 94, 434],
                rule_tags=["command-injection", "injection", "file-upload"],
            ),
            ComplianceRequirement(
                req_id="V12.4",
                title="File Storage",
                description="Verify files are stored securely.",
                cwe_ids=[22, 73],
                rule_tags=["path-traversal"],
            ),
            ComplianceRequirement(
                req_id="V12.5",
                title="File Download",
                description="Verify file download does not introduce vulnerabilities.",
                cwe_ids=[22, 918],
                rule_tags=["path-traversal", "ssrf"],
            ),
            ComplianceRequirement(
                req_id="V12.6",
                title="SSRF Protection",
                description="Verify the application is protected against SSRF attacks.",
                cwe_ids=[918],
                rule_tags=["ssrf"],
            ),
            # V13 - API and Web Service
            ComplianceRequirement(
                req_id="V13.1",
                title="Generic Web Service Security",
                description="Verify API endpoints are secured.",
                cwe_ids=[284, 285, 287, 306],
                rule_tags=["access-control", "authentication"],
            ),
            ComplianceRequirement(
                req_id="V13.2",
                title="RESTful Web Service",
                description="Verify RESTful services use anti-CSRF mechanisms.",
                cwe_ids=[352],
                rule_tags=["csrf"],
            ),
            # V14 - Configuration
            ComplianceRequirement(
                req_id="V14.2",
                title="Dependency",
                description="Verify third-party components are from trusted sources and up-to-date.",
                cwe_ids=[1035, 1104],
                rule_tags=["dependency", "cve"],
            ),
            ComplianceRequirement(
                req_id="V14.3",
                title="Unintended Security Disclosure",
                description="Verify error messages and debug information are not exposed.",
                cwe_ids=[209, 489, 540],
                rule_tags=["debug", "error-handling", "data-exposure"],
            ),
        ],
    )


def _owasp_masvs() -> ComplianceFramework:
    """OWASP MASVS - Mobile Application Security Verification Standard."""
    return ComplianceFramework(
        framework_id="owasp-masvs",
        name="OWASP MASVS",
        version="2.0",
        description="OWASP Mobile Application Security Verification Standard — Mobile App Security Requirements",
        requirements=[
            # MASVS-STORAGE
            ComplianceRequirement(
                req_id="MASVS-STORAGE-1",
                title="Secure Data Storage",
                description="The app securely stores sensitive data.",
                cwe_ids=[200, 311, 312, 313, 316, 359, 532],
                rule_tags=["crypto", "data-exposure", "logging"],
            ),
            ComplianceRequirement(
                req_id="MASVS-STORAGE-2",
                title="Data Leakage Prevention",
                description="The app prevents leakage of sensitive data.",
                cwe_ids=[200, 359, 497, 532, 538],
                rule_tags=["data-exposure", "logging"],
            ),
            # MASVS-CRYPTO
            ComplianceRequirement(
                req_id="MASVS-CRYPTO-1",
                title="Strong Cryptography",
                description="The app employs current strong cryptography.",
                cwe_ids=[326, 327, 328, 329, 330, 338, 916],
                rule_tags=["crypto", "weak-hash", "insecure-random"],
            ),
            ComplianceRequirement(
                req_id="MASVS-CRYPTO-2",
                title="Cryptography Configuration",
                description="The app performs key management according to industry best practices.",
                cwe_ids=[320, 321, 322, 324, 325],
                rule_tags=["crypto", "hardcoded-secret"],
            ),
            # MASVS-AUTH
            ComplianceRequirement(
                req_id="MASVS-AUTH-1",
                title="Authentication",
                description="The app uses secure authentication mechanisms.",
                cwe_ids=[287, 288, 306, 307, 521],
                rule_tags=["authentication"],
            ),
            ComplianceRequirement(
                req_id="MASVS-AUTH-2",
                title="Session Management",
                description="The app performs secure session management.",
                cwe_ids=[384, 613],
                rule_tags=["session"],
            ),
            ComplianceRequirement(
                req_id="MASVS-AUTH-3",
                title="Password Policy",
                description="The app implements a secure password policy.",
                cwe_ids=[521, 522],
                rule_tags=["authentication"],
            ),
            # MASVS-NETWORK
            ComplianceRequirement(
                req_id="MASVS-NETWORK-1",
                title="Secure Connections",
                description="The app secures all network traffic.",
                cwe_ids=[295, 300, 319, 523],
                rule_tags=["insecure-tls"],
            ),
            ComplianceRequirement(
                req_id="MASVS-NETWORK-2",
                title="TLS Configuration",
                description="The app performs proper TLS configuration and verification.",
                cwe_ids=[295, 297],
                rule_tags=["insecure-tls"],
            ),
            # MASVS-PLATFORM
            ComplianceRequirement(
                req_id="MASVS-PLATFORM-1",
                title="Platform Interaction",
                description="The app uses platform APIs securely.",
                cwe_ids=[78, 89, 94, 502, 927],
                rule_tags=["injection", "sql-injection", "command-injection", "deserialization"],
            ),
            ComplianceRequirement(
                req_id="MASVS-PLATFORM-2",
                title="WebView Security",
                description="The app secures WebView interactions.",
                cwe_ids=[79, 749, 919],
                rule_tags=["xss"],
            ),
            # MASVS-CODE
            ComplianceRequirement(
                req_id="MASVS-CODE-1",
                title="Security Best Practices",
                description="The app follows secure coding best practices.",
                cwe_ids=[20, 78, 79, 89, 94, 119],
                rule_tags=["injection", "sql-injection", "xss", "command-injection", "input-validation"],
            ),
            ComplianceRequirement(
                req_id="MASVS-CODE-2",
                title="Input Validation",
                description="The app validates and sanitizes all user input.",
                cwe_ids=[20, 22, 74, 79, 89, 94],
                rule_tags=["input-validation", "injection", "path-traversal"],
            ),
            ComplianceRequirement(
                req_id="MASVS-CODE-3",
                title="Third-Party Libraries",
                description="The app uses up-to-date third-party libraries.",
                cwe_ids=[1035, 1104],
                rule_tags=["dependency", "cve"],
            ),
            ComplianceRequirement(
                req_id="MASVS-CODE-4",
                title="Hardcoded Secrets",
                description="The app does not contain hardcoded sensitive values.",
                cwe_ids=[259, 321, 798],
                rule_tags=["hardcoded-credential", "hardcoded-secret"],
            ),
        ],
    )


def _mitre_attack() -> ComplianceFramework:
    """MITRE ATT&CK - Adversary Tactics, Techniques, and Common Knowledge."""
    return ComplianceFramework(
        framework_id="mitre-attack",
        name="MITRE ATT&CK",
        version="14.0",
        description="MITRE ATT&CK Framework — Adversarial Tactics, Techniques, and Procedures Mapping",
        requirements=[
            # Initial Access
            ComplianceRequirement(
                req_id="T1190",
                title="Exploit Public-Facing Application",
                description="Adversaries may exploit vulnerabilities in internet-facing systems.",
                cwe_ids=[20, 78, 79, 89, 94, 502, 918],
                owasp_ids=["A03"],
                rule_tags=["injection", "sql-injection", "xss", "command-injection", "deserialization", "ssrf"],
            ),
            ComplianceRequirement(
                req_id="T1078",
                title="Valid Accounts",
                description="Adversaries may obtain and abuse valid account credentials.",
                cwe_ids=[259, 287, 306, 521, 522, 798],
                owasp_ids=["A07"],
                rule_tags=["hardcoded-credential", "authentication"],
            ),
            # Execution
            ComplianceRequirement(
                req_id="T1059",
                title="Command and Scripting Interpreter",
                description="Adversaries may abuse command and script interpreters to execute commands.",
                cwe_ids=[77, 78, 94, 95, 96],
                owasp_ids=["A03"],
                rule_tags=["command-injection", "injection"],
            ),
            ComplianceRequirement(
                req_id="T1203",
                title="Exploitation for Client Execution",
                description="Adversaries may exploit software vulnerabilities in client applications.",
                cwe_ids=[79, 94, 502],
                owasp_ids=["A03", "A08"],
                rule_tags=["xss", "injection", "deserialization"],
            ),
            # Persistence
            ComplianceRequirement(
                req_id="T1136",
                title="Create Account",
                description="Adversaries may create accounts to maintain access.",
                cwe_ids=[284, 285, 862],
                owasp_ids=["A01"],
                rule_tags=["access-control"],
            ),
            ComplianceRequirement(
                req_id="T1505.003",
                title="Web Shell",
                description="Adversaries may install web shells on web servers.",
                cwe_ids=[434, 94],
                rule_tags=["file-upload", "injection"],
            ),
            # Privilege Escalation
            ComplianceRequirement(
                req_id="T1068",
                title="Exploitation for Privilege Escalation",
                description="Adversaries may exploit software vulnerabilities to escalate privileges.",
                cwe_ids=[250, 266, 269, 272, 274],
                rule_tags=["privilege"],
            ),
            ComplianceRequirement(
                req_id="T1548",
                title="Abuse Elevation Control Mechanism",
                description="Adversaries may circumvent mechanisms designed to control elevate privileges.",
                cwe_ids=[269, 284, 285, 862, 863],
                owasp_ids=["A01"],
                rule_tags=["privilege", "authorization"],
            ),
            # Defense Evasion
            ComplianceRequirement(
                req_id="T1562.006",
                title="Indicator Blocking",
                description="Adversaries may block logging or security tool output.",
                cwe_ids=[117, 223, 778],
                owasp_ids=["A09"],
                rule_tags=["logging"],
            ),
            ComplianceRequirement(
                req_id="T1140",
                title="Deobfuscate/Decode Files or Information",
                description="Adversaries may use obfuscated files to hide malicious content.",
                cwe_ids=[502, 506],
                rule_tags=["deserialization"],
            ),
            # Credential Access
            ComplianceRequirement(
                req_id="T1552",
                title="Unsecured Credentials",
                description="Adversaries may search for insecurely stored credentials.",
                cwe_ids=[256, 257, 259, 260, 312, 522, 798],
                owasp_ids=["A02", "A07"],
                rule_tags=["hardcoded-credential", "hardcoded-secret", "data-exposure"],
            ),
            ComplianceRequirement(
                req_id="T1110",
                title="Brute Force",
                description="Adversaries may use brute force techniques to gain access.",
                cwe_ids=[307, 521],
                owasp_ids=["A07"],
                rule_tags=["authentication"],
            ),
            # Discovery
            ComplianceRequirement(
                req_id="T1083",
                title="File and Directory Discovery",
                description="Adversaries may enumerate files and directories.",
                cwe_ids=[22, 200, 538, 548],
                owasp_ids=["A01"],
                rule_tags=["path-traversal", "data-exposure"],
            ),
            # Lateral Movement
            ComplianceRequirement(
                req_id="T1210",
                title="Exploitation of Remote Services",
                description="Adversaries may exploit remote services to gain access.",
                cwe_ids=[78, 89, 94, 918],
                owasp_ids=["A03"],
                rule_tags=["injection", "sql-injection", "command-injection", "ssrf"],
            ),
            # Collection
            ComplianceRequirement(
                req_id="T1005",
                title="Data from Local System",
                description="Adversaries may search for and collect data from local systems.",
                cwe_ids=[200, 312, 359, 532],
                rule_tags=["data-exposure"],
            ),
            ComplianceRequirement(
                req_id="T1530",
                title="Data from Cloud Storage",
                description="Adversaries may access data from cloud storage.",
                cwe_ids=[284, 285, 311, 862],
                rule_tags=["access-control", "crypto"],
            ),
            # Exfiltration
            ComplianceRequirement(
                req_id="T1048",
                title="Exfiltration Over Alternative Protocol",
                description="Adversaries may steal data by exfiltrating over different protocols.",
                cwe_ids=[200, 319, 359],
                rule_tags=["data-exposure", "insecure-tls"],
            ),
            # Impact
            ComplianceRequirement(
                req_id="T1565",
                title="Data Manipulation",
                description="Adversaries may manipulate data to impact availability or integrity.",
                cwe_ids=[89, 345, 353],
                owasp_ids=["A03"],
                rule_tags=["sql-injection", "integrity"],
            ),
        ],
    )


# ── Registry ─────────────────────────────────────────────────────────

FRAMEWORK_BUILDERS: dict[str, Any] = {
    # Security Standards
    "owasp-top10-2021": _owasp_top10_2021,
    "cwe-top25-2023": _cwe_top25_2023,
    "sans-top25": _sans_top25,
    # OWASP Verification Standards
    "owasp-asvs": _owasp_asvs,
    "owasp-masvs": _owasp_masvs,
    # Industry Regulations
    "pci-dss-v4": _pci_dss_v4,
    "hipaa": _hipaa,
    "soc2": _soc2,
    # Data Privacy
    "gdpr": _gdpr,
    "ccpa": _ccpa,
    # Government & Federal
    "nist-800-53": _nist_800_53,
    "nist-csf": _nist_csf,
    "fedramp": _fedramp,
    # International Standards
    "iso-27001": _iso_27001,
    # Best Practices
    "cis-controls": _cis_controls,
    # Threat Intelligence
    "mitre-attack": _mitre_attack,
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
