"""Dependency file parsers for various package managers."""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
import xml.etree.ElementTree as ET


@dataclass
class ParsedDependency:
    """A dependency parsed from a manifest file."""

    name: str
    version: str | None
    version_constraint: str | None = None
    ecosystem: str = "unknown"
    is_dev: bool = False
    source_file: str = ""
    line_number: int = 0


class DependencyParser(ABC):
    """Base class for dependency file parsers."""

    @property
    @abstractmethod
    def ecosystem(self) -> str:
        """Return the package ecosystem (pypi, npm, etc.)."""
        pass

    @property
    @abstractmethod
    def file_patterns(self) -> list[str]:
        """Return glob patterns for dependency files."""
        pass

    @abstractmethod
    def parse(self, file_path: Path) -> list[ParsedDependency]:
        """Parse a dependency file and return dependencies."""
        pass

    def can_parse(self, file_path: Path) -> bool:
        """Check if this parser can handle the given file."""
        name = file_path.name.lower()
        for pattern in self.file_patterns:
            if pattern.startswith("*"):
                if name.endswith(pattern[1:]):
                    return True
            elif name == pattern.lower():
                return True
        return False


class RequirementsTxtParser(DependencyParser):
    """Parser for Python requirements.txt files."""

    @property
    def ecosystem(self) -> str:
        return "pypi"

    @property
    def file_patterns(self) -> list[str]:
        return ["requirements.txt", "requirements-*.txt", "requirements/*.txt", "*.requirements.txt"]

    def parse(self, file_path: Path) -> list[ParsedDependency]:
        """Parse requirements.txt file."""
        deps = []
        content = file_path.read_text(encoding="utf-8", errors="replace")

        for line_num, line in enumerate(content.split("\n"), 1):
            line = line.strip()

            # Skip comments and empty lines
            if not line or line.startswith("#") or line.startswith("-"):
                continue

            # Skip editable installs and URLs
            if line.startswith("-e") or "://" in line:
                continue

            # Parse package spec
            match = re.match(r'^([a-zA-Z0-9_-]+)\s*([<>=!~]+.+)?', line)
            if match:
                name = match.group(1)
                version_spec = match.group(2)

                # Extract exact version if pinned
                version = None
                if version_spec:
                    exact_match = re.match(r'==\s*([^\s,;]+)', version_spec)
                    if exact_match:
                        version = exact_match.group(1)

                deps.append(ParsedDependency(
                    name=name.lower(),
                    version=version,
                    version_constraint=version_spec,
                    ecosystem=self.ecosystem,
                    source_file=str(file_path),
                    line_number=line_num,
                ))

        return deps


class PackageJsonParser(DependencyParser):
    """Parser for Node.js package.json files."""

    @property
    def ecosystem(self) -> str:
        return "npm"

    @property
    def file_patterns(self) -> list[str]:
        return ["package.json"]

    def parse(self, file_path: Path) -> list[ParsedDependency]:
        """Parse package.json file."""
        deps = []
        content = file_path.read_text(encoding="utf-8", errors="replace")

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return deps

        # Parse dependencies
        for dep_type, is_dev in [("dependencies", False), ("devDependencies", True)]:
            if dep_type in data:
                for name, version_spec in data[dep_type].items():
                    version = self._extract_version(version_spec)
                    deps.append(ParsedDependency(
                        name=name,
                        version=version,
                        version_constraint=version_spec,
                        ecosystem=self.ecosystem,
                        is_dev=is_dev,
                        source_file=str(file_path),
                    ))

        return deps

    def _extract_version(self, spec: str) -> str | None:
        """Extract exact version from npm version spec."""
        # Remove ^ or ~ prefix
        spec = spec.strip()
        if spec.startswith(("^", "~")):
            return spec[1:]
        if spec.startswith("="):
            return spec[1:]
        # Check if it's a semver
        if re.match(r'^\d+\.\d+\.\d+', spec):
            return spec.split()[0]
        return None


class GoModParser(DependencyParser):
    """Parser for Go go.mod files."""

    @property
    def ecosystem(self) -> str:
        return "go"

    @property
    def file_patterns(self) -> list[str]:
        return ["go.mod"]

    def parse(self, file_path: Path) -> list[ParsedDependency]:
        """Parse go.mod file."""
        deps = []
        content = file_path.read_text(encoding="utf-8", errors="replace")

        in_require = False
        for line_num, line in enumerate(content.split("\n"), 1):
            line = line.strip()

            if line.startswith("require ("):
                in_require = True
                continue
            if line == ")":
                in_require = False
                continue

            # Parse single require or block require
            if line.startswith("require ") or in_require:
                if line.startswith("require "):
                    line = line[8:]

                # Skip comments
                if "//" in line:
                    line = line[:line.index("//")].strip()

                parts = line.split()
                if len(parts) >= 2:
                    name = parts[0]
                    version = parts[1].lstrip("v")

                    deps.append(ParsedDependency(
                        name=name,
                        version=version,
                        version_constraint=parts[1],
                        ecosystem=self.ecosystem,
                        source_file=str(file_path),
                        line_number=line_num,
                    ))

        return deps


class PomXmlParser(DependencyParser):
    """Parser for Maven pom.xml files."""

    @property
    def ecosystem(self) -> str:
        return "maven"

    @property
    def file_patterns(self) -> list[str]:
        return ["pom.xml"]

    def parse(self, file_path: Path) -> list[ParsedDependency]:
        """Parse pom.xml file."""
        deps = []
        content = file_path.read_text(encoding="utf-8", errors="replace")

        try:
            # Remove namespace for easier parsing
            content = re.sub(r'xmlns=["\'][^"\']+["\']', '', content)
            root = ET.fromstring(content)
        except ET.ParseError:
            return deps

        # Find all dependencies
        for dep in root.iter("dependency"):
            group_id = dep.find("groupId")
            artifact_id = dep.find("artifactId")
            version = dep.find("version")
            scope = dep.find("scope")

            if group_id is not None and artifact_id is not None:
                name = f"{group_id.text}:{artifact_id.text}"
                ver = version.text if version is not None else None
                is_dev = scope is not None and scope.text in ("test", "provided")

                deps.append(ParsedDependency(
                    name=name,
                    version=ver,
                    ecosystem=self.ecosystem,
                    is_dev=is_dev,
                    source_file=str(file_path),
                ))

        return deps


class GemfileParser(DependencyParser):
    """Parser for Ruby Gemfile files."""

    @property
    def ecosystem(self) -> str:
        return "rubygems"

    @property
    def file_patterns(self) -> list[str]:
        return ["Gemfile", "*.gemspec"]

    def parse(self, file_path: Path) -> list[ParsedDependency]:
        """Parse Gemfile."""
        deps = []
        content = file_path.read_text(encoding="utf-8", errors="replace")

        # Match gem 'name', 'version' or gem 'name'
        pattern = r'gem\s+[\'"]([^"\']+)[\'"]\s*(?:,\s*[\'"]([^"\']+)[\'"])?'

        for line_num, line in enumerate(content.split("\n"), 1):
            line = line.strip()
            if line.startswith("#"):
                continue

            for match in re.finditer(pattern, line):
                name = match.group(1)
                version_spec = match.group(2)

                version = None
                if version_spec:
                    # Extract version number
                    ver_match = re.search(r'[\d.]+', version_spec)
                    if ver_match:
                        version = ver_match.group(0)

                deps.append(ParsedDependency(
                    name=name,
                    version=version,
                    version_constraint=version_spec,
                    ecosystem=self.ecosystem,
                    source_file=str(file_path),
                    line_number=line_num,
                ))

        return deps


class ComposerJsonParser(DependencyParser):
    """Parser for PHP composer.json files."""

    @property
    def ecosystem(self) -> str:
        return "packagist"

    @property
    def file_patterns(self) -> list[str]:
        return ["composer.json"]

    def parse(self, file_path: Path) -> list[ParsedDependency]:
        """Parse composer.json file."""
        deps = []
        content = file_path.read_text(encoding="utf-8", errors="replace")

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return deps

        for dep_type, is_dev in [("require", False), ("require-dev", True)]:
            if dep_type in data:
                for name, version_spec in data[dep_type].items():
                    # Skip PHP version constraint
                    if name == "php":
                        continue

                    version = self._extract_version(version_spec)
                    deps.append(ParsedDependency(
                        name=name,
                        version=version,
                        version_constraint=version_spec,
                        ecosystem=self.ecosystem,
                        is_dev=is_dev,
                        source_file=str(file_path),
                    ))

        return deps

    def _extract_version(self, spec: str) -> str | None:
        """Extract version from composer version spec."""
        # Remove ^ or ~ prefix
        spec = spec.strip()
        if spec.startswith(("^", "~")):
            spec = spec[1:]
        # Check if it's a version
        if re.match(r'^\d+\.\d+', spec):
            return spec.split()[0]
        return None


class CargoTomlParser(DependencyParser):
    """Parser for Rust Cargo.toml files."""

    @property
    def ecosystem(self) -> str:
        return "crates.io"

    @property
    def file_patterns(self) -> list[str]:
        return ["Cargo.toml"]

    def parse(self, file_path: Path) -> list[ParsedDependency]:
        """Parse Cargo.toml file."""
        deps = []
        content = file_path.read_text(encoding="utf-8", errors="replace")

        in_deps = False
        in_dev_deps = False

        for line_num, line in enumerate(content.split("\n"), 1):
            stripped = line.strip()

            if stripped == "[dependencies]":
                in_deps = True
                in_dev_deps = False
                continue
            elif stripped == "[dev-dependencies]":
                in_deps = False
                in_dev_deps = True
                continue
            elif stripped.startswith("["):
                in_deps = False
                in_dev_deps = False
                continue

            if in_deps or in_dev_deps:
                # Parse name = "version" or name = { version = "x" }
                match = re.match(r'^([a-zA-Z0-9_-]+)\s*=\s*(.+)', stripped)
                if match:
                    name = match.group(1)
                    value = match.group(2)

                    version = None
                    if value.startswith('"'):
                        # Simple version string
                        version = value.strip('"')
                    elif value.startswith("{"):
                        # Table with version
                        ver_match = re.search(r'version\s*=\s*"([^"]+)"', value)
                        if ver_match:
                            version = ver_match.group(1)

                    deps.append(ParsedDependency(
                        name=name,
                        version=version.lstrip("^~") if version else None,
                        version_constraint=version,
                        ecosystem=self.ecosystem,
                        is_dev=in_dev_deps,
                        source_file=str(file_path),
                        line_number=line_num,
                    ))

        return deps


# Registry of all parsers
ALL_PARSERS = [
    RequirementsTxtParser(),
    PackageJsonParser(),
    GoModParser(),
    PomXmlParser(),
    GemfileParser(),
    ComposerJsonParser(),
    CargoTomlParser(),
]


def get_parser_for_file(file_path: Path) -> DependencyParser | None:
    """Get the appropriate parser for a file."""
    for parser in ALL_PARSERS:
        if parser.can_parse(file_path):
            return parser
    return None
