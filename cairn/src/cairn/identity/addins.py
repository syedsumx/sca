"""Spreadsheet add-ins: Cairn for Excel and Cairn for Google Sheets as OAuth clients the operator configures.

Each add-in is an OAuth 2.0 public client of Cairn's authorization server (:mod:`cairn.identity.agents_api`):
authorization code with PKCE (S256) through the person's normal sign-in and a consent page, then access tokens of
one hour and rotating refresh tokens. The tokens are the person's, narrowed to the scopes ``read``, ``query_data``
and ``model``: masks, row filters and table permissions apply as for them; administration is never reachable.

Configuration (environment of the API):

* ``CAIRN_EXCEL_ADDIN_ORIGINS``: the https origin(s) the Office add-in's files are served from (comma separated,
  e.g. ``https://addins.example.com``). The client ``agt_excel_addin`` gets the redirect URI
  ``<origin>/callback.html`` per origin, and these origins get CORS on the routes the add-in calls from the
  browser (the token endpoint and ``/data/sheets``), for bearer tokens only (no cookies).
* ``CAIRN_SHEETS_ADDON_SCRIPT_IDS``: the Apps Script project id(s) of the Google Sheets add-on. The client
  ``agt_sheets_addon`` gets ``https://script.google.com/macros/d/<id>/usercallback`` per id. Apps Script calls
  Cairn from Google's servers, so no CORS is needed.

Unset, the client is not created (or, configured before, disabled and its tokens revoked).
"""

from __future__ import annotations

import os
import re
from typing import Optional
from urllib.parse import urlsplit

EXCEL_CLIENT_ID, SHEETS_CLIENT_ID = "agt_excel_addin", "agt_sheets_addon"
ADDIN_SCOPES = ["read", "query_data", "model"]
SHEETS_ACCESS_TTL_MS = 2 * 3600_000
CORS_PATHS = ("/oauth/token", "/oauth/revoke", "/data/sheets/", "/me")
_SCRIPT_ID = re.compile(r"^[A-Za-z0-9_-]{20,120}$")


def _split(v: Optional[str]) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def excel_origins(value: Optional[str] = None) -> list[str]:
    out = []
    for o in _split(value if value is not None else os.environ.get("CAIRN_EXCEL_ADDIN_ORIGINS")):
        u = urlsplit(o)
        loopback = u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1")
        if (u.scheme != "https" and not loopback) or not u.netloc or u.path not in ("", "/") or u.query or u.fragment:
            raise ValueError(f"CAIRN_EXCEL_ADDIN_ORIGINS: {o!r} is not an https origin (scheme://host[:port])")
        out.append(f"{u.scheme}://{u.netloc}")
    return out


def sheets_script_ids(value: Optional[str] = None) -> list[str]:
    ids = _split(value if value is not None else os.environ.get("CAIRN_SHEETS_ADDON_SCRIPT_IDS"))
    bad = [i for i in ids if not _SCRIPT_ID.match(i)]
    if bad:
        raise ValueError(f"CAIRN_SHEETS_ADDON_SCRIPT_IDS: not Apps Script project ids: {bad}")
    return ids


class SpreadsheetAddins:
    def __init__(self, agents, origins: list[str], script_ids: list[str]):
        self.agents, self.origins, self.script_ids = agents, origins, script_ids

    @classmethod
    def from_env(cls, agents) -> "SpreadsheetAddins":
        return cls(agents, excel_origins(), sheets_script_ids())

    def sync(self) -> dict:
        """Create or update the two clients from the configuration."""
        excel = self.agents.ensure_client(
            EXCEL_CLIENT_ID, "Cairn for Excel", [f"{o}/callback.html" for o in self.origins], ADDIN_SCOPES,
            {"description": "The Office add-in: CAIRN.QUERY, CAIRN.METRIC and CAIRN.ASK in Excel"})
        sheets = self.agents.ensure_client(
            SHEETS_CLIENT_ID, "Cairn for Google Sheets",
            [f"https://script.google.com/macros/d/{i}/usercallback" for i in self.script_ids], ADDIN_SCOPES,
            {"description": "The Google Sheets add-on: CAIRN_QUERY, CAIRN_METRIC and CAIRN_ASK",
             # Two hours: the add-on refreshes hourly from a time-driven trigger (an add-on's trigger runs at most
             # hourly, and Sheets' custom functions cannot store a rotated refresh token themselves).
             "access_ttl_ms": SHEETS_ACCESS_TTL_MS})
        return {"excel": excel, "sheets": sheets}

    def cors_origin(self, origin: Optional[str], path: str) -> Optional[str]:
        if origin and origin in self.origins and any(path == p or (p.endswith("/") and path.startswith(p))
                                                     for p in CORS_PATHS):
            return origin
        return None


class AddinCors:
    """Pure ASGI: CORS for the Office add-in's origins on the routes it calls (bearer tokens, never cookies).
    Other origins get no CORS headers, so a browser refuses them."""

    def __init__(self, app, addins: SpreadsheetAddins):
        self.app, self.addins = app, addins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        origin = (headers.get(b"origin") or b"").decode("latin-1") or None
        allowed = self.addins.cors_origin(origin, scope["path"])
        if allowed is None:
            await self.app(scope, receive, send)
            return
        cors = [(b"access-control-allow-origin", allowed.encode()), (b"vary", b"Origin")]
        if scope["method"] == "OPTIONS" and b"access-control-request-method" in headers:
            await send({"type": "http.response.start", "status": 204, "headers": cors + [
                (b"access-control-allow-methods", b"GET, POST"),
                (b"access-control-allow-headers", b"Authorization, Content-Type"),
                (b"access-control-max-age", b"600")]})
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_with_cors(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *cors]}
            await send(message)
        await self.app(scope, receive, send_with_cors)
