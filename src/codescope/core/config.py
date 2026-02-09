"""Configuration management for CodeScope."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class SourceConfig:
    """Configuration for source files to analyze."""

    include: list[str] = field(default_factory=lambda: ["**/*"])
    exclude: list[str] = field(
        default_factory=lambda: [
            "**/node_modules/**",
            "**/venv/**",
            "**/.venv/**",
            "**/__pycache__/**",
            "**/dist/**",
            "**/build/**",
            "**/.git/**",
            "**/vendor/**",
        ]
    )


@dataclass
class RulesConfig:
    """Configuration for rules."""

    disabled: list[str] = field(default_factory=list)
    severities: dict[str, str] = field(default_factory=dict)
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class QualityGateCondition:
    """A single quality gate condition."""

    metric: str
    operator: str  # GT, LT, EQ, GTE, LTE
    threshold: int | float | str
    on_new_code: bool = False


@dataclass
class QualityGateConfig:
    """Configuration for quality gate."""

    name: str = "default"
    conditions: list[QualityGateCondition] = field(default_factory=list)


@dataclass
class OutputConfig:
    """Configuration for output."""

    format: str = "console"  # console, json, sarif, html
    verbose: bool = False
    show_snippet: bool = True
    output_file: str | None = None


@dataclass
class Config:
    """Main configuration for CodeScope."""

    project_key: str = ""
    project_name: str = ""
    languages: list[str] = field(default_factory=list)  # Empty means auto-detect
    sources: SourceConfig = field(default_factory=SourceConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    quality_gate: QualityGateConfig | None = None
    output: OutputConfig = field(default_factory=OutputConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        """Create Config from dictionary."""
        config = cls()

        # Project settings
        project = data.get("project", {})
        config.project_key = project.get("key", "")
        config.project_name = project.get("name", "")

        # Analysis settings
        analysis = data.get("analysis", {})
        config.languages = analysis.get("languages", [])

        sources = analysis.get("sources", {})
        config.sources = SourceConfig(
            include=sources.get("include", config.sources.include),
            exclude=sources.get("exclude", config.sources.exclude),
        )

        # Rules settings
        rules = data.get("rules", {})
        config.rules = RulesConfig(
            disabled=rules.get("disabled", []),
            severities=rules.get("severities", {}),
            parameters=rules.get("parameters", {}),
        )

        # Quality gate settings
        qg = data.get("quality_gate", {})
        if qg:
            conditions = []
            for cond in qg.get("conditions", []):
                conditions.append(
                    QualityGateCondition(
                        metric=cond["metric"],
                        operator=cond["operator"],
                        threshold=cond["threshold"],
                        on_new_code=cond.get("on_new_code", False),
                    )
                )
            config.quality_gate = QualityGateConfig(
                name=qg.get("name", "default"),
                conditions=conditions,
            )

        # Output settings
        output = data.get("output", {})
        config.output = OutputConfig(
            format=output.get("format", "console"),
            verbose=output.get("verbose", False),
            show_snippet=output.get("show_snippet", True),
            output_file=output.get("output_file"),
        )

        return config

    def to_dict(self) -> dict[str, Any]:
        """Convert Config to dictionary."""
        result: dict[str, Any] = {
            "project": {
                "key": self.project_key,
                "name": self.project_name,
            },
            "analysis": {
                "languages": self.languages,
                "sources": {
                    "include": self.sources.include,
                    "exclude": self.sources.exclude,
                },
            },
            "rules": {
                "disabled": self.rules.disabled,
                "severities": self.rules.severities,
                "parameters": self.rules.parameters,
            },
            "output": {
                "format": self.output.format,
                "verbose": self.output.verbose,
                "show_snippet": self.output.show_snippet,
            },
        }

        if self.quality_gate:
            result["quality_gate"] = {
                "name": self.quality_gate.name,
                "conditions": [
                    {
                        "metric": c.metric,
                        "operator": c.operator,
                        "threshold": c.threshold,
                        "on_new_code": c.on_new_code,
                    }
                    for c in self.quality_gate.conditions
                ],
            }

        return result


@dataclass
class AnalysisConfig:
    """Lightweight config for running an analysis from the API."""

    project_path: Path = field(default_factory=lambda: Path("."))
    exclude_patterns: list[str] = field(default_factory=list)

    def to_config(self) -> "Config":
        """Convert to full Config."""
        config = Config()
        if self.exclude_patterns:
            config.sources.exclude.extend(self.exclude_patterns)
        return config


def load_config(path: Path | None = None) -> Config:
    """Load configuration from file.

    Args:
        path: Path to config file. If None, searches for codescope.yml/yaml
              in current directory.

    Returns:
        Config object with loaded settings.
    """
    if path is None:
        # Search for config file
        for name in ["codescope.yml", "codescope.yaml", ".codescope.yml", ".codescope.yaml"]:
            candidate = Path.cwd() / name
            if candidate.exists():
                path = candidate
                break

    if path is None or not path.exists():
        # Return default config
        return Config()

    resolved = path.resolve()
    with open(resolved) as f:
        data = yaml.safe_load(f) or {}

    return Config.from_dict(data)


def generate_default_config() -> str:
    """Generate default configuration file content."""
    return '''# CodeScope Configuration

project:
  key: "my-project"
  name: "My Project"

analysis:
  # Languages to analyze (auto-detect if not specified)
  languages: []

  # Paths to include/exclude
  sources:
    include:
      - "**/*"
    exclude:
      - "**/node_modules/**"
      - "**/venv/**"
      - "**/__pycache__/**"
      - "**/dist/**"
      - "**/build/**"
      - "**/.git/**"

# Rules configuration
rules:
  # Disable specific rules
  disabled: []

  # Override rule severities
  severities: {}

  # Rule-specific parameters
  parameters:
    "python:S138":  # Function length
      max_lines: 100
    "python:S3776":  # Cognitive complexity
      threshold: 15

# Quality gate
quality_gate:
  name: "default"
  conditions:
    - metric: "bugs"
      operator: "GT"
      threshold: 0
    - metric: "vulnerabilities"
      operator: "GT"
      threshold: 0
    - metric: "code_smells"
      operator: "GT"
      threshold: 10

# Output settings
output:
  format: "console"  # console, json, sarif, html
  verbose: false
  show_snippet: true
'''
