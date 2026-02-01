"""Auto-Fix engine: generates and applies code fix suggestions for known rules."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from codescope.core.models import Issue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class FixDifficulty(str, Enum):
    """Estimated difficulty of applying a fix."""

    TRIVIAL = "TRIVIAL"
    EASY = "EASY"
    MODERATE = "MODERATE"
    HARD = "HARD"


@dataclass
class FixSuggestion:
    """A concrete code-change suggestion for a single issue."""

    rule_id: str
    title: str
    description: str
    original_code: str
    suggested_code: str
    file_path: str
    start_line: int
    end_line: int
    difficulty: FixDifficulty
    confidence: float  # 0.0 -- 1.0
    is_safe_to_apply: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-friendly dictionary."""
        return {
            "rule_id": self.rule_id,
            "title": self.title,
            "description": self.description,
            "original_code": self.original_code,
            "suggested_code": self.suggested_code,
            "file_path": self.file_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "difficulty": self.difficulty.value,
            "confidence": self.confidence,
            "is_safe_to_apply": self.is_safe_to_apply,
        }


@dataclass
class FixResult:
    """Aggregated result of a suggest / apply operation."""

    suggestions: list[FixSuggestion] = field(default_factory=list)
    applied_count: int = 0
    skipped_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestions": [s.to_dict() for s in self.suggestions],
            "applied_count": self.applied_count,
            "skipped_count": self.skipped_count,
            "total": len(self.suggestions),
        }


# Type alias for a fixer callable.
FixerFn = Callable[[Issue, list[str]], FixSuggestion | None]

# ---------------------------------------------------------------------------
# Individual fixer functions
# ---------------------------------------------------------------------------

# ── Python Security ────────────────────────────────────────────────────────


def _fix_sql_injection(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S1001 -- replace string-formatted SQL with parameterized queries."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet

    # Pattern: cursor.execute("... %s ..." % var)  ->  cursor.execute("... %s ...", (var,))
    suggested = re.sub(
        r'(\.execute\s*\(\s*)(f"[^"]*"|f\'[^\']*\'|"[^"]*"\s*%\s*[^)]+|\'[^\']*\'\s*%\s*[^)]+)',
        _rewrite_sql_execute,
        original,
    )

    # Pattern: cursor.execute(f"SELECT ... {var}")  ->  cursor.execute("SELECT ... %s", (var,))
    if suggested == original:
        suggested = re.sub(
            r'(\.execute\s*\(\s*)f(["\'])(.+?)\2',
            _rewrite_fstring_sql,
            original,
        )

    # If nothing changed, provide a generic suggestion
    if suggested == original:
        suggested = (
            "# TODO: Replace string formatting with parameterized query\n"
            "# Example: cursor.execute(\"SELECT * FROM t WHERE id = %s\", (user_id,))\n"
            + original
        )

    return FixSuggestion(
        rule_id="S1001",
        title="Use parameterized SQL query",
        description="Replace string formatting/interpolation in SQL with parameterized queries to prevent SQL injection.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.EASY,
        confidence=0.8,
        is_safe_to_apply=False,
    )


def _rewrite_sql_execute(m: re.Match) -> str:
    prefix = m.group(1)
    raw = m.group(2).strip()
    # "... %s ..." % var  ->  "... %s ...", (var,)
    if "%" in raw:
        parts = raw.split("%", 1)
        query_str = parts[0].rstrip().rstrip("%").rstrip()
        var_part = parts[1].lstrip().lstrip("(").rstrip(")").strip()
        if not query_str.endswith(("\"", "'")):
            query_str = query_str + '"'
        return f'{prefix}{query_str}, ({var_part},)'
    return m.group(0)


def _rewrite_fstring_sql(m: re.Match) -> str:
    prefix = m.group(1)
    quote = m.group(2)
    body = m.group(3)
    # Extract f-string expressions
    params: list[str] = re.findall(r"\{(\w+)\}", body)
    clean_body = re.sub(r"\{(\w+)\}", "%s", body)
    params_tuple = ", ".join(params) + ("," if len(params) == 1 else "")
    return f'{prefix}{quote}{clean_body}{quote}, ({params_tuple})'


def _fix_command_injection(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S1002 -- replace os.system / shell=True with subprocess.run(list)."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    # os.system("cmd " + var) -> subprocess.run(["cmd", var], check=True)
    suggested = re.sub(
        r'os\.system\(\s*(.+?)\s*\)',
        lambda m: _rewrite_os_system(m.group(1)),
        suggested,
    )

    # subprocess.call(cmd, shell=True) -> subprocess.run(shlex.split(cmd), check=True)
    suggested = re.sub(
        r'subprocess\.\w+\((.+?),\s*shell\s*=\s*True',
        lambda m: f'subprocess.run(shlex.split({m.group(1)}), check=True',
        suggested,
    )

    if suggested == original:
        suggested = (
            "# TODO: Replace with subprocess.run() using a list of arguments\n"
            "# Example: subprocess.run([\"cmd\", arg], check=True)\n"
            + original
        )
    else:
        # Ensure import hint
        if "import subprocess" not in suggested and "import shlex" not in suggested:
            suggested = "import subprocess, shlex  # ensure these are imported\n" + suggested

    return FixSuggestion(
        rule_id="S1002",
        title="Use subprocess with argument list",
        description="Replace os.system() or shell=True with subprocess.run() using an explicit argument list to prevent command injection.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.EASY,
        confidence=0.75,
        is_safe_to_apply=False,
    )


def _rewrite_os_system(arg: str) -> str:
    arg = arg.strip().strip("\"'")
    parts = arg.split()
    if len(parts) > 1:
        cmd_list = ", ".join(f'"{p}"' for p in parts)
        return f"subprocess.run([{cmd_list}], check=True)"
    return f'subprocess.run(["{arg}"], check=True)'


def _fix_xss(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S1003 -- add html.escape() around user-controlled output."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    # Wrap bare variable interpolations in f-strings with html.escape()
    suggested = re.sub(
        r'\{(\w+)\}',
        r'{html.escape(str(\1))}',
        suggested,
    )

    if suggested == original:
        suggested = (
            "# TODO: Escape user-supplied data before rendering in HTML\n"
            "# Example: html.escape(user_input)\n"
            + original
        )
    else:
        suggested = "import html  # ensure this is imported\n" + suggested

    return FixSuggestion(
        rule_id="S1003",
        title="Escape output to prevent XSS",
        description="Wrap user-controlled values with html.escape() before embedding in HTML responses.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.EASY,
        confidence=0.7,
        is_safe_to_apply=False,
    )


def _fix_hardcoded_credentials(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S1005 -- replace hardcoded passwords / secrets with env lookups."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    # password = "secret"  ->  password = os.environ.get("PASSWORD", "")
    suggested = re.sub(
        r'''(\b(?:password|passwd|secret|token|api_key|apikey|auth_token)\s*=\s*)(?:f?["'])[^"']*(?:["'])''',
        lambda m: f'{m.group(1)}os.environ.get("{_env_var_name(m.group(0))}", "")',
        suggested,
        flags=re.IGNORECASE,
    )

    if suggested == original:
        suggested = (
            "# TODO: Move credentials to environment variables\n"
            "# Example: password = os.environ.get(\"DB_PASSWORD\", \"\")\n"
            + original
        )
    else:
        suggested = "import os  # ensure this is imported\n" + suggested

    return FixSuggestion(
        rule_id="S1005",
        title="Move credentials to environment variables",
        description="Replace hardcoded passwords, tokens, and API keys with os.environ.get() lookups.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.EASY,
        confidence=0.85,
        is_safe_to_apply=False,
    )


def _env_var_name(text: str) -> str:
    """Derive an environment variable name from an assignment."""
    m = re.match(r'(\w+)\s*=', text)
    if m:
        return m.group(1).upper()
    return "SECRET"


def _fix_weak_crypto(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S1006 -- replace MD5 / SHA-1 with SHA-256."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original.replace("hashlib.md5", "hashlib.sha256")
    suggested = suggested.replace("hashlib.sha1", "hashlib.sha256")
    suggested = suggested.replace("MD5.new", "SHA256.new")
    suggested = suggested.replace("SHA.new", "SHA256.new")

    if suggested == original:
        suggested = (
            "# TODO: Replace weak hash (MD5/SHA-1) with SHA-256 or better\n"
            + original
        )

    return FixSuggestion(
        rule_id="S1006",
        title="Replace weak cryptographic hash",
        description="Replace MD5 or SHA-1 with SHA-256 to avoid collision attacks.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.TRIVIAL,
        confidence=0.95,
        is_safe_to_apply=True,
    )


def _fix_insecure_hash(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S1007 -- replace hashlib.md5 with hashlib.sha256."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original.replace("hashlib.md5", "hashlib.sha256")
    suggested = suggested.replace("hashlib.sha1", "hashlib.sha256")

    if suggested == original:
        suggested = (
            "# TODO: Replace insecure hash with hashlib.sha256\n" + original
        )

    return FixSuggestion(
        rule_id="S1007",
        title="Replace insecure hash function",
        description="Replace hashlib.md5/sha1 with hashlib.sha256 for stronger integrity guarantees.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.TRIVIAL,
        confidence=0.95,
        is_safe_to_apply=True,
    )


def _fix_insecure_random(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S1008 -- replace random.random with secrets module."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    suggested = re.sub(r'random\.random\(\)', 'secrets.token_hex(16)', suggested)
    suggested = re.sub(r'random\.randint\([^)]*\)', 'secrets.randbelow(256)', suggested)
    suggested = re.sub(r'random\.choice\(([^)]+)\)', r'secrets.choice(\1)', suggested)

    if suggested == original:
        suggested = (
            "# TODO: Replace random module with secrets for security-sensitive use\n"
            "# Example: secrets.token_hex(16)\n"
            + original
        )
    else:
        suggested = "import secrets  # ensure this is imported\n" + suggested

    return FixSuggestion(
        rule_id="S1008",
        title="Use cryptographically secure randomness",
        description="Replace random module with secrets module for security-sensitive random values.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.EASY,
        confidence=0.85,
        is_safe_to_apply=False,
    )


def _fix_path_traversal(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S1010 -- add Path.resolve() and base-directory validation."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = (
        "from pathlib import Path  # ensure this is imported\n"
        "\n"
        "# Validate path stays within allowed base directory\n"
        "BASE_DIR = Path(\"/safe/base/directory\")  # adjust to your base\n"
        "resolved = Path(user_supplied_path).resolve()\n"
        "if not str(resolved).startswith(str(BASE_DIR.resolve())):\n"
        '    raise ValueError("Path traversal attempt detected")\n'
        "\n"
        + original
    )

    return FixSuggestion(
        rule_id="S1010",
        title="Add path traversal protection",
        description="Use Path.resolve() and validate that the resolved path stays within an allowed base directory.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.MODERATE,
        confidence=0.65,
        is_safe_to_apply=False,
    )


def _fix_path_traversal_variant(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S2083 -- add Path.resolve() guard."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    # open(user_path) -> open(Path(user_path).resolve())
    suggested = re.sub(
        r'open\((\w+)',
        r'open(str(Path(\1).resolve())',
        suggested,
    )

    if suggested == original:
        suggested = (
            "from pathlib import Path  # ensure this is imported\n"
            "# TODO: Resolve path and validate before use\n"
            "# resolved = Path(user_path).resolve()\n"
            + original
        )

    return FixSuggestion(
        rule_id="S2083",
        title="Resolve path before file access",
        description="Apply Path.resolve() to eliminate directory traversal sequences before accessing the file system.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.MODERATE,
        confidence=0.65,
        is_safe_to_apply=False,
    )


# ── Python Code Smells ────────────────────────────────────────────────────


def _fix_function_too_long(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S3001 -- suggest extraction (not auto-applicable)."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    return FixSuggestion(
        rule_id="S3001",
        title="Extract helper functions",
        description=(
            "This function is too long. Consider breaking it into smaller, "
            "well-named helper functions that each do one thing."
        ),
        original_code=snippet,
        suggested_code=(
            "# This function is too long. Consider splitting it:\n"
            "#\n"
            "# def _step_one(...):\n"
            "#     ...\n"
            "#\n"
            "# def _step_two(...):\n"
            "#     ...\n"
            "#\n"
            "# Then call them from the original function.\n"
            + snippet
        ),
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.HARD,
        confidence=0.3,
        is_safe_to_apply=False,
    )


def _fix_mutable_default_args(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S3004 -- replace mutable default with None + body assignment."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    # def foo(x=[]):  ->  def foo(x=None):  \n    if x is None: x = []
    def _replace_mutable(m: re.Match) -> str:
        param = m.group(1)
        default_val = m.group(2)
        return f"{param}=None"

    # Collect replaced defaults for body insertion
    defaults_found: list[tuple[str, str]] = []

    def _collect_and_replace(m: re.Match) -> str:
        param = m.group(1)
        default_val = m.group(2)
        defaults_found.append((param.strip(), default_val.strip()))
        return f"{param}=None"

    suggested = re.sub(
        r'(\w+)\s*=\s*(\[\]|\{\})',
        _collect_and_replace,
        suggested,
    )

    if defaults_found:
        # Add body assignments after the def line
        body_lines = []
        for param_name, default_val in defaults_found:
            body_lines.append(f"    if {param_name} is None:\n        {param_name} = {default_val}")
        body_insert = "\n".join(body_lines)

        # Insert after the first line ending with ":"
        lines = suggested.split("\n")
        insert_idx = 0
        for i, line in enumerate(lines):
            if line.rstrip().endswith(":"):
                insert_idx = i + 1
                break
        lines.insert(insert_idx, body_insert)
        suggested = "\n".join(lines)

    if suggested == original:
        suggested = (
            "# TODO: Replace mutable default argument with None\n"
            "# def foo(items=None):\n"
            "#     if items is None:\n"
            "#         items = []\n"
            + original
        )

    return FixSuggestion(
        rule_id="S3004",
        title="Replace mutable default argument",
        description="Replace mutable default argument (list/dict) with None and assign inside the function body.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.EASY,
        confidence=0.9,
        is_safe_to_apply=True,
    )


def _fix_unused_variables(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S3007 -- prefix unused variable with underscore."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    # Try to extract the variable name from the issue message
    var_match = re.search(r"['\"](\w+)['\"]", issue.message)
    if var_match:
        var_name = var_match.group(1)
        if not var_name.startswith("_"):
            suggested = suggested.replace(var_name, f"_{var_name}", 1)

    if suggested == original:
        suggested = "# TODO: Prefix unused variable with underscore (e.g., _unused)\n" + original

    return FixSuggestion(
        rule_id="S3007",
        title="Prefix unused variable with underscore",
        description="Prefix the unused variable name with an underscore to indicate it is intentionally unused.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.TRIVIAL,
        confidence=0.9,
        is_safe_to_apply=True,
    )


def _fix_print_statements(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S3010 -- replace print() with logging."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    suggested = re.sub(
        r'print\((.+?)\)',
        r'logger.info(\1)',
        suggested,
    )

    if suggested == original:
        suggested = (
            "# TODO: Replace print statement with logging\n"
            + original
        )
    else:
        suggested = (
            "import logging  # ensure this is imported\n"
            "logger = logging.getLogger(__name__)\n"
            + suggested
        )

    return FixSuggestion(
        rule_id="S3010",
        title="Replace print with logging",
        description="Replace print() calls with logging.getLogger(__name__).info() for proper log management.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.EASY,
        confidence=0.85,
        is_safe_to_apply=False,
    )


def _fix_global_statement(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S3011 -- suggest refactoring global statement to class or parameter."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    return FixSuggestion(
        rule_id="S3011",
        title="Refactor global statement",
        description=(
            "Replace the global statement with a class attribute, function parameter, "
            "or module-level constant. Global mutable state makes code harder to test and reason about."
        ),
        original_code=snippet,
        suggested_code=(
            "# TODO: Refactor to avoid 'global'. Options:\n"
            "# 1. Pass value as function parameter\n"
            "# 2. Use a class with an instance attribute\n"
            "# 3. Return the new value instead of mutating a global\n"
            + snippet
        ),
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.HARD,
        confidence=0.3,
        is_safe_to_apply=False,
    )


def _fix_bare_except(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S3013 -- replace bare except with except Exception."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = re.sub(r'except\s*:', 'except Exception:', original)

    if suggested == original:
        suggested = "# TODO: Replace bare except with 'except Exception:'\n" + original

    return FixSuggestion(
        rule_id="S3013",
        title="Replace bare except with specific exception",
        description="Replace bare 'except:' with 'except Exception:' to avoid catching SystemExit, KeyboardInterrupt, etc.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.TRIVIAL,
        confidence=0.95,
        is_safe_to_apply=True,
    )


def _fix_comparison_to_none(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S3014 -- replace == None / != None with is None / is not None."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = re.sub(r'(\w+)\s*==\s*None', r'\1 is None', original)
    suggested = re.sub(r'(\w+)\s*!=\s*None', r'\1 is not None', suggested)

    if suggested == original:
        suggested = "# TODO: Use 'is None' / 'is not None' instead of == / !=\n" + original

    return FixSuggestion(
        rule_id="S3014",
        title="Use identity comparison with None",
        description="Replace '== None' with 'is None' and '!= None' with 'is not None' per PEP 8.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.TRIVIAL,
        confidence=0.98,
        is_safe_to_apply=True,
    )


def _fix_comparison_to_bool(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S3015 -- replace == True / == False with truthiness check."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = re.sub(r'(\w+)\s*==\s*True\b', r'\1', original)
    suggested = re.sub(r'(\w+)\s*==\s*False\b', r'not \1', suggested)
    suggested = re.sub(r'(\w+)\s*is\s+True\b', r'\1', suggested)
    suggested = re.sub(r'(\w+)\s*is\s+False\b', r'not \1', suggested)

    if suggested == original:
        suggested = "# TODO: Use truthiness check instead of comparing to True/False\n" + original

    return FixSuggestion(
        rule_id="S3015",
        title="Simplify boolean comparison",
        description="Replace explicit comparison to True/False with direct truthiness check per PEP 8.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.TRIVIAL,
        confidence=0.95,
        is_safe_to_apply=True,
    )


# ── JavaScript Security ───────────────────────────────────────────────────


def _fix_js_sql_injection(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S4001 -- replace JS string concat SQL with parameterized query."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    # db.query("SELECT * FROM users WHERE id = " + userId)
    # -> db.query("SELECT * FROM users WHERE id = $1", [userId])
    suggested = re.sub(
        r'(\.query\s*\(\s*)([\"\'])(.+?)\2\s*\+\s*(\w+)\s*\)',
        r'\1"\3$1", [\4])',
        suggested,
    )

    # Template literal: db.query(`SELECT ... ${var}`)
    suggested = re.sub(
        r'(\.query\s*\(\s*)`([^`]*)\$\{(\w+)\}`',
        lambda m: f'{m.group(1)}"{m.group(2).replace("${" + m.group(3) + "}", "$1")}", [{m.group(3)}]',
        suggested,
    )

    if suggested == original:
        suggested = (
            "// TODO: Use parameterized queries instead of string concatenation\n"
            '// Example: db.query("SELECT * FROM users WHERE id = $1", [userId])\n'
            + original
        )

    return FixSuggestion(
        rule_id="S4001",
        title="Use parameterized SQL query",
        description="Replace string concatenation in SQL queries with parameterized placeholders.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.EASY,
        confidence=0.75,
        is_safe_to_apply=False,
    )


def _fix_js_xss(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S4003 -- add DOMPurify.sanitize()."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    # innerHTML = userInput -> innerHTML = DOMPurify.sanitize(userInput)
    suggested = re.sub(
        r'(\.innerHTML\s*=\s*)(\w+)',
        r'\1DOMPurify.sanitize(\2)',
        suggested,
    )

    # document.write(data) -> document.write(DOMPurify.sanitize(data))
    suggested = re.sub(
        r'(document\.write\s*\(\s*)(\w+)(\s*\))',
        r'\1DOMPurify.sanitize(\2)\3',
        suggested,
    )

    if suggested == original:
        suggested = (
            "// TODO: Sanitize user input before inserting into DOM\n"
            "// Example: DOMPurify.sanitize(userInput)\n"
            + original
        )

    return FixSuggestion(
        rule_id="S4003",
        title="Sanitize DOM output with DOMPurify",
        description="Wrap user-controlled values with DOMPurify.sanitize() before inserting into the DOM.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.EASY,
        confidence=0.75,
        is_safe_to_apply=False,
    )


def _fix_eval_usage(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S4005 -- replace eval() with safer alternatives."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = original

    # eval(jsonString) -> JSON.parse(jsonString)
    suggested = re.sub(
        r'eval\((\w+)\)',
        r'JSON.parse(\1)',
        suggested,
    )

    if suggested == original:
        suggested = (
            "// TODO: Replace eval() with a safer alternative\n"
            "// For JSON: JSON.parse(data)\n"
            "// For expressions: use Function constructor with caution\n"
            + original
        )

    return FixSuggestion(
        rule_id="S4005",
        title="Replace eval() with JSON.parse or Function",
        description="Replace eval() with JSON.parse() for data parsing or a restricted Function constructor for expressions.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.MODERATE,
        confidence=0.7,
        is_safe_to_apply=False,
    )


def _fix_prototype_pollution(issue: Issue, source_lines: list[str]) -> FixSuggestion | None:
    """S4006 -- add prototype pollution protection."""
    snippet = _extract_snippet(source_lines, issue.location.start_line, issue.location.end_line)
    if not snippet:
        return None

    original = snippet
    suggested = (
        "// Guard against prototype pollution\n"
        "function safeAssign(target, key, value) {\n"
        '  if (key === "__proto__" || key === "constructor" || key === "prototype") {\n'
        "    return;\n"
        "  }\n"
        "  if (Object.prototype.hasOwnProperty.call(target, key) || !(key in target)) {\n"
        "    target[key] = value;\n"
        "  }\n"
        "}\n"
        "\n"
        + original
    )

    return FixSuggestion(
        rule_id="S4006",
        title="Guard against prototype pollution",
        description="Add hasOwnProperty checks and block __proto__/constructor/prototype keys to prevent prototype pollution.",
        original_code=original,
        suggested_code=suggested,
        file_path=str(issue.location.file_path),
        start_line=issue.location.start_line,
        end_line=issue.location.end_line,
        difficulty=FixDifficulty.MODERATE,
        confidence=0.6,
        is_safe_to_apply=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_snippet(source_lines: list[str], start_line: int, end_line: int) -> str:
    """Extract the relevant source lines (1-indexed) as a single string."""
    if not source_lines:
        return ""
    # Clamp to valid range
    start = max(0, start_line - 1)
    end = min(len(source_lines), end_line)
    if start >= end:
        # Fallback: return the single start line if possible
        if 0 <= start < len(source_lines):
            return source_lines[start]
        return ""
    return "\n".join(source_lines[start:end])


# ---------------------------------------------------------------------------
# Fixer registry
# ---------------------------------------------------------------------------

_FIXER_REGISTRY: dict[str, FixerFn] = {
    # Python Security
    "S1001": _fix_sql_injection,
    "S1002": _fix_command_injection,
    "S1003": _fix_xss,
    "S1005": _fix_hardcoded_credentials,
    "S1006": _fix_weak_crypto,
    "S1007": _fix_insecure_hash,
    "S1008": _fix_insecure_random,
    "S1010": _fix_path_traversal,
    "S2083": _fix_path_traversal_variant,
    # Python Code Smells
    "S3001": _fix_function_too_long,
    "S3004": _fix_mutable_default_args,
    "S3007": _fix_unused_variables,
    "S3010": _fix_print_statements,
    "S3011": _fix_global_statement,
    "S3013": _fix_bare_except,
    "S3014": _fix_comparison_to_none,
    "S3015": _fix_comparison_to_bool,
    # JavaScript Security
    "S4001": _fix_js_sql_injection,
    "S4003": _fix_js_xss,
    "S4005": _fix_eval_usage,
    "S4006": _fix_prototype_pollution,
}

# Human-readable metadata for each fixer
_FIXER_META: dict[str, dict[str, str]] = {
    "S1001": {"name": "SQL Injection", "language": "python", "category": "security"},
    "S1002": {"name": "Command Injection", "language": "python", "category": "security"},
    "S1003": {"name": "XSS", "language": "python", "category": "security"},
    "S1005": {"name": "Hardcoded Credentials", "language": "python", "category": "security"},
    "S1006": {"name": "Weak Crypto", "language": "python", "category": "security"},
    "S1007": {"name": "Insecure Hash", "language": "python", "category": "security"},
    "S1008": {"name": "Insecure Random", "language": "python", "category": "security"},
    "S1010": {"name": "Path Traversal", "language": "python", "category": "security"},
    "S2083": {"name": "Path Traversal (variant)", "language": "python", "category": "security"},
    "S3001": {"name": "Function Too Long", "language": "python", "category": "code_smell"},
    "S3004": {"name": "Mutable Default Args", "language": "python", "category": "code_smell"},
    "S3007": {"name": "Unused Variables", "language": "python", "category": "code_smell"},
    "S3010": {"name": "Print Statements", "language": "python", "category": "code_smell"},
    "S3011": {"name": "Global Statement", "language": "python", "category": "code_smell"},
    "S3013": {"name": "Bare Except", "language": "python", "category": "code_smell"},
    "S3014": {"name": "Comparison to None", "language": "python", "category": "code_smell"},
    "S3015": {"name": "Comparison to True/False", "language": "python", "category": "code_smell"},
    "S4001": {"name": "JS SQL Injection", "language": "javascript", "category": "security"},
    "S4003": {"name": "JS XSS", "language": "javascript", "category": "security"},
    "S4005": {"name": "Eval Usage", "language": "javascript", "category": "security"},
    "S4006": {"name": "Prototype Pollution", "language": "javascript", "category": "security"},
}

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class AutoFixEngine:
    """Main entry point for generating and applying auto-fix suggestions."""

    def __init__(self) -> None:
        self._fixers: dict[str, FixerFn] = dict(_FIXER_REGISTRY)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def suggest_fixes(self, issues: list[Issue]) -> FixResult:
        """Generate fix suggestions for a batch of issues.

        For each issue whose ``rule_id`` has a registered fixer, the engine
        attempts to read the relevant source file and produce a concrete
        code-change suggestion.
        """
        result = FixResult()

        for issue in issues:
            suggestion = self.suggest_fix(issue)
            if suggestion is not None:
                result.suggestions.append(suggestion)
            else:
                result.skipped_count += 1

        return result

    def suggest_fix(
        self,
        issue: Issue,
        source_code: str | None = None,
    ) -> FixSuggestion | None:
        """Generate a fix suggestion for a single issue.

        Parameters
        ----------
        issue:
            The issue to fix.
        source_code:
            If provided, used instead of reading the file from disk.
        """
        fixer = self._fixers.get(issue.rule_id)
        if fixer is None:
            logger.debug("No fixer registered for rule %s", issue.rule_id)
            return None

        source_lines = self._get_source_lines(issue, source_code)
        if source_lines is None:
            logger.warning(
                "Could not read source for %s (rule %s)",
                issue.location.file_path,
                issue.rule_id,
            )
            return None

        try:
            return fixer(issue, source_lines)
        except Exception:
            logger.exception(
                "Fixer for %s raised an exception on %s:%d",
                issue.rule_id,
                issue.location.file_path,
                issue.location.start_line,
            )
            return None

    def apply_fixes(
        self,
        suggestions: list[FixSuggestion],
        dry_run: bool = True,
    ) -> FixResult:
        """Apply fix suggestions to source files.

        When *dry_run* is ``True`` (the default) no files are modified --
        the result simply reports what *would* be changed.

        When applying, fixes are grouped by file and applied from the
        **bottom** of the file upward so that line-number shifts do not
        invalidate earlier fixes.
        """
        result = FixResult(suggestions=list(suggestions))

        if dry_run:
            result.applied_count = 0
            result.skipped_count = len(suggestions)
            return result

        # Group suggestions by file, then sort each group bottom-to-top
        by_file: dict[str, list[FixSuggestion]] = {}
        for s in suggestions:
            by_file.setdefault(s.file_path, []).append(s)

        for file_path, fixes in by_file.items():
            path = Path(file_path)
            if not path.is_file():
                logger.warning("File not found, skipping: %s", file_path)
                result.skipped_count += len(fixes)
                continue

            try:
                content = path.read_text(encoding="utf-8")
            except OSError:
                logger.warning("Cannot read file, skipping: %s", file_path)
                result.skipped_count += len(fixes)
                continue

            lines = content.splitlines(keepends=True)

            # Sort fixes bottom-to-top to avoid line-shift issues
            fixes.sort(key=lambda f: f.start_line, reverse=True)

            applied_in_file = 0
            for fix in fixes:
                if not fix.is_safe_to_apply:
                    result.skipped_count += 1
                    continue

                start_idx = max(0, fix.start_line - 1)
                end_idx = min(len(lines), fix.end_line)

                # Replace the affected lines with the suggested code
                new_lines = fix.suggested_code.splitlines(keepends=True)
                # Ensure last line has a newline
                if new_lines and not new_lines[-1].endswith("\n"):
                    new_lines[-1] += "\n"

                lines[start_idx:end_idx] = new_lines
                applied_in_file += 1

            if applied_in_file > 0:
                try:
                    path.write_text("".join(lines), encoding="utf-8")
                    result.applied_count += applied_in_file
                except OSError:
                    logger.exception("Failed to write %s", file_path)
                    result.skipped_count += applied_in_file
            else:
                result.skipped_count += len(fixes)

        return result

    def get_available_fixers(self) -> list[dict[str, str]]:
        """Return metadata about all registered auto-fixers."""
        fixers: list[dict[str, str]] = []
        for rule_id in sorted(self._fixers):
            meta = _FIXER_META.get(rule_id, {})
            fixers.append({
                "rule_id": rule_id,
                "name": meta.get("name", rule_id),
                "language": meta.get("language", "unknown"),
                "category": meta.get("category", "unknown"),
            })
        return fixers

    def register_fixer(self, rule_id: str, fixer: FixerFn) -> None:
        """Register a custom fixer function at runtime."""
        self._fixers[rule_id] = fixer

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_source_lines(
        issue: Issue,
        source_code: str | None,
    ) -> list[str] | None:
        """Return the full source lines for the file that *issue* refers to."""
        if source_code is not None:
            return source_code.splitlines()

        file_path = Path(issue.location.file_path)
        if not file_path.is_file():
            return None

        try:
            return file_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
