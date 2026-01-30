"""Authentication and authorization API routes."""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from codescope.auth.database import AuthDatabase
from codescope.auth.middleware import (
    get_current_user,
    require_role,
    _get_auth_db,
    _get_token_mgr,
)
from codescope.auth.models import Role, User
from codescope.auth.oauth import get_configured_providers, get_oauth_provider
from codescope.auth.tokens import TokenManager

router = APIRouter()
logger = logging.getLogger(__name__)

# Store OAuth state tokens in memory (short-lived)
_oauth_states: dict[str, str] = {}


# ── Request / Response schemas ──────────────────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=50)
    email: str
    password: str = Field(min_length=8)
    display_name: str = ""


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class UserResponse(BaseModel):
    user_id: str
    username: str
    email: str
    display_name: str
    role: str
    is_sso: bool
    sso_provider: str
    created_at: str
    last_login: Optional[str]


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


class CreateAPIKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    role: str = "ci"
    expires_days: Optional[int] = None


class APIKeyResponse(BaseModel):
    key_id: str
    name: str
    role: str
    created_at: str
    expires_at: Optional[str]
    last_used: Optional[str]


class APIKeyCreatedResponse(APIKeyResponse):
    raw_key: str  # Only returned on creation


class UpdateUserRoleRequest(BaseModel):
    role: str


class OAuthProviderInfo(BaseModel):
    name: str
    authorize_url: str


# ── Helpers ─────────────────────────────────────────────────────

def _user_response(user: User) -> UserResponse:
    return UserResponse(
        user_id=user.user_id,
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        role=user.role.value,
        is_sso=user.is_sso,
        sso_provider=user.sso_provider,
        created_at=user.created_at.isoformat(),
        last_login=user.last_login.isoformat() if user.last_login else None,
    )


def _create_token_response(user: User, token_mgr: TokenManager, auth_db: AuthDatabase) -> TokenResponse:
    access_token = token_mgr.create_access_token(user)
    refresh_token, refresh_hash, expires_at = token_mgr.create_refresh_token(user)
    auth_db.store_refresh_token(refresh_hash, user.user_id, expires_at)

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=token_mgr.access_token_minutes * 60,
        user=_user_response(user),
    )


# ── Auth endpoints ──────────────────────────────────────────────

@router.post("/auth/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    """Authenticate with username and password."""
    auth_db = _get_auth_db()
    token_mgr = _get_token_mgr()

    user = auth_db.authenticate(request.username, request.password)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )

    return _create_token_response(user, token_mgr, auth_db)


@router.post("/auth/register", response_model=TokenResponse)
async def register(request: RegisterRequest):
    """Register a new user account."""
    auth_db = _get_auth_db()
    token_mgr = _get_token_mgr()

    # Check if username or email already exists
    if auth_db.get_user_by_username(request.username):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username already taken",
        )

    try:
        user = auth_db.create_user(
            username=request.username,
            email=request.email,
            password=request.password,
            display_name=request.display_name or request.username,
            role=Role.VIEWER,  # New users start as viewers
        )
    except Exception as exc:
        logger.error("Registration failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration failed. Email may already be in use.",
        )

    return _create_token_response(user, token_mgr, auth_db)


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh_token(request: RefreshRequest):
    """Exchange a refresh token for new access + refresh tokens."""
    auth_db = _get_auth_db()
    token_mgr = _get_token_mgr()

    # Validate the refresh token JWT
    payload = token_mgr.validate_refresh_token(request.refresh_token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token",
        )

    # Check it's in the database
    token_hash = hashlib.sha256(request.refresh_token.encode()).hexdigest()
    user_id = auth_db.validate_refresh_token(token_hash)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has been revoked",
        )

    # Revoke old refresh token (rotation)
    auth_db.revoke_refresh_token(token_hash)

    user = auth_db.get_user_by_id(user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or deactivated",
        )

    return _create_token_response(user, token_mgr, auth_db)


@router.post("/auth/logout")
async def logout(user: User = Depends(get_current_user)):
    """Revoke all refresh tokens for the current user."""
    auth_db = _get_auth_db()
    auth_db.revoke_all_user_tokens(user.user_id)
    return {"message": "Logged out successfully"}


@router.get("/auth/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)):
    """Get the current authenticated user's info."""
    return _user_response(user)


@router.put("/auth/password")
async def change_password(
    request: ChangePasswordRequest,
    user: User = Depends(get_current_user),
):
    """Change the current user's password."""
    if user.is_sso:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SSO users cannot change passwords",
        )

    auth_db = _get_auth_db()

    # Verify current password
    if auth_db.authenticate(user.username, request.current_password) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )

    auth_db.change_password(user.user_id, request.new_password)
    # Revoke all tokens to force re-login
    auth_db.revoke_all_user_tokens(user.user_id)

    return {"message": "Password changed. Please log in again."}


# ── OAuth SSO endpoints ─────────────────────────────────────────

@router.get("/auth/sso/providers")
async def list_sso_providers(request: Request):
    """List configured SSO providers with authorize URLs."""
    providers = get_configured_providers()
    base_url = str(request.base_url).rstrip("/")
    result = []
    for p in providers:
        state = secrets.token_urlsafe(32)
        _oauth_states[state] = p.name
        redirect_uri = f"{base_url}/api/v1/auth/sso/{p.name}/callback"
        result.append(OAuthProviderInfo(
            name=p.name,
            authorize_url=p.get_authorize_url(redirect_uri, state),
        ))
    return result


@router.get("/auth/sso/{provider}/callback")
async def sso_callback(provider: str, code: str, state: str, request: Request):
    """OAuth callback handler. Exchanges code for tokens and creates/updates user."""
    # Validate state
    expected_provider = _oauth_states.pop(state, None)
    if expected_provider is None or expected_provider != provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state",
        )

    oauth = get_oauth_provider(provider)
    if oauth is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {provider}")

    base_url = str(request.base_url).rstrip("/")
    redirect_uri = f"{base_url}/api/v1/auth/sso/{provider}/callback"

    # Exchange code for access token
    access_token = oauth.exchange_code(code, redirect_uri)
    if access_token is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to exchange authorization code",
        )

    # Get user info from provider
    user_info = oauth.get_user_info(access_token)
    if user_info is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to get user info from provider",
        )

    # Create or update SSO user
    auth_db = _get_auth_db()
    token_mgr = _get_token_mgr()

    user = auth_db.create_sso_user(
        username=user_info.username,
        email=user_info.email,
        provider=user_info.provider,
        sso_id=user_info.sso_id,
        display_name=user_info.display_name,
    )

    token_response = _create_token_response(user, token_mgr, auth_db)

    # Return HTML that posts tokens to the frontend
    html = f"""
    <!DOCTYPE html>
    <html>
    <body>
        <script>
            window.opener.postMessage({{
                type: 'sso_callback',
                access_token: '{token_response.access_token}',
                refresh_token: '{token_response.refresh_token}',
                user: {token_response.user.model_dump_json() if hasattr(token_response.user, 'model_dump_json') else '{}'}
            }}, window.location.origin);
            window.close();
        </script>
        <p>Authentication successful. This window should close automatically.</p>
    </body>
    </html>
    """
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html)


# ── API Key endpoints ───────────────────────────────────────────

@router.post("/auth/api-keys", response_model=APIKeyCreatedResponse)
async def create_api_key(
    request: CreateAPIKeyRequest,
    user: User = Depends(get_current_user),
):
    """Create a new API key for the authenticated user."""
    try:
        role = Role(request.role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role: {request.role}. Must be one of: admin, analyst, viewer, ci",
        )

    # Users can't create keys with higher roles than their own
    if not user.can(role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot create API key with role '{role.value}' — exceeds your permissions",
        )

    auth_db = _get_auth_db()
    raw_key, api_key = auth_db.create_api_key(
        user_id=user.user_id,
        name=request.name,
        role=role,
        expires_days=request.expires_days,
    )

    return APIKeyCreatedResponse(
        key_id=api_key.key_id,
        name=api_key.name,
        role=api_key.role.value,
        created_at=api_key.created_at.isoformat(),
        expires_at=api_key.expires_at.isoformat() if api_key.expires_at else None,
        last_used=None,
        raw_key=raw_key,
    )


@router.get("/auth/api-keys", response_model=list[APIKeyResponse])
async def list_api_keys(user: User = Depends(get_current_user)):
    """List all API keys for the authenticated user."""
    auth_db = _get_auth_db()
    keys = auth_db.list_api_keys(user.user_id)
    return [
        APIKeyResponse(
            key_id=k.key_id,
            name=k.name,
            role=k.role.value,
            created_at=k.created_at.isoformat(),
            expires_at=k.expires_at.isoformat() if k.expires_at else None,
            last_used=k.last_used.isoformat() if k.last_used else None,
        )
        for k in keys
        if k.is_active
    ]


@router.delete("/auth/api-keys/{key_id}")
async def revoke_api_key(key_id: str, user: User = Depends(get_current_user)):
    """Revoke an API key."""
    auth_db = _get_auth_db()
    if not auth_db.revoke_api_key(key_id, user.user_id):
        raise HTTPException(status_code=404, detail="API key not found")
    return {"message": "API key revoked"}


# ── Admin endpoints ─────────────────────────────────────────────

@router.get(
    "/auth/users",
    response_model=list[UserResponse],
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def list_users():
    """List all users (admin only)."""
    auth_db = _get_auth_db()
    users = auth_db.list_users()
    return [_user_response(u) for u in users]


@router.put(
    "/auth/users/{user_id}/role",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def update_user_role(user_id: str, request: UpdateUserRoleRequest):
    """Update a user's role (admin only)."""
    try:
        role = Role(request.role)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role: {request.role}",
        )

    auth_db = _get_auth_db()
    if not auth_db.update_user_role(user_id, role):
        raise HTTPException(status_code=404, detail="User not found")
    return {"message": f"User role updated to {role.value}"}


@router.delete(
    "/auth/users/{user_id}",
    dependencies=[Depends(require_role(Role.ADMIN))],
)
async def deactivate_user(user_id: str, user: User = Depends(get_current_user)):
    """Deactivate a user (admin only)."""
    if user_id == user.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account",
        )
    auth_db = _get_auth_db()
    if not auth_db.deactivate_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    # Revoke all their tokens
    auth_db.revoke_all_user_tokens(user_id)
    return {"message": "User deactivated"}
