"""FastAPI authentication middleware and dependency injection."""

from __future__ import annotations

import logging
from functools import wraps
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from codescope.auth.database import AuthDatabase
from codescope.auth.models import Role, User
from codescope.auth.tokens import TokenManager

logger = logging.getLogger(__name__)

# Shared instances (initialized in app startup)
_auth_db: Optional[AuthDatabase] = None
_token_mgr: Optional[TokenManager] = None

security = HTTPBearer(auto_error=False)


def init_auth(auth_db: AuthDatabase, token_mgr: TokenManager) -> None:
    """Initialize auth middleware with shared instances."""
    global _auth_db, _token_mgr
    _auth_db = auth_db
    _token_mgr = token_mgr


def _get_auth_db() -> AuthDatabase:
    if _auth_db is None:
        raise HTTPException(status_code=500, detail="Auth not initialized")
    return _auth_db


def _get_token_mgr() -> TokenManager:
    if _token_mgr is None:
        raise HTTPException(status_code=500, detail="Auth not initialized")
    return _token_mgr


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:
    """Extract and validate the current user from the request.

    Supports:
    - Bearer token (JWT) in Authorization header
    - API key in X-API-Key header
    - API key as Bearer token with 'cs_' prefix
    """
    auth_db = _get_auth_db()
    token_mgr = _get_token_mgr()

    # Try API key from X-API-Key header
    api_key_header = request.headers.get("X-API-Key")
    if api_key_header:
        result = auth_db.validate_api_key(api_key_header)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired API key",
            )
        api_key, user = result
        # Override user role with API key's role (may be more restrictive)
        user.role = api_key.role
        return user

    # Try Bearer token
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    # Check if it's an API key (starts with cs_)
    if token.startswith("cs_"):
        result = auth_db.validate_api_key(token)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired API key",
            )
        api_key, user = result
        user.role = api_key.role
        return user

    # Validate as JWT
    payload = token_mgr.validate_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = auth_db.get_user_by_id(payload.user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated",
        )

    return user


async def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[User]:
    """Like get_current_user but returns None instead of raising 401."""
    try:
        return await get_current_user(request, credentials)
    except HTTPException:
        return None


def require_auth(user: User = Depends(get_current_user)) -> User:
    """Dependency that requires authentication."""
    return user


def require_role(required_role: Role):
    """Dependency factory that requires a specific role or higher.

    Usage:
        @router.get("/admin", dependencies=[Depends(require_role(Role.ADMIN))])
        async def admin_endpoint(): ...
    """
    async def _check_role(user: User = Depends(get_current_user)) -> User:
        if not user.can(required_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Requires {required_role.value} role or higher",
            )
        return user

    return _check_role
