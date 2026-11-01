"""Single sign-on for Trino through Cairn: Cairn is the OAuth2 authorization server Trino's web UI and its
clients' external authentication (the CLI's ``--external-authentication``, JDBC ``externalAuthentication=true``,
Python's ``OAuth2Authentication``) sign in with.

A person opens Trino; Trino sends their browser to Cairn's ``/v1/trino/oauth/authorize``; Cairn knows them
from their Cairn session (or sends them to Cairn's sign-in first) and returns a one-time code to Trino's
callback; Trino exchanges it at ``/v1/trino/oauth/token`` for an access token, which is the same Cairn-signed
token Trino already trusts (``jwks.json``). No consent page: Trino is Cairn's own engine.

* Codes are single-use, live 2 minutes, are bound to the redirect URI and (when the client sends one) a
  PKCE challenge, and are kept hashed.
* The redirect URI must start with one of ``CAIRN_TRINO_OAUTH_REDIRECT_URIS`` (Trino's
  ``https://<host>/oauth2/callback``); the token endpoint needs Trino's client secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from sqlalchemy import BigInteger, Boolean, Column, String, Table, delete, select, update

from cairn.common.db import metadata

CLIENT_ID = "trino"
CODE_TTL_MS = 120_000

trino_oauth_codes = Table(
    "cairn_trino_oauth_codes", metadata,
    Column("code_hash", String(64), primary_key=True),
    Column("actor", String(256), nullable=False),
    Column("redirect_uri", String(1024), nullable=False),
    Column("challenge", String(128), nullable=False),
    Column("method", String(8), nullable=False),
    Column("expires_ms", BigInteger, nullable=False),
    Column("used", Boolean, nullable=False, default=False),
)


class OAuthError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.status = error, status


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


class TrinoOAuth:
    def __init__(self, engine, tokens, client_secret: str, redirect_prefixes: list[str], ttl: int = 3600):
        if len(client_secret) < 24:
            raise ValueError("CAIRN_TRINO_OAUTH_SECRET must be at least 24 characters")
        prefixes = [p.strip() for p in redirect_prefixes if p.strip()]
        if not prefixes:
            raise ValueError("CAIRN_TRINO_OAUTH_REDIRECT_URIS: list Trino's callback, e.g. https://trino.example.com/oauth2/callback")
        for p in prefixes:
            if not p.startswith("https://"):
                raise ValueError(f"CAIRN_TRINO_OAUTH_REDIRECT_URIS: {p!r} is not https (Trino signs in over TLS only)")
        self.engine, self.tokens, self.secret, self.prefixes, self.ttl = engine, tokens, client_secret, prefixes, ttl

    def redirect_ok(self, uri: str) -> bool:
        return any(uri == p or uri.startswith(p.rstrip("/") + "/") or uri.startswith(p + "?") for p in self.prefixes)

    def check(self, client_id: str, redirect_uri: str, response_type: str, method: str) -> None:
        if client_id != CLIENT_ID:
            raise OAuthError("unauthorized_client", "unknown client")
        if not self.redirect_ok(redirect_uri):
            raise OAuthError("invalid_request", "this redirect address is not allowed")
        if response_type != "code":
            raise OAuthError("unsupported_response_type", "only the authorization code flow is supported")
        if method not in ("", "S256"):
            raise OAuthError("invalid_request", "PKCE must use S256")

    def issue_code(self, actor: str, redirect_uri: str, challenge: str, method: str) -> str:
        code = secrets.token_urlsafe(32)
        now = int(time.time() * 1000)
        with self.engine.begin() as conn:
            conn.execute(delete(trino_oauth_codes).where(trino_oauth_codes.c.expires_ms < now - 3_600_000))
            conn.execute(trino_oauth_codes.insert().values(
                code_hash=_hash(code), actor=actor, redirect_uri=redirect_uri, challenge=challenge, method=method,
                expires_ms=now + CODE_TTL_MS, used=False))
        return code

    def exchange(self, code: str, redirect_uri: str, client_id: str, client_secret: str, verifier: str) -> dict:
        if client_id != CLIENT_ID or not hmac.compare_digest(client_secret.encode(), self.secret.encode()):
            raise OAuthError("invalid_client", "wrong client credentials", 401)
        t = trino_oauth_codes
        now = int(time.time() * 1000)
        with self.engine.begin() as conn:
            row = conn.execute(select(t).where(t.c.code_hash == _hash(code))).first()
            # One use only: the first exchange marks it, whatever the outcome.
            if row is None or conn.execute(update(t).where((t.c.code_hash == row.code_hash) & (t.c.used.is_(False)))
                                           .values(used=True)).rowcount != 1:
                raise OAuthError("invalid_grant", "unknown or used code")
        if row.expires_ms < now:
            raise OAuthError("invalid_grant", "the code has expired")
        if row.redirect_uri != redirect_uri:
            raise OAuthError("invalid_grant", "the redirect address differs from the one the code was issued for")
        if row.challenge:
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if not verifier or not hmac.compare_digest(digest, row.challenge):
                raise OAuthError("invalid_grant", "the PKCE verifier does not match")
        tok = self.tokens.mint(row.actor, self.ttl)
        return {"access_token": tok["token"], "token_type": "Bearer", "expires_in": tok["expires_in"], "scope": "trino"}
