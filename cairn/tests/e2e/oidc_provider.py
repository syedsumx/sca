"""Minimal OpenID Connect provider for the SSO end-to-end test (authorization code flow + PKCE).

    OIDC_ISSUER=http://cairn-oidc:8891 OIDC_CLIENT_ID=cairn OIDC_CLIENT_SECRET=s3cret \
    OIDC_USERS='{"dana": {"name": "Dana", "email": "dana@example.com", "groups": ["analysts"]}}' \
    python oidc_provider.py

The sign-in page is a form with a username field; any configured user signs in without a password.
Every token request is recorded and served at /__requests for the test to inspect.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = os.environ.get("OIDC_ISSUER", "http://localhost:8891").rstrip("/")
CLIENT_ID = os.environ.get("OIDC_CLIENT_ID", "cairn")
CLIENT_SECRET = os.environ.get("OIDC_CLIENT_SECRET", "s3cret")
USERS = json.loads(os.environ.get("OIDC_USERS", "{}"))
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
KID = "e2e"
CODES: dict[str, dict] = {}
TOKENS: dict[str, str] = {}
REQUESTS: list[dict] = []


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwks() -> dict:
    pub = KEY.public_key().public_numbers()
    return {"keys": [{"kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": _b64(pub.n), "e": _b64(pub.e)}]}


def discovery() -> dict:
    return {"issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
            "userinfo_endpoint": f"{ISSUER}/userinfo", "jwks_uri": f"{ISSUER}/jwks",
            "response_types_supported": ["code"], "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"], "scopes_supported": ["openid", "email", "profile"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "code_challenge_methods_supported": ["S256", "plain"], "grant_types_supported": ["authorization_code"],
            "claims_supported": ["sub", "preferred_username", "email", "name", "groups"]}


def claims(username: str) -> dict:
    u = USERS[username]
    return {"sub": username, "preferred_username": username, "email": u.get("email"), "name": u.get("name"),
            "given_name": u.get("name", username).split()[0], "groups": u.get("groups", [])}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body, ctype: str = "application/json", headers: dict | None = None):
        data = json.dumps(body).encode() if not isinstance(body, (bytes, str)) else (
            body.encode() if isinstance(body, str) else body)
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _form(self) -> dict:
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/.well-known/openid-configuration":
            return self._send(200, discovery())
        if url.path == "/jwks":
            return self._send(200, jwks())
        if url.path == "/__requests":
            return self._send(200, REQUESTS)
        if url.path == "/userinfo":
            user = TOKENS.get((self.headers.get("Authorization") or "").removeprefix("Bearer "))
            return self._send(200, claims(user)) if user else self._send(401, {"error": "invalid_token"})
        if url.path == "/authorize":
            if q.get("client_id") != CLIENT_ID:
                return self._send(400, {"error": "unauthorized_client"})
            hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
                             for k, v in q.items())
            return self._send(200, f"""<!doctype html><title>Example IdP</title><h1>Example IdP</h1>
<form method="post" action="/authorize">{hidden}<label>Username <input name="username" id="username"></label>
<button type="submit" id="signin">Sign in</button></form>""", "text/html")
        return self._send(404, {"error": "not_found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        form = self._form()
        if url.path == "/authorize":
            if form.get("username") not in USERS:
                return self._send(403, "unknown user", "text/plain")
            code = secrets.token_urlsafe(16)
            CODES[code] = {**form, "issued": time.time()}
            back = form["redirect_uri"] + ("&" if "?" in form["redirect_uri"] else "?") + urllib.parse.urlencode(
                {"code": code, "state": form.get("state", "")})
            return self._send(302, b"", headers={"Location": back})
        if url.path == "/token":
            cid, secret = form.get("client_id"), form.get("client_secret")
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                cid, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
                cid, secret = urllib.parse.unquote(cid), urllib.parse.unquote(secret)
            grant = CODES.pop(form.get("code", ""), None)
            REQUESTS.append({"path": "/token", "client_id": cid, "pkce": bool(form.get("code_verifier")),
                             "ok": bool(grant)})
            if cid != CLIENT_ID or secret != CLIENT_SECRET:
                return self._send(401, {"error": "invalid_client"})
            if not grant or time.time() - grant["issued"] > 60 or grant.get("redirect_uri") != form.get("redirect_uri"):
                return self._send(400, {"error": "invalid_grant"})
            challenge = grant.get("code_challenge")
            if challenge:
                verifier = form.get("code_verifier", "")
                if grant.get("code_challenge_method", "plain") == "S256":
                    verifier = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                if verifier != challenge:
                    return self._send(400, {"error": "invalid_grant", "error_description": "PKCE"})
            user = grant["username"]
            now = int(time.time())
            id_token = jwt.encode({**claims(user), "iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300,
                                   **({"nonce": grant["nonce"]} if grant.get("nonce") else {})},
                                  KEY, algorithm="RS256", headers={"kid": KID})
            access = secrets.token_urlsafe(24)
            TOKENS[access] = user
            return self._send(200, {"access_token": access, "token_type": "Bearer", "expires_in": 300,
                                    "id_token": id_token, "scope": grant.get("scope", "openid")})
        return self._send(404, {"error": "not_found"})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(urllib.parse.urlparse(ISSUER).port or 8891)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
