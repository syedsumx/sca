"""JWT token management."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime, timedelta
from typing import Optional

from codescope.auth.models import Role, TokenPayload, User

logger = logging.getLogger(__name__)


class TokenManager:
    """Handles JWT creation and validation using HMAC-SHA256.

    Uses a pure-Python implementation so there's no dependency on PyJWT.
    """

    def __init__(
        self,
        secret_key: Optional[str] = None,
        access_token_minutes: int = 30,
        refresh_token_days: int = 7,
    ):
        self.secret_key = secret_key or os.environ.get(
            "CODESCOPE_SECRET_KEY",
            self._generate_default_secret(),
        )
        self.access_token_minutes = access_token_minutes
        self.refresh_token_days = refresh_token_days

    @staticmethod
    def _generate_default_secret() -> str:
        """Generate or load a persistent secret key."""
        data_dir = os.path.expanduser(
            os.environ.get("CODESCOPE_DATA_DIR", "~/.codescope")
        )
        # Resolve to absolute path and ensure it's under the expected parent
        safe_dir = os.path.realpath(data_dir)
        secret_loc = os.path.join(safe_dir, ".secret_key")
        os.makedirs(safe_dir, exist_ok=True)
        try:
            with open(secret_loc) as fh:
                return fh.read().strip()
        except FileNotFoundError:
            key = secrets.token_hex(32)
            with open(secret_loc, "w") as fh:
                fh.write(key)
            os.chmod(secret_loc, 0o600)
            return key

    # ── Token creation ──────────────────────────────────────────

    def create_access_token(self, user: User) -> str:
        """Create a short-lived access token."""
        now = int(time.time())
        payload = {
            "user_id": user.user_id,
            "username": user.username,
            "role": user.role.value,
            "token_type": "access",
            "iat": now,
            "exp": now + (self.access_token_minutes * 60),
        }
        return self._encode(payload)

    def create_refresh_token(self, user: User) -> tuple[str, str, datetime]:
        """Create a long-lived refresh token.

        Returns (token_string, token_hash, expires_at).
        """
        now = int(time.time())
        expires_at = datetime.utcnow() + timedelta(days=self.refresh_token_days)
        payload = {
            "user_id": user.user_id,
            "username": user.username,
            "role": user.role.value,
            "token_type": "refresh",
            "iat": now,
            "exp": now + (self.refresh_token_days * 86400),
        }
        token = self._encode(payload)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        return token, token_hash, expires_at

    # ── Token validation ────────────────────────────────────────

    def decode_token(self, token: str) -> Optional[TokenPayload]:
        """Decode and validate a JWT token."""
        payload = self._decode(token)
        if payload is None:
            return None

        # Check expiration
        if payload.get("exp", 0) < int(time.time()):
            return None

        try:
            return TokenPayload(
                user_id=payload["user_id"],
                username=payload["username"],
                role=payload["role"],
                exp=payload["exp"],
                iat=payload["iat"],
                token_type=payload.get("token_type", "access"),
            )
        except (KeyError, ValueError):
            return None

    def validate_access_token(self, token: str) -> Optional[TokenPayload]:
        """Validate an access token specifically."""
        payload = self.decode_token(token)
        if payload is None or payload.token_type != "access":
            return None
        return payload

    def validate_refresh_token(self, token: str) -> Optional[TokenPayload]:
        """Validate a refresh token specifically."""
        payload = self.decode_token(token)
        if payload is None or payload.token_type != "refresh":
            return None
        return payload

    # ── HMAC-SHA256 JWT implementation ──────────────────────────

    def _encode(self, payload: dict) -> str:
        """Encode a payload as a JWT token."""
        header = {"alg": "HS256", "typ": "JWT"}
        h = self._b64_encode(json.dumps(header))
        p = self._b64_encode(json.dumps(payload))
        signature = self._sign(f"{h}.{p}")
        return f"{h}.{p}.{signature}"

    def _decode(self, token: str) -> Optional[dict]:
        """Decode and verify a JWT token."""
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, signature = parts

        # Verify signature
        expected_sig = self._sign(f"{header_b64}.{payload_b64}")
        if not hmac.compare_digest(signature, expected_sig):
            return None

        try:
            payload = json.loads(self._b64_decode(payload_b64))
            return payload
        except (json.JSONDecodeError, Exception):
            return None

    def _sign(self, data: str) -> str:
        """Create HMAC-SHA256 signature."""
        sig = hmac.new(
            self.secret_key.encode(),
            data.encode(),
            hashlib.sha256,
        ).digest()
        return self._b64_encode_raw(sig)

    @staticmethod
    def _b64_encode(data: str) -> str:
        return urlsafe_b64encode(data.encode()).rstrip(b"=").decode()

    @staticmethod
    def _b64_encode_raw(data: bytes) -> str:
        return urlsafe_b64encode(data).rstrip(b"=").decode()

    @staticmethod
    def _b64_decode(data: str) -> str:
        padding = 4 - len(data) % 4
        if padding != 4:
            data += "=" * padding
        return urlsafe_b64decode(data).decode()
