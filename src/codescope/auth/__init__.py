"""Authentication and authorization module for CodeScope."""

from codescope.auth.models import User, Role, APIKey
from codescope.auth.database import AuthDatabase
from codescope.auth.tokens import TokenManager
from codescope.auth.middleware import require_auth, require_role
from codescope.auth.oauth import OAuthProvider, GitHubOAuth, GitLabOAuth, AzureEntraOAuth

__all__ = [
    "User",
    "Role",
    "APIKey",
    "AuthDatabase",
    "TokenManager",
    "require_auth",
    "require_role",
    "OAuthProvider",
    "GitHubOAuth",
    "GitLabOAuth",
    "AzureEntraOAuth",
]
