"""Caller identity for Cairn HTTP APIs.

Three credential types are accepted, in order:

1. ``Authorization: Bearer <CAIRN_SERVICE_TOKEN>``: service-to-service (Java authorizer
   plugin, schedulers). Compared in constant time.
2. ``Authorization: Bearer <Cairn Core PAT>``: validated by asking Cairn Core ``me`` *with that
   token*, so Cairn Core remains the source of truth for token validity and revocation.
3. Cairn Core UI session cookies (``PLAY_SESSION``/``actor``) when Cairn is served
   same-origin behind the edge proxy: validated by forwarding the cookies to the
   frontend's GraphQL ``me`` query.

Successful validations are cached for 60 s keyed by a SHA-256 of the credential.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx

SERVICE_PRINCIPAL = "urn:li:corpuser:__cairn_service"

_ME_QUERY = "query { me { corpUser { urn username } } }"


@dataclass(frozen=True)
class Principal:
    urn: str
    via: str  # service | token | session | dev
    groups: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_service(self) -> bool:
        return self.via == "service"


class AuthenticationError(Exception):
    pass


class IdentityResolver:
    def __init__(
        self,
        core_url: str,
        frontend_url: Optional[str],
        service_token: Optional[str],
        dev_trust_actor_header: bool = False,
        ttl_seconds: int = 60,
        http: Optional[httpx.Client] = None,
        me_lookup: Optional[Callable[[str, dict, dict], Optional[str]]] = None,
    ):
        self.core_url = core_url.rstrip("/")
        self.frontend_url = frontend_url.rstrip("/") if frontend_url else None
        self.service_token = service_token
        self.dev_trust_actor_header = dev_trust_actor_header
        self.ttl = ttl_seconds
        self._http = http or httpx.Client(timeout=10.0)
        self._me_lookup = me_lookup or self._graphql_me
        self._cache: dict[str, tuple[float, Principal]] = {}
        self._lock = threading.Lock()

    def resolve(self, headers: dict[str, str], cookies: dict[str, str]) -> Principal:
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if self.service_token and hmac.compare_digest(token, self.service_token):
                return Principal(SERVICE_PRINCIPAL, "service")
            return self._cached(
                "t:" + token,
                lambda: self._lookup_or_fail(
                    f"{self.core_url}/api/graphql", {"Authorization": f"Bearer {token}"}, {}
                ),
                "token",
            )
        if cookies and self.frontend_url and ("PLAY_SESSION" in cookies or "actor" in cookies):
            cookie_key = "c:" + "&".join(f"{k}={v}" for k, v in sorted(cookies.items()))
            return self._cached(
                cookie_key,
                lambda: self._lookup_or_fail(
                    f"{self.frontend_url}/api/v2/graphql", {}, cookies
                ),
                "session",
            )
        if self.dev_trust_actor_header:
            actor = headers.get("x-cairn-actor") or headers.get("X-Cairn-Actor")
            if actor:
                return Principal(actor, "dev")
        raise AuthenticationError("Missing or invalid credentials")

    # ---- internals --------------------------------------------------------------
    def _cached(self, raw_key: str, fetch: Callable[[], str], via: str) -> Principal:
        key = hashlib.sha256(raw_key.encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]
        urn = fetch()
        principal = Principal(urn, via)
        with self._lock:
            self._cache[key] = (now + self.ttl, principal)
            if len(self._cache) > 10_000:
                for k in [k for k, (exp, _) in self._cache.items() if exp <= now]:
                    del self._cache[k]
        return principal

    def _lookup_or_fail(self, url: str, headers: dict, cookies: dict) -> str:
        urn = self._me_lookup(url, headers, cookies)
        if not urn:
            raise AuthenticationError("Cairn Core rejected the credential")
        return urn

    def _graphql_me(self, url: str, headers: dict, cookies: dict) -> Optional[str]:
        try:
            resp = self._http.post(
                url,
                json={"query": _ME_QUERY},
                headers={"Content-Type": "application/json", **headers},
                cookies=cookies,
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        data = (resp.json() or {}).get("data") or {}
        return ((data.get("me") or {}).get("corpUser") or {}).get("urn")
