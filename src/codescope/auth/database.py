"""SQLite-based auth database for users, API keys, and sessions."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from codescope.auth.models import APIKey, Role, User

logger = logging.getLogger(__name__)

# Use bcrypt if available, fallback to PBKDF2
try:
    import bcrypt

    def _hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    def _verify_password(password: str, hashed: str) -> bool:
        return bcrypt.checkpw(password.encode(), hashed.encode())

except ImportError:
    logger.info("bcrypt not installed, using PBKDF2 for password hashing")

    def _hash_password(password: str) -> str:
        salt = secrets.token_hex(16)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        return f"pbkdf2:{salt}:{h.hex()}"

    def _verify_password(password: str, hashed: str) -> bool:
        if not hashed.startswith("pbkdf2:"):
            return False
        _, salt, expected = hashed.split(":", 2)
        h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
        return secrets.compare_digest(h.hex(), expected)


def _hash_api_key(key: str) -> str:
    """Hash an API key using SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()


class AuthDatabase:
    """SQLite-backed auth storage."""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = os.environ.get(
                "CODESCOPE_AUTH_DB",
                str(Path.home() / ".codescope" / "auth.db"),
            )
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        conn = self._get_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE NOT NULL,
                    display_name TEXT DEFAULT '',
                    password_hash TEXT DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'viewer',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    is_sso INTEGER NOT NULL DEFAULT 0,
                    sso_provider TEXT DEFAULT '',
                    sso_id TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_login TEXT
                );

                CREATE TABLE IF NOT EXISTS api_keys (
                    key_id TEXT PRIMARY KEY,
                    key_hash TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'ci',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_used TEXT,
                    expires_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS refresh_tokens (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
                CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
                CREATE INDEX IF NOT EXISTS idx_users_sso ON users(sso_provider, sso_id);
                CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
                CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
            """)
            conn.commit()

            # Create default admin if no users exist
            row = conn.execute("SELECT COUNT(*) as cnt FROM users").fetchone()
            if row["cnt"] == 0:
                self._create_default_admin(conn)
        finally:
            conn.close()

    def _create_default_admin(self, conn: sqlite3.Connection) -> None:
        """Create default admin user on first run."""
        default_password = os.environ.get("CODESCOPE_ADMIN_PASSWORD", "admin")
        user_id = secrets.token_hex(16)
        now = datetime.utcnow().isoformat()

        conn.execute(
            """INSERT INTO users
               (user_id, username, email, display_name, password_hash, role, is_active, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
            (
                user_id,
                "admin",
                "admin@codescope.local",
                "Administrator",
                _hash_password(default_password),
                Role.ADMIN.value,
                now,
            ),
        )
        conn.commit()
        logger.info(
            "Created default admin user (username: admin). "
            "Change the password after first login."
        )

    # ── User operations ─────────────────────────────────────────

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        role: Role = Role.VIEWER,
        display_name: str = "",
    ) -> User:
        """Create a new local user."""
        conn = self._get_conn()
        try:
            user_id = secrets.token_hex(16)
            now = datetime.utcnow().isoformat()
            conn.execute(
                """INSERT INTO users
                   (user_id, username, email, display_name, password_hash, role, is_active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                (user_id, username, email, display_name, _hash_password(password), role.value, now),
            )
            conn.commit()
            return self.get_user_by_id(user_id)  # type: ignore
        finally:
            conn.close()

    def create_sso_user(
        self,
        username: str,
        email: str,
        provider: str,
        sso_id: str,
        display_name: str = "",
        role: Role = Role.ANALYST,
    ) -> User:
        """Create or update an SSO user."""
        conn = self._get_conn()
        try:
            # Check if SSO user already exists
            row = conn.execute(
                "SELECT user_id FROM users WHERE sso_provider=? AND sso_id=?",
                (provider, sso_id),
            ).fetchone()

            now = datetime.utcnow().isoformat()

            if row:
                # Update existing SSO user
                conn.execute(
                    """UPDATE users SET username=?, email=?, display_name=?, last_login=?
                       WHERE user_id=?""",
                    (username, email, display_name, now, row["user_id"]),
                )
                conn.commit()
                return self.get_user_by_id(row["user_id"])  # type: ignore
            else:
                user_id = secrets.token_hex(16)
                conn.execute(
                    """INSERT INTO users
                       (user_id, username, email, display_name, role,
                        is_active, is_sso, sso_provider, sso_id, created_at, last_login)
                       VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?, ?, ?)""",
                    (user_id, username, email, display_name, role.value,
                     provider, sso_id, now, now),
                )
                conn.commit()
                return self.get_user_by_id(user_id)  # type: ignore
        finally:
            conn.close()

    def authenticate(self, username: str, password: str) -> Optional[User]:
        """Authenticate with username/password. Returns User or None."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM users WHERE username=? AND is_active=1 AND is_sso=0",
                (username,),
            ).fetchone()

            if row is None:
                return None

            if not _verify_password(password, row["password_hash"]):
                return None

            # Update last login
            conn.execute(
                "UPDATE users SET last_login=? WHERE user_id=?",
                (datetime.utcnow().isoformat(), row["user_id"]),
            )
            conn.commit()

            return self._row_to_user(row)
        finally:
            conn.close()

    def get_user_by_id(self, user_id: str) -> Optional[User]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()

    def get_user_by_username(self, username: str) -> Optional[User]:
        conn = self._get_conn()
        try:
            row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return self._row_to_user(row) if row else None
        finally:
            conn.close()

    def list_users(self) -> list[User]:
        conn = self._get_conn()
        try:
            rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
            return [self._row_to_user(r) for r in rows]
        finally:
            conn.close()

    def update_user_role(self, user_id: str, role: Role) -> bool:
        conn = self._get_conn()
        try:
            conn.execute("UPDATE users SET role=? WHERE user_id=?", (role.value, user_id))
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def deactivate_user(self, user_id: str) -> bool:
        conn = self._get_conn()
        try:
            conn.execute("UPDATE users SET is_active=0 WHERE user_id=?", (user_id,))
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    def change_password(self, user_id: str, new_password: str) -> bool:
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE user_id=? AND is_sso=0",
                (_hash_password(new_password), user_id),
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    # ── API Key operations ──────────────────────────────────────

    def create_api_key(
        self,
        user_id: str,
        name: str,
        role: Role = Role.CI,
        expires_days: Optional[int] = None,
    ) -> tuple[str, APIKey]:
        """Create a new API key. Returns (raw_key, APIKey)."""
        conn = self._get_conn()
        try:
            raw_key = f"cs_{secrets.token_urlsafe(32)}"
            key_id = secrets.token_hex(8)
            now = datetime.utcnow().isoformat()
            expires_at = None
            if expires_days:
                expires_at = (datetime.utcnow() + timedelta(days=expires_days)).isoformat()

            conn.execute(
                """INSERT INTO api_keys
                   (key_id, key_hash, name, user_id, role, is_active, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
                (key_id, _hash_api_key(raw_key), name, user_id, role.value, now, expires_at),
            )
            conn.commit()

            api_key = APIKey(
                key_id=key_id,
                key_hash=_hash_api_key(raw_key),
                name=name,
                user_id=user_id,
                role=role,
                created_at=datetime.fromisoformat(now),
                expires_at=datetime.fromisoformat(expires_at) if expires_at else None,
            )
            return raw_key, api_key
        finally:
            conn.close()

    def validate_api_key(self, raw_key: str) -> Optional[tuple[APIKey, User]]:
        """Validate an API key and return the key + associated user."""
        conn = self._get_conn()
        try:
            key_hash = _hash_api_key(raw_key)
            row = conn.execute(
                "SELECT * FROM api_keys WHERE key_hash=? AND is_active=1",
                (key_hash,),
            ).fetchone()

            if row is None:
                return None

            api_key = APIKey(
                key_id=row["key_id"],
                key_hash=row["key_hash"],
                name=row["name"],
                user_id=row["user_id"],
                role=Role(row["role"]),
                is_active=bool(row["is_active"]),
                created_at=datetime.fromisoformat(row["created_at"]),
                last_used=datetime.fromisoformat(row["last_used"]) if row["last_used"] else None,
                expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            )

            if api_key.is_expired:
                return None

            # Update last_used
            conn.execute(
                "UPDATE api_keys SET last_used=? WHERE key_id=?",
                (datetime.utcnow().isoformat(), api_key.key_id),
            )
            conn.commit()

            user = self.get_user_by_id(api_key.user_id)
            if user is None or not user.is_active:
                return None

            return api_key, user
        finally:
            conn.close()

    def list_api_keys(self, user_id: str) -> list[APIKey]:
        conn = self._get_conn()
        try:
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            return [
                APIKey(
                    key_id=r["key_id"],
                    key_hash=r["key_hash"],
                    name=r["name"],
                    user_id=r["user_id"],
                    role=Role(r["role"]),
                    is_active=bool(r["is_active"]),
                    created_at=datetime.fromisoformat(r["created_at"]),
                    last_used=datetime.fromisoformat(r["last_used"]) if r["last_used"] else None,
                    expires_at=datetime.fromisoformat(r["expires_at"]) if r["expires_at"] else None,
                )
                for r in rows
            ]
        finally:
            conn.close()

    def revoke_api_key(self, key_id: str, user_id: str) -> bool:
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE api_keys SET is_active=0 WHERE key_id=? AND user_id=?",
                (key_id, user_id),
            )
            conn.commit()
            return conn.total_changes > 0
        finally:
            conn.close()

    # ── Refresh token operations ────────────────────────────────

    def store_refresh_token(self, token_hash: str, user_id: str, expires_at: datetime) -> None:
        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO refresh_tokens (token_hash, user_id, expires_at, created_at)
                   VALUES (?, ?, ?, ?)""",
                (token_hash, user_id, expires_at.isoformat(), datetime.utcnow().isoformat()),
            )
            conn.commit()
        finally:
            conn.close()

    def validate_refresh_token(self, token_hash: str) -> Optional[str]:
        """Validate refresh token, return user_id if valid."""
        conn = self._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash=?",
                (token_hash,),
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
                conn.execute("DELETE FROM refresh_tokens WHERE token_hash=?", (token_hash,))
                conn.commit()
                return None
            return row["user_id"]
        finally:
            conn.close()

    def revoke_refresh_token(self, token_hash: str) -> None:
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM refresh_tokens WHERE token_hash=?", (token_hash,))
            conn.commit()
        finally:
            conn.close()

    def revoke_all_user_tokens(self, user_id: str) -> None:
        conn = self._get_conn()
        try:
            conn.execute("DELETE FROM refresh_tokens WHERE user_id=?", (user_id,))
            conn.commit()
        finally:
            conn.close()

    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            user_id=row["user_id"],
            username=row["username"],
            email=row["email"],
            display_name=row["display_name"] or "",
            password_hash=row["password_hash"] or "",
            role=Role(row["role"]),
            is_active=bool(row["is_active"]),
            is_sso=bool(row["is_sso"]),
            sso_provider=row["sso_provider"] or "",
            sso_id=row["sso_id"] or "",
            created_at=datetime.fromisoformat(row["created_at"]),
            last_login=datetime.fromisoformat(row["last_login"]) if row["last_login"] else None,
        )
