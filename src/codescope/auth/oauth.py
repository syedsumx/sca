"""OAuth SSO providers for GitHub, GitLab, and Azure Entra ID."""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

import urllib.request
import json

logger = logging.getLogger(__name__)


@dataclass
class OAuthUserInfo:
    """User info returned by an OAuth provider."""

    provider: str
    sso_id: str
    username: str
    email: str
    display_name: str
    avatar_url: str = ""


class OAuthProvider(ABC):
    """Base class for OAuth providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g., 'github', 'gitlab')."""
        ...

    @property
    @abstractmethod
    def client_id(self) -> str: ...

    @property
    @abstractmethod
    def client_secret(self) -> str: ...

    @abstractmethod
    def get_authorize_url(self, redirect_uri: str, state: str) -> str:
        """Get the OAuth authorization URL to redirect the user to."""
        ...

    @abstractmethod
    def exchange_code(self, code: str, redirect_uri: str) -> Optional[str]:
        """Exchange authorization code for access token."""
        ...

    @abstractmethod
    def get_user_info(self, access_token: str) -> Optional[OAuthUserInfo]:
        """Fetch user info using the access token."""
        ...

    def _http_post(self, url: str, data: dict, headers: Optional[dict] = None) -> Optional[dict]:
        """Make an HTTP POST request."""
        req_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            req_headers.update(headers)
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode(),
                headers=req_headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:
            logger.error("OAuth HTTP POST failed (%s): %s", url, exc)
            return None

    def _http_get(self, url: str, headers: Optional[dict] = None) -> Optional[dict]:
        """Make an HTTP GET request."""
        req_headers = {"Accept": "application/json"}
        if headers:
            req_headers.update(headers)
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:
            logger.error("OAuth HTTP GET failed (%s): %s", url, exc)
            return None


class GitHubOAuth(OAuthProvider):
    """GitHub OAuth 2.0 provider."""

    @property
    def name(self) -> str:
        return "github"

    @property
    def client_id(self) -> str:
        return os.environ.get("CODESCOPE_GITHUB_CLIENT_ID", "")

    @property
    def client_secret(self) -> str:
        return os.environ.get("CODESCOPE_GITHUB_CLIENT_SECRET", "")

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def get_authorize_url(self, redirect_uri: str, state: str) -> str:
        params = urlencode({
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": "read:user user:email",
            "state": state,
        })
        return f"https://github.com/login/oauth/authorize?{params}"

    def exchange_code(self, code: str, redirect_uri: str) -> Optional[str]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }
        resp = self._http_post("https://github.com/login/oauth/access_token", data)
        if resp and "access_token" in resp:
            return resp["access_token"]
        return None

    def get_user_info(self, access_token: str) -> Optional[OAuthUserInfo]:
        headers = {"Authorization": f"Bearer {access_token}"}
        user_data = self._http_get("https://api.github.com/user", headers)
        if not user_data:
            return None

        # Get primary email if not public
        email = user_data.get("email", "")
        if not email:
            emails = self._http_get("https://api.github.com/user/emails", headers)
            if emails:
                for e in emails:
                    if isinstance(e, dict) and e.get("primary"):
                        email = e.get("email", "")
                        break

        return OAuthUserInfo(
            provider="github",
            sso_id=str(user_data.get("id", "")),
            username=user_data.get("login", ""),
            email=email,
            display_name=user_data.get("name", "") or user_data.get("login", ""),
            avatar_url=user_data.get("avatar_url", ""),
        )


class GitLabOAuth(OAuthProvider):
    """GitLab OAuth 2.0 provider."""

    @property
    def name(self) -> str:
        return "gitlab"

    @property
    def client_id(self) -> str:
        return os.environ.get("CODESCOPE_GITLAB_CLIENT_ID", "")

    @property
    def client_secret(self) -> str:
        return os.environ.get("CODESCOPE_GITLAB_CLIENT_SECRET", "")

    @property
    def base_url(self) -> str:
        return os.environ.get("CODESCOPE_GITLAB_URL", "https://gitlab.com")

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def get_authorize_url(self, redirect_uri: str, state: str) -> str:
        params = urlencode({
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "read_user",
            "state": state,
        })
        return f"{self.base_url}/oauth/authorize?{params}"

    def exchange_code(self, code: str, redirect_uri: str) -> Optional[str]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        resp = self._http_post(f"{self.base_url}/oauth/token", data)
        if resp and "access_token" in resp:
            return resp["access_token"]
        return None

    def get_user_info(self, access_token: str) -> Optional[OAuthUserInfo]:
        headers = {"Authorization": f"Bearer {access_token}"}
        user_data = self._http_get(f"{self.base_url}/api/v4/user", headers)
        if not user_data:
            return None

        return OAuthUserInfo(
            provider="gitlab",
            sso_id=str(user_data.get("id", "")),
            username=user_data.get("username", ""),
            email=user_data.get("email", ""),
            display_name=user_data.get("name", "") or user_data.get("username", ""),
            avatar_url=user_data.get("avatar_url", ""),
        )


class AzureEntraOAuth(OAuthProvider):
    """Azure Entra ID (formerly Azure AD) OAuth 2.0 provider."""

    @property
    def name(self) -> str:
        return "azure"

    @property
    def client_id(self) -> str:
        return os.environ.get("CODESCOPE_AZURE_CLIENT_ID", "")

    @property
    def client_secret(self) -> str:
        return os.environ.get("CODESCOPE_AZURE_CLIENT_SECRET", "")

    @property
    def tenant_id(self) -> str:
        return os.environ.get("CODESCOPE_AZURE_TENANT_ID", "common")

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def _authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    def get_authorize_url(self, redirect_uri: str, state: str) -> str:
        params = urlencode({
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid profile email User.Read",
            "state": state,
            "response_mode": "query",
        })
        return f"{self._authority}/oauth2/v2.0/authorize?{params}"

    def exchange_code(self, code: str, redirect_uri: str) -> Optional[str]:
        # Azure token endpoint expects form-encoded POST, not JSON
        form_data = urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "scope": "openid profile email User.Read",
        }).encode()
        try:
            req = urllib.request.Request(
                f"{self._authority}/oauth2/v2.0/token",
                data=form_data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
                return data.get("access_token")
        except Exception as exc:
            logger.error("Azure Entra token exchange failed: %s", exc)
            return None

    def get_user_info(self, access_token: str) -> Optional[OAuthUserInfo]:
        headers = {"Authorization": f"Bearer {access_token}"}
        user_data = self._http_get("https://graph.microsoft.com/v1.0/me", headers)
        if not user_data:
            return None

        # Azure may return 'id', 'displayName', 'mail', 'userPrincipalName'
        email = user_data.get("mail", "") or user_data.get("userPrincipalName", "")
        username = email.split("@")[0] if email else user_data.get("id", "")

        return OAuthUserInfo(
            provider="azure",
            sso_id=str(user_data.get("id", "")),
            username=username,
            email=email,
            display_name=user_data.get("displayName", "") or username,
        )


# Provider registry
_PROVIDERS: dict[str, OAuthProvider] = {
    "github": GitHubOAuth(),
    "gitlab": GitLabOAuth(),
    "azure": AzureEntraOAuth(),
}


def get_oauth_provider(name: str) -> Optional[OAuthProvider]:
    return _PROVIDERS.get(name)


def get_configured_providers() -> list[OAuthProvider]:
    return [p for p in _PROVIDERS.values() if p.is_configured]
