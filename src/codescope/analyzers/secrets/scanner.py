"""Detect hardcoded secrets, API keys, tokens, and credentials in source code."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Secret detection patterns ───────────────────────────────────────
_SECRET_PATTERNS: list[dict] = [
    # AWS
    {"id": "aws-access-key", "name": "AWS Access Key", "pattern": r"(?:A3T[A-Z0-9]|AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}", "severity": "CRITICAL"},
    {"id": "aws-secret-key", "name": "AWS Secret Key", "pattern": r"(?i)aws[_\-]?secret[_\-]?access[_\-]?key[\s=:\"']+[A-Za-z0-9/+=]{40}", "severity": "CRITICAL"},
    # GitHub
    {"id": "github-token", "name": "GitHub Token", "pattern": r"gh[pousr]_[A-Za-z0-9_]{36,255}", "severity": "CRITICAL"},
    {"id": "github-pat", "name": "GitHub Personal Access Token", "pattern": r"github_pat_[A-Za-z0-9_]{22,255}", "severity": "CRITICAL"},
    # GitLab
    {"id": "gitlab-token", "name": "GitLab Token", "pattern": r"glpat-[A-Za-z0-9\-_]{20,}", "severity": "CRITICAL"},
    # Slack
    {"id": "slack-token", "name": "Slack Token", "pattern": r"xox[baprs]-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24,}", "severity": "CRITICAL"},
    {"id": "slack-webhook", "name": "Slack Webhook", "pattern": r"https://hooks\.slack\.com/services/T[A-Z0-9]{8,}/B[A-Z0-9]{8,}/[a-zA-Z0-9]{24,}", "severity": "MAJOR"},
    # Stripe
    {"id": "stripe-secret", "name": "Stripe Secret Key", "pattern": r"sk_live_[0-9a-zA-Z]{24,}", "severity": "CRITICAL"},
    {"id": "stripe-publishable", "name": "Stripe Publishable Key", "pattern": r"pk_live_[0-9a-zA-Z]{24,}", "severity": "MAJOR"},
    # Google
    {"id": "google-api-key", "name": "Google API Key", "pattern": r"AIza[0-9A-Za-z\-_]{35}", "severity": "CRITICAL"},
    {"id": "google-oauth", "name": "Google OAuth Secret", "pattern": r"(?i)google[_\-]?oauth[_\-]?secret[\s=:\"']+[A-Za-z0-9\-_]{24,}", "severity": "CRITICAL"},
    # Azure
    {"id": "azure-storage-key", "name": "Azure Storage Key", "pattern": r"(?i)(?:account[_\-]?key|storage[_\-]?key)[\s=:\"']+[A-Za-z0-9+/]{86}==", "severity": "CRITICAL"},
    {"id": "azure-connection", "name": "Azure Connection String", "pattern": r"DefaultEndpointsProtocol=https?;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/]{86}==", "severity": "CRITICAL"},
    # Generic high-entropy secrets
    {"id": "private-key", "name": "Private Key", "pattern": r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", "severity": "CRITICAL"},
    {"id": "generic-api-key", "name": "Generic API Key", "pattern": r'(?i)(?:api[_\-]?key|apikey|api[_\-]?token)[\s]*[=:]\s*["\']?[A-Za-z0-9\-_\.]{20,}["\']?', "severity": "MAJOR"},
    {"id": "generic-secret", "name": "Generic Secret", "pattern": r'(?i)(?:secret|password|passwd|pwd|token|auth[_\-]?token)[\s]*[=:]\s*["\'][^\s"\']{8,}["\']', "severity": "MAJOR"},
    {"id": "jwt-token", "name": "JWT Token", "pattern": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", "severity": "MAJOR"},
    # Database URLs with credentials
    {"id": "database-url", "name": "Database URL with Credentials", "pattern": r"(?i)(?:postgres|mysql|mongodb|redis|amqp)://[^:]+:[^@]+@[^\s\"']+", "severity": "CRITICAL"},
    # SendGrid
    {"id": "sendgrid-key", "name": "SendGrid API Key", "pattern": r"SG\.[A-Za-z0-9\-_]{22}\.[A-Za-z0-9\-_]{43}", "severity": "CRITICAL"},
    # Twilio
    {"id": "twilio-key", "name": "Twilio API Key", "pattern": r"SK[0-9a-fA-F]{32}", "severity": "MAJOR"},
    # npm
    {"id": "npm-token", "name": "npm Access Token", "pattern": r"npm_[A-Za-z0-9]{36}", "severity": "CRITICAL"},
    # PyPI
    {"id": "pypi-token", "name": "PyPI API Token", "pattern": r"pypi-[A-Za-z0-9\-_]{50,}", "severity": "CRITICAL"},
]

_IGNORE_PATHS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "vendor",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
}

_BINARY_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "ico", "svg", "woff", "woff2", "ttf",
    "eot", "pdf", "zip", "tar", "gz", "bz2", "exe", "dll", "so", "dylib",
}

_ALLOWLIST_PATTERNS = [
    re.compile(r"(?:example|sample|test|dummy|fake|placeholder|CHANGE_ME|xxx|your[_-])", re.IGNORECASE),
    re.compile(r"<[^>]+>"),  # Template placeholders like <YOUR_KEY>
    re.compile(r"\$\{[^}]+\}"),  # Variable interpolation
]


@dataclass
class SecretFinding:
    """A detected secret in source code."""

    rule_id: str
    rule_name: str
    severity: str
    file: str
    line: int
    column: int = 0
    matched_text: str = ""  # redacted
    entropy: float = 0.0

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "matched_text": self.matched_text,
            "entropy": round(self.entropy, 2),
        }


class SecretScanner:
    """Scan files and directories for hardcoded secrets."""

    def __init__(self, include_entropy: bool = True, min_entropy: float = 3.5) -> None:
        self._compiled = [
            {**p, "_re": re.compile(p["pattern"])}
            for p in _SECRET_PATTERNS
        ]
        self.include_entropy = include_entropy
        self.min_entropy = min_entropy

    def scan_file(self, file_path: str, content: Optional[str] = None) -> list[SecretFinding]:
        """Scan a single file for secrets."""
        path = Path(file_path)
        if path.suffix.lstrip(".") in _BINARY_EXTENSIONS:
            return []
        if content is None:
            try:
                content = path.read_text(errors="replace")
            except OSError:
                return []

        findings: list[SecretFinding] = []
        lines = content.split("\n")

        for pattern in self._compiled:
            regex = pattern["_re"]
            for i, line_text in enumerate(lines, 1):
                for match in regex.finditer(line_text):
                    matched = match.group()
                    # Skip allowlisted patterns
                    if any(aw.search(matched) for aw in _ALLOWLIST_PATTERNS):
                        continue
                    # Redact the secret value
                    redacted = matched[:8] + "***" + matched[-4:] if len(matched) > 16 else "***"
                    entropy = _shannon_entropy(matched)
                    findings.append(SecretFinding(
                        rule_id=pattern["id"],
                        rule_name=pattern["name"],
                        severity=pattern["severity"],
                        file=file_path,
                        line=i,
                        column=match.start(),
                        matched_text=redacted,
                        entropy=entropy,
                    ))
        return findings

    def scan_directory(self, dir_path: str | Path) -> list[SecretFinding]:
        """Recursively scan a directory for secrets."""
        dir_path = Path(dir_path)
        all_findings: list[SecretFinding] = []

        for fpath in dir_path.rglob("*"):
            if not fpath.is_file():
                continue
            if any(part in _IGNORE_PATHS for part in fpath.parts):
                continue
            if fpath.suffix.lstrip(".") in _BINARY_EXTENSIONS:
                continue
            if fpath.stat().st_size > 2 * 1024 * 1024:  # skip files > 2MB
                continue
            all_findings.extend(self.scan_file(str(fpath)))

        return all_findings

    def scan_git_history(self, repo_path: str | Path, max_commits: int = 50) -> list[SecretFinding]:
        """Scan recent git commit diffs for secrets."""
        import subprocess
        repo_path = Path(repo_path)
        findings: list[SecretFinding] = []

        try:
            result = subprocess.run(
                ["git", "log", f"--max-count={max_commits}", "--diff-filter=A",
                 "--pretty=format:%H", "--name-only"],
                cwd=str(repo_path), capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return findings

            current_commit = ""
            for line in result.stdout.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if len(line) == 40 and all(c in "0123456789abcdef" for c in line):
                    current_commit = line[:8]
                    continue
                file_path = repo_path / line
                if file_path.exists() and file_path.is_file():
                    for f in self.scan_file(str(file_path)):
                        f.file = f"{f.file} (commit: {current_commit})"
                        findings.append(f)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

        return findings


def _shannon_entropy(s: str) -> float:
    """Calculate Shannon entropy of a string."""
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for c in s:
        freq[c] = freq.get(c, 0) + 1
    length = len(s)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())
