"""Auth data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Role(str, Enum):
    """User roles with hierarchical permissions."""

    ADMIN = "admin"
    ANALYST = "analyst"
    VIEWER = "viewer"
    CI = "ci"

    @property
    def level(self) -> int:
        """Permission level (higher = more access)."""
        return {"admin": 100, "analyst": 50, "viewer": 20, "ci": 30}[self.value]

    def has_permission(self, required: "Role") -> bool:
        """Check if this role has at least the required permission level."""
        return self.level >= required.level


@dataclass
class User:
    """Represents an authenticated user."""

    user_id: str
    username: str
    email: str
    role: Role
    display_name: str = ""
    password_hash: str = ""
    is_active: bool = True
    is_sso: bool = False
    sso_provider: str = ""
    sso_id: str = ""
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_login: Optional[datetime] = None

    @property
    def is_admin(self) -> bool:
        return self.role == Role.ADMIN

    def can(self, required_role: Role) -> bool:
        return self.role.has_permission(required_role)


@dataclass
class APIKey:
    """API key for programmatic access."""

    key_id: str
    key_hash: str
    name: str
    user_id: str
    role: Role
    is_active: bool = True
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_used: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return datetime.utcnow() > self.expires_at


@dataclass
class TokenPayload:
    """Decoded JWT token payload."""

    user_id: str
    username: str
    role: str
    exp: int
    iat: int
    token_type: str = "access"
