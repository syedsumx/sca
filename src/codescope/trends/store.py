"""SQLite-backed storage for analysis trend data."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".codescope" / "trends.db"


@dataclass
class TrendSnapshot:
    """A point-in-time snapshot of analysis metrics."""

    project: str
    timestamp: str  # ISO-8601
    commit_sha: str = ""
    branch: str = ""
    total_issues: int = 0
    bugs: int = 0
    vulnerabilities: int = 0
    code_smells: int = 0
    blockers: int = 0
    critical: int = 0
    major: int = 0
    minor: int = 0
    coverage: float = 0.0
    duplication_pct: float = 0.0
    quality_gate: str = "none"  # passed, failed, warn, none
    extra: dict = field(default_factory=dict)


class TrendStore:
    """Persists and queries analysis snapshots for trend charts."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._path = Path(db_path) if db_path else _DEFAULT_DB
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                project     TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                commit_sha  TEXT DEFAULT '',
                branch      TEXT DEFAULT '',
                total_issues INTEGER DEFAULT 0,
                bugs        INTEGER DEFAULT 0,
                vulnerabilities INTEGER DEFAULT 0,
                code_smells INTEGER DEFAULT 0,
                blockers    INTEGER DEFAULT 0,
                critical    INTEGER DEFAULT 0,
                major       INTEGER DEFAULT 0,
                minor       INTEGER DEFAULT 0,
                coverage    REAL DEFAULT 0.0,
                duplication_pct REAL DEFAULT 0.0,
                quality_gate TEXT DEFAULT 'none',
                extra       TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_snap_project ON snapshots(project);
            CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(timestamp);
        """)
        self._conn.commit()

    def record(self, snapshot: TrendSnapshot) -> int:
        """Store a new snapshot, returns the row id."""
        cur = self._conn.execute(
            """INSERT INTO snapshots
               (project, timestamp, commit_sha, branch, total_issues, bugs,
                vulnerabilities, code_smells, blockers, critical, major, minor,
                coverage, duplication_pct, quality_gate, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot.project, snapshot.timestamp, snapshot.commit_sha,
                snapshot.branch, snapshot.total_issues, snapshot.bugs,
                snapshot.vulnerabilities, snapshot.code_smells,
                snapshot.blockers, snapshot.critical, snapshot.major,
                snapshot.minor, snapshot.coverage, snapshot.duplication_pct,
                snapshot.quality_gate, json.dumps(snapshot.extra),
            ),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def get_trends(
        self,
        project: str,
        limit: int = 50,
        branch: Optional[str] = None,
    ) -> list[dict]:
        """Get recent snapshots for a project, newest first."""
        sql = "SELECT * FROM snapshots WHERE project = ?"
        params: list = [project]
        if branch:
            sql += " AND branch = ?"
            params.append(branch)
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = self._conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["extra"] = json.loads(d.get("extra", "{}"))
            results.append(d)
        return list(reversed(results))  # chronological order

    def get_projects(self) -> list[str]:
        """List distinct projects with trend data."""
        rows = self._conn.execute("SELECT DISTINCT project FROM snapshots ORDER BY project").fetchall()
        return [r["project"] for r in rows]

    def get_delta(self, project: str) -> Optional[dict]:
        """Compare the two most recent snapshots — what changed."""
        rows = self._conn.execute(
            "SELECT * FROM snapshots WHERE project = ? ORDER BY timestamp DESC LIMIT 2",
            (project,),
        ).fetchall()
        if len(rows) < 2:
            return None
        current, previous = dict(rows[0]), dict(rows[1])
        delta: dict = {}
        for key in ("total_issues", "bugs", "vulnerabilities", "code_smells",
                     "blockers", "critical", "major", "minor", "coverage", "duplication_pct"):
            c_val = current.get(key, 0)
            p_val = previous.get(key, 0)
            diff = round(c_val - p_val, 2) if isinstance(c_val, float) else c_val - p_val
            delta[key] = {"current": c_val, "previous": p_val, "delta": diff}
        delta["quality_gate"] = {"current": current.get("quality_gate"), "previous": previous.get("quality_gate")}
        return delta

    def delete_project(self, project: str) -> int:
        """Delete all snapshots for a project."""
        cur = self._conn.execute("DELETE FROM snapshots WHERE project = ?", (project,))
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()
