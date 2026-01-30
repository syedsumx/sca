"""AI-generated code vetting analyzer.

Scans source code for patterns commonly introduced by AI code generation
tools (ChatGPT, Copilot, Claude, etc.) including:
- Placeholder / stub code left incomplete
- Hallucinated APIs and imports
- Insecure defaults and hardcoded secrets
- Quality issues (broad exceptions, verbose logic)
- Truncated or incomplete output
- Over-engineered abstractions
- License compliance risks from copied code
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from codescope.analyzers.aivetting.patterns import (
    BUILTIN_PATTERNS,
    AICodePattern,
    PatternCategory,
    PatternSeverity,
)

logger = logging.getLogger(__name__)

# Map file extensions to language identifiers
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".rs": "rust",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
}


@dataclass
class AIVettingFinding:
    """A single finding from the AI vetting analysis."""

    pattern_id: str
    name: str
    category: str
    severity: str
    message: str
    file_path: str
    start_line: int
    end_line: int
    matched_text: str
    confidence: float


@dataclass
class AIVettingReport:
    """Report from the AI vetting analysis."""

    files_scanned: int = 0
    findings: list[AIVettingFinding] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    @property
    def total_findings(self) -> int:
        return len(self.findings)

    @property
    def risk_score(self) -> float:
        """Calculate an overall risk score (0-100) based on findings."""
        if not self.findings:
            return 0.0

        severity_weights = {
            "BLOCKER": 10.0,
            "CRITICAL": 7.0,
            "MAJOR": 4.0,
            "MINOR": 1.5,
            "INFO": 0.5,
        }

        total_weight = sum(
            severity_weights.get(f.severity, 1.0) * f.confidence
            for f in self.findings
        )

        # Normalize: cap at 100
        return min(100.0, total_weight)

    @property
    def risk_level(self) -> str:
        score = self.risk_score
        if score >= 50:
            return "HIGH"
        if score >= 20:
            return "MEDIUM"
        if score > 0:
            return "LOW"
        return "NONE"


class AIVettingAnalyzer:
    """Analyzer that detects AI-generated code issues."""

    def __init__(
        self,
        patterns: Optional[list[AICodePattern]] = None,
        min_confidence: float = 0.5,
        exclude_patterns: Optional[list[str]] = None,
    ):
        self.patterns = patterns or BUILTIN_PATTERNS
        self.min_confidence = min_confidence
        self.exclude_patterns = exclude_patterns or []

    def analyze_file(self, file_path: Path) -> list[AIVettingFinding]:
        """Analyze a single file for AI-generated code issues."""
        findings: list[AIVettingFinding] = []

        ext = file_path.suffix.lower()
        language = _EXT_TO_LANG.get(ext)
        if language is None:
            return findings

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, PermissionError) as exc:
            logger.debug("Cannot read %s: %s", file_path, exc)
            return findings

        lines = content.split("\n")

        for pattern in self.patterns:
            if pattern.confidence < self.min_confidence:
                continue

            if "all" not in pattern.languages and language not in pattern.languages:
                continue

            if self._is_excluded(pattern.pattern_id):
                continue

            try:
                compiled = re.compile(pattern.regex, re.IGNORECASE)
            except re.error as exc:
                logger.warning("Invalid regex in pattern %s: %s", pattern.pattern_id, exc)
                continue

            if pattern.multiline:
                for match in compiled.finditer(content):
                    start = content[:match.start()].count("\n") + 1
                    end = content[:match.end()].count("\n") + 1
                    matched = match.group(0)[:120]
                    findings.append(
                        AIVettingFinding(
                            pattern_id=pattern.pattern_id,
                            name=pattern.name,
                            category=pattern.category.value,
                            severity=pattern.severity.value,
                            message=pattern.message_template.replace("{match}", matched),
                            file_path=str(file_path),
                            start_line=start,
                            end_line=end,
                            matched_text=matched,
                            confidence=pattern.confidence,
                        )
                    )
            else:
                for line_num, line in enumerate(lines, 1):
                    match = compiled.search(line)
                    if match:
                        matched = match.group(0)[:120]
                        findings.append(
                            AIVettingFinding(
                                pattern_id=pattern.pattern_id,
                                name=pattern.name,
                                category=pattern.category.value,
                                severity=pattern.severity.value,
                                message=pattern.message_template.replace("{match}", matched),
                                file_path=str(file_path),
                                start_line=line_num,
                                end_line=line_num,
                                matched_text=matched,
                                confidence=pattern.confidence,
                            )
                        )

        return findings

    def analyze_directory(
        self,
        directory: Path,
        progress_callback: Optional[callable] = None,
    ) -> AIVettingReport:
        """Analyze all files in a directory tree."""
        report = AIVettingReport()

        source_files = self._collect_files(directory)
        total = len(source_files)

        for idx, file_path in enumerate(source_files):
            findings = self.analyze_file(file_path)
            report.findings.extend(findings)
            report.files_scanned += 1

            if progress_callback:
                progress_callback(idx + 1, total)

        # Build summary
        for finding in report.findings:
            cat = finding.category
            report.summary[cat] = report.summary.get(cat, 0) + 1

        return report

    def _collect_files(self, directory: Path) -> list[Path]:
        """Collect all source files from directory."""
        files = []
        for ext in _EXT_TO_LANG:
            files.extend(directory.rglob(f"*{ext}"))
        return sorted(files)

    def _is_excluded(self, pattern_id: str) -> bool:
        """Check if a pattern is excluded."""
        return pattern_id in self.exclude_patterns
