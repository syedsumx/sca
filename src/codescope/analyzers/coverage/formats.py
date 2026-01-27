"""Coverage report format parsers."""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from codescope.analyzers.coverage.parser import (
    CoverageParser,
    CoverageReport,
    FileCoverage,
    LineCoverage,
)


class CoberturaParser(CoverageParser):
    """Parser for Cobertura XML format (used by many tools)."""

    format_name = "cobertura"

    def can_parse(self, file_path: Path) -> bool:
        """Check if file is Cobertura format."""
        if not file_path.suffix == ".xml":
            return False
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")[:1000]
            return "cobertura" in content.lower() or "<coverage" in content
        except Exception:
            return False

    def parse(self, file_path: Path) -> CoverageReport:
        """Parse Cobertura XML file."""
        report = CoverageReport(format=self.format_name, source_file=str(file_path))

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            # Find all classes/files
            for package in root.findall(".//package"):
                for cls in package.findall(".//class"):
                    filename = cls.get("filename", "")
                    if not filename:
                        continue

                    file_cov = FileCoverage(file_path=filename)

                    # Parse lines
                    for line in cls.findall(".//line"):
                        line_num = int(line.get("number", 0))
                        hits = int(line.get("hits", 0))
                        is_branch = line.get("branch", "false").lower() == "true"

                        branch_coverage = None
                        if is_branch:
                            condition_coverage = line.get("condition-coverage", "")
                            # Parse "50% (1/2)" format
                            match = re.search(r"(\d+)%", condition_coverage)
                            if match:
                                branch_coverage = float(match.group(1))

                        file_cov.lines.append(LineCoverage(
                            line_number=line_num,
                            hits=hits,
                            is_branch=is_branch,
                            branch_coverage=branch_coverage,
                        ))

                    if file_cov.lines:
                        report.files.append(file_cov)

        except ET.ParseError:
            pass

        return report


class LcovParser(CoverageParser):
    """Parser for LCOV info format."""

    format_name = "lcov"

    def can_parse(self, file_path: Path) -> bool:
        """Check if file is LCOV format."""
        if file_path.suffix not in (".info", ".lcov"):
            if file_path.name not in ("lcov.info", "coverage.info"):
                return False
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")[:500]
            return "SF:" in content or "TN:" in content
        except Exception:
            return False

    def parse(self, file_path: Path) -> CoverageReport:
        """Parse LCOV info file."""
        report = CoverageReport(format=self.format_name, source_file=str(file_path))

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")

            current_file: FileCoverage | None = None

            for line in content.split("\n"):
                line = line.strip()

                if line.startswith("SF:"):
                    # Start of new file
                    filename = line[3:]
                    current_file = FileCoverage(file_path=filename)

                elif line.startswith("DA:"):
                    # Line coverage: DA:line_number,execution_count
                    if current_file:
                        parts = line[3:].split(",")
                        if len(parts) >= 2:
                            line_num = int(parts[0])
                            hits = int(parts[1])
                            current_file.lines.append(LineCoverage(
                                line_number=line_num,
                                hits=hits,
                            ))

                elif line.startswith("BRDA:"):
                    # Branch coverage: BRDA:line,block,branch,taken
                    if current_file:
                        parts = line[5:].split(",")
                        if len(parts) >= 4:
                            line_num = int(parts[0])
                            taken = parts[3]
                            hits = 0 if taken == "-" else int(taken)

                            # Find or create line entry
                            line_entry = None
                            for l in current_file.lines:
                                if l.line_number == line_num:
                                    line_entry = l
                                    break

                            if line_entry:
                                line_entry.is_branch = True
                                line_entry.branch_coverage = 100.0 if hits > 0 else 0.0
                            else:
                                current_file.lines.append(LineCoverage(
                                    line_number=line_num,
                                    hits=hits,
                                    is_branch=True,
                                    branch_coverage=100.0 if hits > 0 else 0.0,
                                ))

                elif line == "end_of_record":
                    # End of file record
                    if current_file and current_file.lines:
                        report.files.append(current_file)
                    current_file = None

        except Exception:
            pass

        return report


class CoveragePyParser(CoverageParser):
    """Parser for coverage.py JSON format."""

    format_name = "coverage.py"

    def can_parse(self, file_path: Path) -> bool:
        """Check if file is coverage.py JSON format."""
        if file_path.suffix != ".json":
            return False
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(content)
            return "meta" in data and "files" in data
        except Exception:
            return False

    def parse(self, file_path: Path) -> CoverageReport:
        """Parse coverage.py JSON file."""
        report = CoverageReport(format=self.format_name, source_file=str(file_path))

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            data = json.loads(content)

            for filename, file_data in data.get("files", {}).items():
                file_cov = FileCoverage(file_path=filename)

                # Get executed lines
                executed = set(file_data.get("executed_lines", []))
                missing = set(file_data.get("missing_lines", []))

                all_lines = executed | missing
                for line_num in sorted(all_lines):
                    file_cov.lines.append(LineCoverage(
                        line_number=line_num,
                        hits=1 if line_num in executed else 0,
                    ))

                if file_cov.lines:
                    report.files.append(file_cov)

        except (json.JSONDecodeError, KeyError):
            pass

        return report


class JacocoParser(CoverageParser):
    """Parser for JaCoCo XML format (Java)."""

    format_name = "jacoco"

    def can_parse(self, file_path: Path) -> bool:
        """Check if file is JaCoCo format."""
        if not file_path.suffix == ".xml":
            return False
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")[:1000]
            return "jacoco" in content.lower() or "<report" in content and "INSTRUCTION" in content
        except Exception:
            return False

    def parse(self, file_path: Path) -> CoverageReport:
        """Parse JaCoCo XML file."""
        report = CoverageReport(format=self.format_name, source_file=str(file_path))

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            for package in root.findall(".//package"):
                package_name = package.get("name", "").replace("/", ".")

                for sourcefile in package.findall(".//sourcefile"):
                    filename = sourcefile.get("name", "")
                    full_path = f"{package_name.replace('.', '/')}/{filename}"

                    file_cov = FileCoverage(file_path=full_path)

                    for line in sourcefile.findall(".//line"):
                        line_num = int(line.get("nr", 0))
                        mi = int(line.get("mi", 0))  # Missed instructions
                        ci = int(line.get("ci", 0))  # Covered instructions
                        mb = int(line.get("mb", 0))  # Missed branches
                        cb = int(line.get("cb", 0))  # Covered branches

                        hits = 1 if ci > 0 else 0
                        is_branch = (mb + cb) > 0
                        branch_coverage = None
                        if is_branch:
                            total_branches = mb + cb
                            branch_coverage = (cb / total_branches * 100) if total_branches > 0 else 0

                        file_cov.lines.append(LineCoverage(
                            line_number=line_num,
                            hits=hits,
                            is_branch=is_branch,
                            branch_coverage=branch_coverage,
                        ))

                    if file_cov.lines:
                        report.files.append(file_cov)

        except ET.ParseError:
            pass

        return report


class CloverParser(CoverageParser):
    """Parser for Clover XML format."""

    format_name = "clover"

    def can_parse(self, file_path: Path) -> bool:
        """Check if file is Clover format."""
        if not file_path.suffix == ".xml":
            return False
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")[:1000]
            return "<coverage" in content and "clover" in content.lower()
        except Exception:
            return False

    def parse(self, file_path: Path) -> CoverageReport:
        """Parse Clover XML file."""
        report = CoverageReport(format=self.format_name, source_file=str(file_path))

        try:
            tree = ET.parse(file_path)
            root = tree.getroot()

            for file_elem in root.findall(".//file"):
                filename = file_elem.get("name", "") or file_elem.get("path", "")
                if not filename:
                    continue

                file_cov = FileCoverage(file_path=filename)

                for line in file_elem.findall(".//line"):
                    line_num = int(line.get("num", 0))
                    count = int(line.get("count", 0))
                    line_type = line.get("type", "stmt")

                    is_branch = line_type in ("cond", "branch")
                    branch_coverage = None
                    if is_branch:
                        true_count = int(line.get("truecount", 0))
                        false_count = int(line.get("falsecount", 0))
                        total = true_count + false_count
                        if total > 0:
                            covered = (1 if true_count > 0 else 0) + (1 if false_count > 0 else 0)
                            branch_coverage = covered / 2 * 100

                    file_cov.lines.append(LineCoverage(
                        line_number=line_num,
                        hits=count,
                        is_branch=is_branch,
                        branch_coverage=branch_coverage,
                    ))

                if file_cov.lines:
                    report.files.append(file_cov)

        except ET.ParseError:
            pass

        return report


# Registry of all parsers
ALL_PARSERS = [
    CoberturaParser(),
    LcovParser(),
    CoveragePyParser(),
    JacocoParser(),
    CloverParser(),
]
