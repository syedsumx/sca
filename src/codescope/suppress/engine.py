"""Suppression engine for ignoring/suppressing analysis issues.

Supports four suppression sources:
- INLINE:  comment directives in source code (``# codescope:ignore ...``)
- FILE:    ``.codescopeignore`` pattern file at project root
- CONFIG:  ``suppressions:`` section in ``codescope.yml``
- API:     manual suppressions persisted in the SQLite store

All sources are evaluated in precedence order; the first matching
suppression wins.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from codescope.core.models import Issue

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Inline-comment regex
# ---------------------------------------------------------------------------
# Matches patterns such as:
#   # codescope:ignore S1001
#   # codescope:ignore S1001,S1002
#   # codescope:ignore * reason: false positive
#   // codescope:ignore S1001
#   /* codescope:ignore S1001 */
_INLINE_RE = re.compile(
    r"(?:#|//|/\*)\s*codescope:ignore\s+"
    r"(?P<rules>[A-Za-z0-9_*]+(?:\s*,\s*[A-Za-z0-9_*]+)*)"
    r"(?:\s+reason:\s*(?P<reason>[^*]*?))?"
    r"\s*(?:\*/\s*)?$"
)

# ISO-8601 format used for datetime serialisation
_ISO_FMT = "%Y-%m-%dT%H:%M:%S%z"


# ---------------------------------------------------------------------------
# SuppressionSource
# ---------------------------------------------------------------------------
class SuppressionSource(str, Enum):
    """Origin of a suppression directive."""

    INLINE = "inline"
    FILE = "file"
    CONFIG = "config"
    API = "api"


# ---------------------------------------------------------------------------
# Suppression dataclass
# ---------------------------------------------------------------------------
@dataclass
class Suppression:
    """A single suppression entry.

    Attributes:
        id:           Unique identifier (UUID).
        rule_id:      Rule to suppress (e.g. ``"S1001"``), or ``"*"`` for all.
        file_pattern: Glob pattern or exact file path (e.g. ``"src/**/*.py"``).
        line:         Specific source line, or ``None`` for the whole file.
        reason:       Human-readable justification.
        author:       Who created the suppression.
        created_at:   Timestamp of creation.
        expires_at:   Optional expiry timestamp.
        source:       Where the suppression originated.
    """

    rule_id: str
    file_pattern: str
    reason: str = ""
    author: str = ""
    source: SuppressionSource = SuppressionSource.API
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    line: int | None = None
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    expires_at: datetime | None = None

    # ---- properties -------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Return ``True`` if the suppression has not expired."""
        if self.expires_at is None:
            return True
        return datetime.now(timezone.utc) < self.expires_at

    # ---- serialisation ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dictionary."""
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "file_pattern": self.file_pattern,
            "line": self.line,
            "reason": self.reason,
            "author": self.author,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "source": self.source.value,
            "is_active": self.is_active,
        }


# ---------------------------------------------------------------------------
# SuppressionStore  (SQLite-backed persistence)
# ---------------------------------------------------------------------------
_CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS suppressions (
    id           TEXT PRIMARY KEY,
    rule_id      TEXT NOT NULL,
    file_pattern TEXT NOT NULL,
    line         INTEGER,
    reason       TEXT NOT NULL DEFAULT '',
    author       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    source       TEXT NOT NULL
);
"""


class SuppressionStore:
    """Persistent SQLite store for API / manual suppressions.

    The database file is placed at ``<project_root>/.codescope/suppressions.db``.
    """

    def __init__(self, project_root: Path) -> None:
        self._db_dir = project_root / ".codescope"
        self._db_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self._db_dir / "suppressions.db"
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.commit()

    # ---- internal helpers -------------------------------------------------

    def _row_to_suppression(self, row: sqlite3.Row) -> Suppression:
        """Map a database row to a :class:`Suppression`."""
        created = datetime.fromisoformat(row["created_at"])
        expires = (
            datetime.fromisoformat(row["expires_at"])
            if row["expires_at"]
            else None
        )
        return Suppression(
            id=row["id"],
            rule_id=row["rule_id"],
            file_pattern=row["file_pattern"],
            line=row["line"],
            reason=row["reason"],
            author=row["author"],
            created_at=created,
            expires_at=expires,
            source=SuppressionSource(row["source"]),
        )

    def _suppression_to_params(self, s: Suppression) -> tuple:
        """Return a parameter tuple for INSERT."""
        return (
            s.id,
            s.rule_id,
            s.file_pattern,
            s.line,
            s.reason,
            s.author,
            s.created_at.isoformat(),
            s.expires_at.isoformat() if s.expires_at else None,
            s.source.value,
        )

    # ---- public API -------------------------------------------------------

    def add(self, suppression: Suppression) -> Suppression:
        """Persist a new suppression and return it."""
        self._conn.execute(
            "INSERT INTO suppressions "
            "(id, rule_id, file_pattern, line, reason, author, created_at, expires_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            self._suppression_to_params(suppression),
        )
        self._conn.commit()
        return suppression

    def remove(self, suppression_id: str) -> bool:
        """Delete a suppression by *id*.  Return ``True`` if it existed."""
        cursor = self._conn.execute(
            "DELETE FROM suppressions WHERE id = ?",
            (suppression_id,),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def list_all(self) -> list[Suppression]:
        """Return every stored suppression (active and expired)."""
        rows = self._conn.execute(
            "SELECT * FROM suppressions ORDER BY created_at DESC",
        ).fetchall()
        return [self._row_to_suppression(r) for r in rows]

    def list_active(self) -> list[Suppression]:
        """Return only active (non-expired) suppressions."""
        return [s for s in self.list_all() if s.is_active]

    def list_expired(self) -> list[Suppression]:
        """Return only expired suppressions."""
        return [s for s in self.list_all() if not s.is_active]

    def find_by_rule(self, rule_id: str) -> list[Suppression]:
        """Return all suppressions for a given *rule_id*."""
        rows = self._conn.execute(
            "SELECT * FROM suppressions WHERE rule_id = ? ORDER BY created_at DESC",
            (rule_id,),
        ).fetchall()
        return [self._row_to_suppression(r) for r in rows]

    def find_by_file(self, file_path: str) -> list[Suppression]:
        """Return all suppressions whose pattern matches *file_path*."""
        all_suppressions = self.list_all()
        return [
            s for s in all_suppressions
            if fnmatch.fnmatch(file_path, s.file_pattern)
            or s.file_pattern == file_path
        ]

    def clear_expired(self) -> int:
        """Remove all expired suppressions and return the count removed."""
        expired = self.list_expired()
        if not expired:
            return 0
        ids = [s.id for s in expired]
        placeholders = ",".join("?" for _ in ids)
        cursor = self._conn.execute(
            f"DELETE FROM suppressions WHERE id IN ({placeholders})",  # noqa: S608
            ids,
        )
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# SuppressionEngine
# ---------------------------------------------------------------------------
class SuppressionEngine:
    """Central engine that aggregates suppressions from all sources.

    Usage::

        engine = SuppressionEngine(project_root)
        active, suppressed = engine.filter_issues(all_issues)
    """

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()
        self._store = SuppressionStore(self._project_root)

        # Caches filled lazily or explicitly
        self._inline_cache: dict[str, list[Suppression]] = {}
        self._ignorefile_suppressions: list[Suppression] | None = None
        self._config_suppressions: list[Suppression] = []

    # ---- properties -------------------------------------------------------

    @property
    def store(self) -> SuppressionStore:
        """Access the underlying persistent store."""
        return self._store

    # ---- source loaders ---------------------------------------------------

    def load_inline_suppressions(
        self,
        file_path: Path,
        source_code: str,
    ) -> list[Suppression]:
        """Parse inline ``codescope:ignore`` comments from *source_code*.

        Both "same-line" and "line-above" annotations are supported.
        Results are cached per *file_path* (string key).
        """
        key = str(file_path)
        if key in self._inline_cache:
            return self._inline_cache[key]

        suppressions: list[Suppression] = []
        lines = source_code.splitlines()
        relative = self._relative(file_path)

        for idx, line in enumerate(lines, start=1):
            match = _INLINE_RE.search(line)
            if not match:
                continue

            raw_rules = match.group("rules")
            reason = (match.group("reason") or "").strip()
            rule_ids = [r.strip() for r in raw_rules.split(",")]

            # If the line is *only* a comment (no code before the comment
            # marker), it applies to the *next* line.  Otherwise it applies
            # to the current line.
            stripped = line[:match.start()].strip()
            is_standalone_comment = stripped == ""
            target_line = idx + 1 if is_standalone_comment else idx

            for rid in rule_ids:
                suppressions.append(
                    Suppression(
                        rule_id=rid,
                        file_pattern=relative,
                        line=target_line,
                        reason=reason or "inline suppression",
                        author="inline",
                        source=SuppressionSource.INLINE,
                    )
                )

        self._inline_cache[key] = suppressions
        return suppressions

    def load_ignorefile(self, project_root: Path | None = None) -> list[Suppression]:
        """Parse the ``.codescopeignore`` file at *project_root*.

        File format (one directive per line)::

            # Comment line
            S1001:tests/**/*.py      # rule + file pattern
            *:src/generated/**       # all rules + file pattern
            S3010                    # rule globally (all files)

        Returns the parsed list and caches it internally.
        """
        root = (project_root or self._project_root).resolve()
        ignore_path = root / ".codescopeignore"

        suppressions: list[Suppression] = []
        if not ignore_path.is_file():
            self._ignorefile_suppressions = suppressions
            return suppressions

        try:
            text = ignore_path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Could not read %s", ignore_path)
            self._ignorefile_suppressions = suppressions
            return suppressions

        for raw_line in text.splitlines():
            line = raw_line.strip()
            # Skip blanks and comments
            if not line or line.startswith("#"):
                continue

            # Strip inline comments  (e.g. ``S1001  # some note``)
            if "  #" in line:
                line = line[: line.index("  #")].strip()

            if ":" in line:
                rule_id, file_pattern = line.split(":", maxsplit=1)
                rule_id = rule_id.strip()
                file_pattern = file_pattern.strip()
            else:
                rule_id = line
                file_pattern = "**/*"

            suppressions.append(
                Suppression(
                    rule_id=rule_id,
                    file_pattern=file_pattern,
                    reason="codescopeignore",
                    author="ignorefile",
                    source=SuppressionSource.FILE,
                )
            )

        self._ignorefile_suppressions = suppressions
        return suppressions

    def load_config_suppressions(self, config: dict[str, Any]) -> list[Suppression]:
        """Load suppressions from the ``suppressions:`` key of a config dict.

        Expected shape::

            suppressions:
              - rule_id: S1001
                file_pattern: "tests/**/*.py"
                reason: "Acceptable in test code"
              - rule_id: "*"
                file_pattern: "src/generated/**"
        """
        suppressions: list[Suppression] = []
        raw_list = config.get("suppressions", [])
        if not isinstance(raw_list, list):
            return suppressions

        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            rule_id = str(entry.get("rule_id", "")).strip()
            if not rule_id:
                continue

            file_pattern = str(entry.get("file_pattern", "**/*")).strip()
            reason = str(entry.get("reason", "config suppression")).strip()
            author = str(entry.get("author", "config")).strip()
            line = entry.get("line")
            expires_at_raw = entry.get("expires_at")

            expires_at: datetime | None = None
            if expires_at_raw:
                try:
                    expires_at = datetime.fromisoformat(str(expires_at_raw))
                except (ValueError, TypeError):
                    pass

            suppressions.append(
                Suppression(
                    rule_id=rule_id,
                    file_pattern=file_pattern,
                    line=int(line) if line is not None else None,
                    reason=reason,
                    author=author,
                    expires_at=expires_at,
                    source=SuppressionSource.CONFIG,
                )
            )

        self._config_suppressions = suppressions
        return suppressions

    # ---- matching ---------------------------------------------------------

    def is_suppressed(self, issue: Issue) -> bool:
        """Return ``True`` if *issue* matches any active suppression."""
        return self.get_suppression_for(issue) is not None

    def get_suppression_for(self, issue: Issue) -> Suppression | None:
        """Return the first matching :class:`Suppression` for *issue*, or ``None``."""
        relative = self._relative(issue.location.file_path)

        # 1. Inline suppressions
        inline = self._inline_cache.get(str(issue.location.file_path), [])
        for s in inline:
            if self._matches(s, issue, relative):
                return s

        # 2. .codescopeignore
        if self._ignorefile_suppressions is None:
            self.load_ignorefile()
        for s in (self._ignorefile_suppressions or []):
            if self._matches(s, issue, relative):
                return s

        # 3. Config suppressions
        for s in self._config_suppressions:
            if self._matches(s, issue, relative):
                return s

        # 4. API / store suppressions
        for s in self._store.list_active():
            if self._matches(s, issue, relative):
                return s

        return None

    def filter_issues(
        self,
        issues: list[Issue],
    ) -> tuple[list[Issue], list[Issue]]:
        """Split *issues* into ``(active, suppressed)`` lists."""
        active: list[Issue] = []
        suppressed: list[Issue] = []
        for issue in issues:
            if self.is_suppressed(issue):
                suppressed.append(issue)
            else:
                active.append(issue)
        return active, suppressed

    # ---- private helpers --------------------------------------------------

    def _matches(
        self,
        suppression: Suppression,
        issue: Issue,
        relative_path: str,
    ) -> bool:
        """Check whether *suppression* applies to *issue*."""
        # Must be active (not expired)
        if not suppression.is_active:
            return False

        # Rule must match
        if suppression.rule_id != "*" and suppression.rule_id != issue.rule_id:
            return False

        # File pattern must match
        if not self._file_matches(suppression.file_pattern, relative_path):
            return False

        # Line must match (if suppression specifies one)
        if suppression.line is not None:
            issue_line = issue.location.start_line
            if suppression.line != issue_line:
                return False

        return True

    def _file_matches(self, pattern: str, relative_path: str) -> bool:
        """Check if *pattern* matches the *relative_path*.

        Supports:
        - Exact match
        - fnmatch glob (with ``**`` translated to recursive match)
        """
        # Normalise separators
        pattern = pattern.replace("\\", "/")
        path = relative_path.replace("\\", "/")

        if pattern == path:
            return True

        # ``**`` is not natively supported by fnmatch, but we can translate
        # it to a pattern that works.  ``**`` should match zero or more
        # directories, which in fnmatch is roughly ``*`` for each segment.
        # We use a PurePosixPath-based approach for robustness.
        if "**" in pattern:
            # Convert ``**`` into a regex for proper recursive matching
            regex = self._glob_to_regex(pattern)
            return bool(re.fullmatch(regex, path))

        return fnmatch.fnmatch(path, pattern)

    @staticmethod
    def _glob_to_regex(pattern: str) -> str:
        """Convert a glob *pattern* (with ``**`` support) to a regex string."""
        # Escape everything except our glob wildcards
        parts: list[str] = []
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == "*":
                if i + 1 < len(pattern) and pattern[i + 1] == "*":
                    # ``**`` -> match anything (including path separators)
                    if i + 2 < len(pattern) and pattern[i + 2] == "/":
                        parts.append("(?:.+/)?")
                        i += 3
                        continue
                    parts.append(".*")
                    i += 2
                    continue
                else:
                    # Single ``*`` -> match anything except ``/``
                    parts.append("[^/]*")
            elif c == "?":
                parts.append("[^/]")
            elif c in r"\.+^${}()|[]":
                parts.append("\\" + c)
            else:
                parts.append(c)
            i += 1
        return "".join(parts)

    def _relative(self, file_path: Path | str) -> str:
        """Return *file_path* relative to the project root, as a forward-slash string."""
        try:
            return str(Path(file_path).resolve().relative_to(self._project_root)).replace(
                "\\", "/"
            )
        except ValueError:
            # Path is not under project root; return as-is
            return str(file_path).replace("\\", "/")
