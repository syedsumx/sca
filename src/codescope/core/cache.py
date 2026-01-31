"""Caching utilities for CodeScope analysis."""

import hashlib
import json
import pickle
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
import os
import time


@dataclass
class CacheConfig:
    """Configuration for analysis cache."""

    cache_dir: Optional[Path] = None
    max_size_mb: int = 500
    ttl_seconds: int = 86400 * 7  # 7 days
    enabled: bool = True

    def __post_init__(self):
        if self.cache_dir is None:
            # Default to user cache directory
            cache_base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
            self.cache_dir = cache_base / 'codescope'

        self.cache_dir.mkdir(parents=True, exist_ok=True)


class FileHasher:
    """Utility for computing file content hashes."""

    @staticmethod
    def hash_file(file_path: Path) -> str:
        """Compute SHA256 hash of a file's contents."""
        hasher = hashlib.sha256()

        with open(str(Path(file_path).resolve()), 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                hasher.update(chunk)

        return hasher.hexdigest()

    @staticmethod
    def hash_string(content: str) -> str:
        """Compute SHA256 hash of a string."""
        return hashlib.sha256(content.encode('utf-8')).hexdigest()

    @staticmethod
    def hash_files(files: list[Path]) -> str:
        """Compute combined hash of multiple files."""
        hasher = hashlib.sha256()

        for file_path in sorted(files):
            hasher.update(str(file_path).encode('utf-8'))
            hasher.update(FileHasher.hash_file(file_path).encode('utf-8'))

        return hasher.hexdigest()


class AnalysisCache:
    """SQLite-based cache for analysis results."""

    def __init__(self, config: Optional[CacheConfig] = None):
        """Initialize analysis cache."""
        self.config = config or CacheConfig()

        if not self.config.enabled:
            self._db = None
            return

        self._db_path = self.config.cache_dir / 'analysis_cache.db'
        self._db = sqlite3.connect(str(self._db_path))
        self._init_db()

    def _init_db(self):
        """Initialize database schema."""
        cursor = self._db.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                hash TEXT NOT NULL,
                data BLOB NOT NULL,
                created_at INTEGER NOT NULL,
                accessed_at INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL
            )
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_hash ON cache(hash)
        ''')

        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_accessed_at ON cache(accessed_at)
        ''')

        self._db.commit()

    def get(self, key: str, content_hash: Optional[str] = None) -> Optional[Any]:
        """Get cached value.

        Args:
            key: Cache key (e.g., file path)
            content_hash: Optional hash to validate freshness

        Returns:
            Cached value or None if not found/stale
        """
        if not self._db:
            return None

        cursor = self._db.cursor()

        cursor.execute(
            'SELECT hash, data, created_at FROM cache WHERE key = ?',
            (key,)
        )

        row = cursor.fetchone()
        if not row:
            return None

        stored_hash, data, created_at = row

        # Check hash if provided
        if content_hash and stored_hash != content_hash:
            return None

        # Check TTL
        if time.time() - created_at > self.config.ttl_seconds:
            self.delete(key)
            return None

        # Update access time
        cursor.execute(
            'UPDATE cache SET accessed_at = ? WHERE key = ?',
            (int(time.time()), key)
        )
        self._db.commit()

        try:
            return pickle.loads(data)
        except Exception:
            self.delete(key)
            return None

    def put(self, key: str, value: Any, content_hash: str) -> None:
        """Store a value in the cache.

        Args:
            key: Cache key
            value: Value to cache
            content_hash: Hash of source content
        """
        if not self._db:
            return

        try:
            data = pickle.dumps(value)
        except Exception:
            return

        size_bytes = len(data)
        current_time = int(time.time())

        cursor = self._db.cursor()

        cursor.execute('''
            INSERT OR REPLACE INTO cache (key, hash, data, created_at, accessed_at, size_bytes)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (key, content_hash, data, current_time, current_time, size_bytes))

        self._db.commit()

        # Cleanup if needed
        self._maybe_cleanup()

    def delete(self, key: str) -> None:
        """Delete a cached value."""
        if not self._db:
            return

        cursor = self._db.cursor()
        cursor.execute('DELETE FROM cache WHERE key = ?', (key,))
        self._db.commit()

    def clear(self) -> None:
        """Clear all cached values."""
        if not self._db:
            return

        cursor = self._db.cursor()
        cursor.execute('DELETE FROM cache')
        self._db.commit()

    def _maybe_cleanup(self) -> None:
        """Clean up old entries if cache is too large."""
        if not self._db:
            return

        cursor = self._db.cursor()

        # Get total size
        cursor.execute('SELECT SUM(size_bytes) FROM cache')
        total_size = cursor.fetchone()[0] or 0

        max_size_bytes = self.config.max_size_mb * 1024 * 1024

        if total_size > max_size_bytes:
            # Delete oldest accessed entries until under limit
            target_size = max_size_bytes * 0.8  # Aim for 80%

            cursor.execute('''
                DELETE FROM cache WHERE key IN (
                    SELECT key FROM cache
                    ORDER BY accessed_at ASC
                    LIMIT (SELECT COUNT(*) / 4 FROM cache)
                )
            ''')

            self._db.commit()

    def get_stats(self) -> dict:
        """Get cache statistics."""
        if not self._db:
            return {"enabled": False}

        cursor = self._db.cursor()

        cursor.execute('SELECT COUNT(*), SUM(size_bytes) FROM cache')
        count, total_size = cursor.fetchone()

        return {
            "enabled": True,
            "entries": count or 0,
            "size_bytes": total_size or 0,
            "size_mb": (total_size or 0) / (1024 * 1024),
            "max_size_mb": self.config.max_size_mb,
        }

    def close(self) -> None:
        """Close database connection."""
        if self._db:
            self._db.close()


class IncrementalAnalyzer:
    """Support for incremental analysis of changed files only."""

    def __init__(
        self,
        cache: Optional[AnalysisCache] = None,
        state_file: Optional[Path] = None,
    ):
        """Initialize incremental analyzer."""
        self.cache = cache or AnalysisCache()
        self.state_file = state_file
        self._file_hashes: dict[str, str] = {}

        if state_file and state_file.exists():
            self._load_state()

    def _load_state(self) -> None:
        """Load previous analysis state."""
        try:
            content = self.state_file.read_text(encoding='utf-8')
            self._file_hashes = json.loads(content)
        except Exception:
            self._file_hashes = {}

    def _save_state(self) -> None:
        """Save current analysis state."""
        if self.state_file:
            try:
                self.state_file.write_text(
                    json.dumps(self._file_hashes, indent=2),
                    encoding='utf-8'
                )
            except Exception:
                pass

    def get_changed_files(self, files: list[Path]) -> list[Path]:
        """Get list of files that have changed since last analysis.

        Args:
            files: All files to potentially analyze

        Returns:
            List of files that need (re)analysis
        """
        changed = []

        for file_path in files:
            current_hash = FileHasher.hash_file(file_path)
            path_str = str(file_path)

            previous_hash = self._file_hashes.get(path_str)

            if previous_hash != current_hash:
                changed.append(file_path)
                self._file_hashes[path_str] = current_hash

        return changed

    def mark_analyzed(self, file_path: Path) -> None:
        """Mark a file as analyzed."""
        self._file_hashes[str(file_path)] = FileHasher.hash_file(file_path)
        self._save_state()

    def mark_all_analyzed(self, files: list[Path]) -> None:
        """Mark multiple files as analyzed."""
        for file_path in files:
            self._file_hashes[str(file_path)] = FileHasher.hash_file(file_path)
        self._save_state()

    def invalidate(self, file_path: Path) -> None:
        """Invalidate cache for a file."""
        path_str = str(file_path)
        if path_str in self._file_hashes:
            del self._file_hashes[path_str]
        self._save_state()

    def invalidate_all(self) -> None:
        """Invalidate all cached state."""
        self._file_hashes = {}
        self._save_state()
