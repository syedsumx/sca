"""YAML-based custom rules DSL engine."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CustomRule:
    """A user-defined detection rule loaded from YAML."""

    id: str
    name: str
    description: str = ""
    pattern: str = ""  # regex pattern
    message: str = ""
    severity: str = "MAJOR"  # BLOCKER, CRITICAL, MAJOR, MINOR, INFO
    type: str = "CODE_SMELL"  # BUG, VULNERABILITY, CODE_SMELL
    languages: list[str] = field(default_factory=list)  # empty = all
    file_pattern: str = ""  # glob for matching files e.g. "*.py"
    cwe: list[int] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    enabled: bool = True
    multiline: bool = False
    negate: bool = False  # True = flag when pattern does NOT match

    def compile(self) -> Optional[re.Pattern]:
        if not self.pattern:
            return None
        flags = re.MULTILINE
        if self.multiline:
            flags |= re.DOTALL
        try:
            return re.compile(self.pattern, flags)
        except re.error as exc:
            logger.error("Invalid regex in rule %s: %s", self.id, exc)
            return None


@dataclass
class CustomRuleMatch:
    """A match from a custom rule."""

    rule: CustomRule
    file: str
    line: int
    column: int = 0
    matched_text: str = ""


class CustomRulesEngine:
    """Load and execute custom rules from YAML files."""

    def __init__(self) -> None:
        self.rules: list[CustomRule] = []

    def load_yaml(self, path: str | Path) -> int:
        """Load rules from a YAML file. Returns count of rules loaded."""
        path = Path(path)
        if not path.exists():
            logger.warning("Rules file not found: %s", path)
            return 0

        # Use a simple YAML-subset parser to avoid PyYAML dependency
        data = self._parse_yaml(path.read_text())
        rules_data = data if isinstance(data, list) else data.get("rules", [])

        count = 0
        for entry in rules_data:
            if not isinstance(entry, dict):
                continue
            rule = CustomRule(
                id=str(entry.get("id", f"custom_{count}")),
                name=str(entry.get("name", "")),
                description=str(entry.get("description", "")),
                pattern=str(entry.get("pattern", "")),
                message=str(entry.get("message", "")),
                severity=str(entry.get("severity", "MAJOR")).upper(),
                type=str(entry.get("type", "CODE_SMELL")).upper(),
                languages=entry.get("languages", []),
                file_pattern=str(entry.get("file_pattern", "")),
                cwe=entry.get("cwe", []),
                tags=entry.get("tags", []),
                enabled=entry.get("enabled", True),
                multiline=entry.get("multiline", False),
                negate=entry.get("negate", False),
            )
            self.rules.append(rule)
            count += 1
        return count

    def load_from_dict(self, rules_list: list[dict]) -> int:
        """Load rules from a list of dicts (e.g. from API)."""
        count = 0
        for entry in rules_list:
            rule = CustomRule(
                id=str(entry.get("id", f"custom_{len(self.rules)}")),
                name=str(entry.get("name", "")),
                description=str(entry.get("description", "")),
                pattern=str(entry.get("pattern", "")),
                message=str(entry.get("message", "")),
                severity=str(entry.get("severity", "MAJOR")).upper(),
                type=str(entry.get("type", "CODE_SMELL")).upper(),
                languages=entry.get("languages", []),
                file_pattern=str(entry.get("file_pattern", "")),
                cwe=entry.get("cwe", []),
                tags=entry.get("tags", []),
                enabled=entry.get("enabled", True),
                multiline=entry.get("multiline", False),
                negate=entry.get("negate", False),
            )
            self.rules.append(rule)
            count += 1
        return count

    def scan_file(self, file_path: str, content: str) -> list[CustomRuleMatch]:
        """Run all custom rules against a single file."""
        matches: list[CustomRuleMatch] = []
        path = Path(file_path)
        ext = path.suffix.lstrip(".")

        for rule in self.rules:
            if not rule.enabled:
                continue
            if rule.languages and ext not in rule.languages:
                lang_exts = {"python": "py", "javascript": "js", "typescript": "ts",
                             "java": "java", "go": "go", "rust": "rs", "ruby": "rb"}
                matched_lang = any(
                    lang_exts.get(lang, lang) == ext for lang in rule.languages
                )
                if not matched_lang:
                    continue
            if rule.file_pattern:
                if not path.match(rule.file_pattern):
                    continue

            regex = rule.compile()
            if regex is None and not rule.negate:
                continue

            if rule.negate:
                # Flag if pattern does NOT appear
                if regex and not regex.search(content):
                    matches.append(CustomRuleMatch(
                        rule=rule, file=file_path, line=1,
                        matched_text="(pattern absent)",
                    ))
            else:
                for m in regex.finditer(content):  # type: ignore[union-attr]
                    line = content[:m.start()].count("\n") + 1
                    matches.append(CustomRuleMatch(
                        rule=rule, file=file_path, line=line,
                        column=m.start() - content.rfind("\n", 0, m.start()),
                        matched_text=m.group()[:120],
                    ))
        return matches

    def scan_directory(self, dir_path: str | Path, extensions: Optional[set[str]] = None) -> list[CustomRuleMatch]:
        """Scan all files in a directory tree."""
        dir_path = Path(dir_path)
        if extensions is None:
            extensions = {"py", "js", "ts", "tsx", "jsx", "java", "go", "rs", "rb", "php", "c", "cpp", "cs"}
        all_matches: list[CustomRuleMatch] = []
        for fpath in dir_path.rglob("*"):
            if not fpath.is_file():
                continue
            if fpath.suffix.lstrip(".") not in extensions:
                continue
            if any(part.startswith(".") or part == "node_modules" for part in fpath.parts):
                continue
            try:
                content = fpath.read_text(errors="replace")
            except OSError:
                continue
            all_matches.extend(self.scan_file(str(fpath), content))
        return all_matches

    @staticmethod
    def _parse_yaml(text: str) -> dict | list:
        """Minimal YAML parser for simple rule files (no PyYAML needed).

        Supports: key: value, lists with -, nested rules list, quoted strings.
        Falls back to yaml module if available.
        """
        try:
            import yaml
            return yaml.safe_load(text)
        except ImportError:
            pass

        # Minimal fallback parser for flat rule definitions
        import ast
        lines = text.split("\n")
        result: dict = {"rules": []}
        current: Optional[dict] = None
        current_key: Optional[str] = None

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            indent = len(line) - len(line.lstrip())

            if stripped.startswith("- ") and indent <= 2:
                # New rule item
                if current is not None:
                    result["rules"].append(current)
                if ":" in stripped[2:]:
                    k, v = stripped[2:].split(":", 1)
                    current = {k.strip(): _yaml_val(v.strip())}
                else:
                    current = {}
                current_key = None
                continue

            if current is not None and ":" in stripped:
                k, v = stripped.split(":", 1)
                k, v = k.strip(), v.strip()
                if v.startswith("[") and v.endswith("]"):
                    # inline list
                    current[k] = [_yaml_val(x.strip().strip("'\"")) for x in v[1:-1].split(",") if x.strip()]
                elif v == "":
                    current_key = k
                    current[k] = []
                else:
                    current[k] = _yaml_val(v)
            elif current is not None and stripped.startswith("- ") and current_key:
                current[current_key].append(_yaml_val(stripped[2:].strip()))

        if current is not None:
            result["rules"].append(current)
        return result


def _yaml_val(s: str):
    """Convert a YAML scalar string to Python value."""
    s = s.strip().strip("'\"")
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s
