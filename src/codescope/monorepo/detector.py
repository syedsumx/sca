"""Monorepo detection engine for CodeScope.

Automatically detects workspace type and enumerates sub-projects for a
variety of monorepo toolchains (Nx, Turborepo, Lerna, pnpm, Yarn, Cargo,
Go workspaces, Maven multi-module, Gradle multi-project, and Python
monorepos).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Enums & data classes
# ---------------------------------------------------------------------------

class WorkspaceType(str, Enum):
    """Supported monorepo / workspace types."""

    NX = "nx"
    TURBOREPO = "turborepo"
    LERNA = "lerna"
    PNPM_WORKSPACE = "pnpm_workspace"
    YARN_WORKSPACE = "yarn_workspace"
    CARGO_WORKSPACE = "cargo_workspace"
    GO_WORKSPACE = "go_workspace"
    MAVEN_MULTI = "maven_multi"
    GRADLE_MULTI = "gradle_multi"
    PYTHON_MONOREPO = "python_monorepo"
    CUSTOM = "custom"
    SINGLE_PROJECT = "single_project"


@dataclass
class SubProject:
    """A single sub-project inside a monorepo."""

    name: str
    path: Path  # relative to the workspace root
    language: str  # primary language
    has_config: bool = False  # has its own codescope.yml
    dependencies: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceInfo:
    """Describes a detected workspace layout."""

    root_path: Path
    workspace_type: WorkspaceType
    projects: list[SubProject] = field(default_factory=list)
    shared_configs: list[Path] = field(default_factory=list)
    dependency_graph: dict[str, list[str]] = field(default_factory=dict)

    # -- properties ----------------------------------------------------------

    @property
    def total_projects(self) -> int:
        """Total number of detected sub-projects."""
        return len(self.projects)

    # -- serialisation -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise the workspace info to a plain dictionary."""
        return {
            "root_path": str(self.root_path),
            "workspace_type": self.workspace_type.value,
            "total_projects": self.total_projects,
            "projects": [
                {
                    "name": p.name,
                    "path": str(p.path),
                    "language": p.language,
                    "has_config": p.has_config,
                    "dependencies": p.dependencies,
                    "metadata": p.metadata,
                }
                for p in self.projects
            ],
            "shared_configs": [str(c) for c in self.shared_configs],
            "dependency_graph": self.dependency_graph,
        }


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class MonorepoDetector:
    """Detect monorepo workspace type and enumerate sub-projects."""

    # Ordered so more-specific toolchains are checked first.
    _DETECTORS: list[str] = [
        "_detect_nx",
        "_detect_turborepo",
        "_detect_lerna",
        "_detect_pnpm",
        "_detect_yarn",
        "_detect_cargo",
        "_detect_go",
        "_detect_maven",
        "_detect_gradle",
        "_detect_python",
    ]

    # -- public API ----------------------------------------------------------

    def detect(self, root_path: Path) -> WorkspaceInfo:
        """Auto-detect the workspace type at *root_path*.

        Returns a :class:`WorkspaceInfo` describing the layout.  If no
        known workspace toolchain is found the workspace type is set to
        ``SINGLE_PROJECT``.
        """
        root = root_path.resolve()
        for method_name in self._DETECTORS:
            result = getattr(self, method_name)(root)
            if result is not None:
                # Build the dependency graph from individual projects.
                result.dependency_graph = {
                    p.name: list(p.dependencies) for p in result.projects
                }
                return result

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.SINGLE_PROJECT,
        )

    # -- private detectors ---------------------------------------------------

    def _detect_nx(self, root: Path) -> WorkspaceInfo | None:
        """Detect an Nx workspace (``nx.json``)."""
        nx_json = root / "nx.json"
        if not nx_json.is_file():
            return None

        projects: list[SubProject] = []

        # Nx >=13 may use individual project.json files.
        workspace_json = root / "workspace.json"
        if workspace_json.is_file():
            data = _read_json(workspace_json)
            for name, cfg in (data.get("projects") or {}).items():
                proj_root = cfg if isinstance(cfg, str) else cfg.get("root", name)
                projects.append(self._make_sub_project(root, name, Path(proj_root)))
        else:
            # Scan for project.json files in sub-directories.
            for pj in root.rglob("project.json"):
                if pj == root / "project.json":
                    continue
                rel = pj.parent.relative_to(root)
                data = _read_json(pj)
                name = data.get("name", rel.name)
                projects.append(self._make_sub_project(root, name, rel))

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.NX,
            projects=projects,
            shared_configs=self._find_shared_configs(root),
        )

    def _detect_turborepo(self, root: Path) -> WorkspaceInfo | None:
        """Detect a Turborepo workspace (``turbo.json``)."""
        turbo_json = root / "turbo.json"
        if not turbo_json.is_file():
            return None

        pkg_json = root / "package.json"
        workspaces = self._read_npm_workspaces(pkg_json)
        projects = self._resolve_js_workspaces(root, workspaces)

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.TURBOREPO,
            projects=projects,
            shared_configs=self._find_shared_configs(root),
        )

    def _detect_lerna(self, root: Path) -> WorkspaceInfo | None:
        """Detect a Lerna monorepo (``lerna.json``)."""
        lerna_json = root / "lerna.json"
        if not lerna_json.is_file():
            return None

        data = _read_json(lerna_json)
        patterns = data.get("packages", ["packages/*"])
        projects = self._resolve_js_workspaces(root, patterns)

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.LERNA,
            projects=projects,
            shared_configs=self._find_shared_configs(root),
        )

    def _detect_pnpm(self, root: Path) -> WorkspaceInfo | None:
        """Detect a pnpm workspace (``pnpm-workspace.yaml``)."""
        pnpm_ws = root / "pnpm-workspace.yaml"
        if not pnpm_ws.is_file():
            return None

        with open(pnpm_ws) as fh:
            data = yaml.safe_load(fh) or {}

        patterns = data.get("packages", [])
        projects = self._resolve_js_workspaces(root, patterns)

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.PNPM_WORKSPACE,
            projects=projects,
            shared_configs=self._find_shared_configs(root),
        )

    def _detect_yarn(self, root: Path) -> WorkspaceInfo | None:
        """Detect Yarn workspaces via ``package.json``."""
        pkg_json = root / "package.json"
        workspaces = self._read_npm_workspaces(pkg_json)
        if not workspaces:
            return None

        # Only treat as Yarn workspace when no higher-level tool was found.
        projects = self._resolve_js_workspaces(root, workspaces)

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.YARN_WORKSPACE,
            projects=projects,
            shared_configs=self._find_shared_configs(root),
        )

    def _detect_cargo(self, root: Path) -> WorkspaceInfo | None:
        """Detect a Cargo workspace (``Cargo.toml`` with ``[workspace]``)."""
        cargo_toml = root / "Cargo.toml"
        if not cargo_toml.is_file():
            return None

        text = cargo_toml.read_text(encoding="utf-8")
        if "[workspace]" not in text:
            return None

        # Simple TOML parsing for the members array.
        members: list[str] = []
        in_members = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("members"):
                in_members = True
                # Inline array on same line?
                match = re.search(r"\[([^\]]*)\]", stripped)
                if match:
                    members.extend(_parse_toml_string_array(match.group(0)))
                    in_members = False
                continue
            if in_members:
                if stripped.startswith("]"):
                    in_members = False
                    continue
                members.extend(_parse_toml_string_array(f"[{stripped}]"))

        projects: list[SubProject] = []
        for pattern in members:
            for member_dir in sorted(root.glob(pattern)):
                if member_dir.is_dir() and (member_dir / "Cargo.toml").is_file():
                    rel = member_dir.relative_to(root)
                    projects.append(
                        self._make_sub_project(root, rel.name, rel, language="rust")
                    )

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.CARGO_WORKSPACE,
            projects=projects,
            shared_configs=self._find_shared_configs(root),
        )

    def _detect_go(self, root: Path) -> WorkspaceInfo | None:
        """Detect a Go workspace (``go.work``)."""
        go_work = root / "go.work"
        if not go_work.is_file():
            return None

        projects: list[SubProject] = []
        text = go_work.read_text(encoding="utf-8")
        in_use = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("use ("):
                in_use = True
                continue
            if stripped == "use" and not stripped.endswith(")"):
                in_use = True
                continue
            if in_use:
                if stripped == ")":
                    in_use = False
                    continue
                mod_path = stripped.strip().strip('"')
                if mod_path:
                    for mod_dir in sorted(root.glob(mod_path)):
                        if mod_dir.is_dir():
                            rel = mod_dir.relative_to(root)
                            projects.append(
                                self._make_sub_project(root, rel.name, rel, language="go")
                            )
            elif stripped.startswith("use "):
                mod_path = stripped[4:].strip().strip('"')
                if mod_path:
                    mod_dir = root / mod_path
                    if mod_dir.is_dir():
                        rel = mod_dir.relative_to(root)
                        projects.append(
                            self._make_sub_project(root, rel.name, rel, language="go")
                        )

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.GO_WORKSPACE,
            projects=projects,
            shared_configs=self._find_shared_configs(root),
        )

    def _detect_maven(self, root: Path) -> WorkspaceInfo | None:
        """Detect a Maven multi-module project (``pom.xml`` with ``<modules>``)."""
        pom_xml = root / "pom.xml"
        if not pom_xml.is_file():
            return None

        text = pom_xml.read_text(encoding="utf-8")
        if "<modules>" not in text:
            return None

        # Simple regex extraction of <module> entries.
        modules = re.findall(r"<module>\s*(.+?)\s*</module>", text)
        projects: list[SubProject] = []
        for mod in modules:
            mod_dir = root / mod
            if mod_dir.is_dir():
                rel = mod_dir.relative_to(root)
                projects.append(
                    self._make_sub_project(root, rel.name, rel, language="java")
                )

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.MAVEN_MULTI,
            projects=projects,
            shared_configs=self._find_shared_configs(root),
        )

    def _detect_gradle(self, root: Path) -> WorkspaceInfo | None:
        """Detect a Gradle multi-project build (``settings.gradle(.kts)``)."""
        settings_file: Path | None = None
        for name in ("settings.gradle.kts", "settings.gradle"):
            candidate = root / name
            if candidate.is_file():
                settings_file = candidate
                break

        if settings_file is None:
            return None

        text = settings_file.read_text(encoding="utf-8")
        # Match include(":foo", ":bar") or include ':foo', ':bar'
        includes = re.findall(r"""include\s*\(?\s*(['"])(.*?)\1""", text)
        module_names: list[str] = []
        for _, name_str in includes:
            for part in name_str.split(","):
                clean = part.strip().strip("'\"").lstrip(":")
                if clean:
                    module_names.append(clean)

        # Also handle the groovy-style: include ':moduleA', ':moduleB'
        for match in re.finditer(r"include\s+((?:['\"]:[^'\"]+['\"][,\s]*)+)", text):
            for part in re.findall(r"['\"]:(.*?)['\"]", match.group(1)):
                if part and part not in module_names:
                    module_names.append(part)

        projects: list[SubProject] = []
        for mod_name in module_names:
            # Gradle uses ':' as path separator.
            mod_dir = root / mod_name.replace(":", "/")
            if mod_dir.is_dir():
                rel = mod_dir.relative_to(root)
                projects.append(
                    self._make_sub_project(root, mod_name, rel, language="java")
                )

        if not projects:
            return None

        return WorkspaceInfo(
            root_path=root,
            workspace_type=WorkspaceType.GRADLE_MULTI,
            projects=projects,
            shared_configs=self._find_shared_configs(root),
        )

    def _detect_python(self, root: Path) -> WorkspaceInfo | None:
        """Detect a Python monorepo.

        Recognised layouts:
        * ``pyproject.toml`` with ``[tool.hatch.envs]``
        * Multiple ``setup.py`` or ``pyproject.toml`` files in immediate
          sub-directories.
        """
        root_pyproject = root / "pyproject.toml"
        if root_pyproject.is_file():
            text = root_pyproject.read_text(encoding="utf-8")
            if "[tool.hatch.envs" in text:
                # Hatch-based monorepo — enumerate sub-dirs that have their
                # own pyproject.toml / setup.py.
                projects = self._find_python_sub_projects(root)
                if projects:
                    return WorkspaceInfo(
                        root_path=root,
                        workspace_type=WorkspaceType.PYTHON_MONOREPO,
                        projects=projects,
                        shared_configs=self._find_shared_configs(root),
                    )

        # Fallback: multiple Python projects as direct children.
        projects = self._find_python_sub_projects(root)
        if len(projects) >= 2:
            return WorkspaceInfo(
                root_path=root,
                workspace_type=WorkspaceType.PYTHON_MONOREPO,
                projects=projects,
                shared_configs=self._find_shared_configs(root),
            )

        return None

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _make_sub_project(
        root: Path,
        name: str,
        rel_path: Path,
        language: str | None = None,
    ) -> SubProject:
        """Create a :class:`SubProject`, auto-detecting language if needed."""
        abs_path = root / rel_path

        if language is None:
            language = _guess_language(abs_path)

        has_config = any(
            (abs_path / cfg).is_file()
            for cfg in ("codescope.yml", "codescope.yaml", ".codescope.yml", ".codescope.yaml")
        )

        # Attempt to read JS/TS dependencies from package.json.
        deps: list[str] = []
        pkg_json = abs_path / "package.json"
        if pkg_json.is_file():
            data = _read_json(pkg_json)
            all_deps: dict[str, str] = {}
            all_deps.update(data.get("dependencies") or {})
            all_deps.update(data.get("devDependencies") or {})
            # Only keep workspace-local dependencies (those starting with @
            # scope or matching sibling names — we record all and let the
            # caller filter later).
            deps = list(all_deps.keys())

        return SubProject(
            name=name,
            path=rel_path,
            language=language,
            has_config=has_config,
            dependencies=deps,
            metadata={},
        )

    @staticmethod
    def _read_npm_workspaces(pkg_json: Path) -> list[str]:
        """Read the ``workspaces`` field from a *package.json*."""
        if not pkg_json.is_file():
            return []
        data = _read_json(pkg_json)
        workspaces = data.get("workspaces", [])
        if isinstance(workspaces, dict):
            # Yarn Berry format: { packages: [...] }
            workspaces = workspaces.get("packages", [])
        return workspaces if isinstance(workspaces, list) else []

    def _resolve_js_workspaces(
        self,
        root: Path,
        patterns: list[str],
    ) -> list[SubProject]:
        """Resolve JS/TS workspace glob patterns to sub-projects."""
        projects: list[SubProject] = []
        seen: set[Path] = set()
        for pattern in patterns:
            # Strip leading "!" (exclusion) — we skip those.
            if pattern.startswith("!"):
                continue
            for match in sorted(root.glob(pattern)):
                if match.is_dir() and match not in seen:
                    seen.add(match)
                    rel = match.relative_to(root)
                    pkg_json = match / "package.json"
                    name = rel.name
                    if pkg_json.is_file():
                        data = _read_json(pkg_json)
                        name = data.get("name", name)
                    projects.append(self._make_sub_project(root, name, rel))
        return projects

    @staticmethod
    def _find_shared_configs(root: Path) -> list[Path]:
        """Return shared configuration files at the workspace root."""
        candidates = [
            "codescope.yml",
            "codescope.yaml",
            ".codescope.yml",
            ".codescope.yaml",
            ".eslintrc.json",
            ".eslintrc.js",
            "tsconfig.json",
            "tsconfig.base.json",
            ".prettierrc",
            ".prettierrc.json",
        ]
        return [
            root / c for c in candidates if (root / c).is_file()
        ]

    def _find_python_sub_projects(self, root: Path) -> list[SubProject]:
        """Find Python sub-projects as direct children of *root*."""
        projects: list[SubProject] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / "pyproject.toml").is_file() or (child / "setup.py").is_file():
                rel = child.relative_to(root)
                projects.append(
                    self._make_sub_project(root, rel.name, rel, language="python")
                )
        return projects


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any]:
    """Read and return a JSON file as a dict, returning ``{}`` on error."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return {}


def _parse_toml_string_array(text: str) -> list[str]:
    """Parse a TOML-style string array fragment, e.g. ``["foo/*", "bar"]``."""
    return [m.strip() for m in re.findall(r'"([^"]*)"', text)]


def _guess_language(project_dir: Path) -> str:
    """Guess the primary language for a project directory."""
    indicators: dict[str, list[str]] = {
        "typescript": ["tsconfig.json"],
        "javascript": ["package.json"],
        "python": ["pyproject.toml", "setup.py", "setup.cfg"],
        "rust": ["Cargo.toml"],
        "go": ["go.mod"],
        "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "csharp": ["*.csproj"],
    }

    for lang, markers in indicators.items():
        for marker in markers:
            if "*" in marker:
                if list(project_dir.glob(marker)):
                    return lang
            elif (project_dir / marker).is_file():
                return lang

    return "unknown"
