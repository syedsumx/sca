"""Embedded dashboards: a portal shows a Cairn dashboard to its own users, each seeing their own data.

An operator registers each **embed client** (``CAIRN_EMBED_CLIENTS_FILE``, JSON)::

    {"portal": {"secret_file": "/run/secrets/portal-embed", "origins": ["https://portal.example.com"],
                "dashboards": ["<dashboard id>", ...] or ["*"], "users": ["urn:li:corpuser:portal-*", ...]}}

The portal's **server** signs a short token for one of its users and one dashboard (HMAC-SHA256 with the client's
secret, at least 32 bytes)::

    claims = {"client": "portal", "sub": "urn:li:corpuser:portal-ana", "dashboard": "<id>",
              "origin": "https://portal.example.com", "exp": now + 300, "jti": "<unique>"}
    body = b64url(json.dumps(claims)); token = body + "." + b64url(hmac_sha256(secret, body))

and puts ``<iframe src="https://cairn.example.com/embed/v1/dashboards/<id>?token=...">`` in its page. Cairn checks
the signature, the client, that the user and the dashboard are ones the client may use, the origin, the expiry
(at most 10 minutes ahead) and that the ``jti`` was never used, then serves the dashboard page with a session
of its own (an hour by default, ``CAIRN_EMBED_SESSION_TTL``) bound to that user, dashboard and origin. The page
may be framed only by that origin (``frame-ancestors``).

Every tile runs **as the token's user**: their policies, masks and row filters apply. A user the client may not
sign for, an unknown or wrong origin, a reused or expired token and a dashboard the client was not given are all
refused (403). Sessions start and are refused in the audit (``embed.session_started``, ``embed.refused``).
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import html
import json
import os
import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import BigInteger, Column, String, Table, delete, insert, select

from cairn.common.db import metadata

MAX_AHEAD = 600
embed_nonces = Table(
    "cairn_embed_nonces", metadata,
    Column("client", String(128), primary_key=True),
    Column("jti", String(128), primary_key=True),
    Column("exp", BigInteger, nullable=False, index=True),
)
embed_sessions = Table(
    "cairn_embed_sessions", metadata,
    Column("id_hash", String(64), primary_key=True),
    Column("client", String(128), nullable=False),
    Column("sub", String(512), nullable=False),
    Column("dashboard", String(64), nullable=False),
    Column("origin", String(512), nullable=False),
    Column("exp", BigInteger, nullable=False, index=True),
)


class EmbedRefused(Exception):
    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(secret: bytes, claims: dict) -> str:
    """What a portal's server does (here for tests and for Python portals)."""
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    return f"{body}.{_b64(hmac.new(secret, body.encode(), hashlib.sha256).digest())}"


def load_clients(path: Optional[str]) -> dict[str, dict]:
    if not path:
        return {}
    with open(path) as f:
        raw = json.load(f)
    out = {}
    for cid, c in (raw or {}).items():
        if c.get("secret_file"):
            with open(c["secret_file"], "rb") as f:
                secret = f.read().strip()
        else:
            secret = str(c.get("secret") or "").encode()
        if len(secret) < 32:
            raise RuntimeError(f"embed client {cid}: the secret needs at least 32 bytes")
        origins = [o.rstrip("/") for o in c.get("origins") or []]
        if not origins or not all(o.startswith(("https://", "http://")) for o in origins):
            raise RuntimeError(f"embed client {cid}: origins are required (https://…)")
        users = list(c.get("users") or [])
        if not users or not all(str(u).startswith("urn:li:corpuser:") for u in users):
            raise RuntimeError(f"embed client {cid}: users are urn:li:corpuser:… patterns")
        out[cid] = {"secret": secret, "origins": origins, "dashboards": list(c.get("dashboards") or []), "users": users}
    return out


class EmbeddedDashboards:
    def __init__(self, engine, boards, clients: dict[str, dict], audit=None, session_ttl: Optional[int] = None):
        self.engine, self.boards, self.clients, self.audit = engine, boards, clients, audit
        self.ttl = session_ttl or int(os.environ.get("CAIRN_EMBED_SESSION_TTL", "3600"))

    def _refuse(self, why: str, client: str = "", sub: str = "", dashboard: str = "") -> EmbedRefused:
        if self.audit is not None:
            self.audit.record("security", "embed.refused", sub or "anonymous", f"dashboard:{dashboard}" if dashboard else None,
                              {"client": client[:128], "why": why})
        return EmbedRefused(why)

    def open(self, token: str, dashboard: str) -> dict:
        """Checks a portal's token and starts a session: ``{session, sub, origin, exp}``."""
        try:
            body, sig = token.split(".")
            claims = json.loads(_unb64(body))
            if not isinstance(claims, dict):
                raise ValueError
        except (ValueError, TypeError):
            raise self._refuse("not an embed token")
        cid = str(claims.get("client") or "")
        client = self.clients.get(cid)
        if client is None:
            raise self._refuse("unknown embed client", cid)
        if not hmac.compare_digest(_b64(hmac.new(client["secret"], body.encode(), hashlib.sha256).digest()), sig):
            raise self._refuse("bad signature", cid)
        sub, did = str(claims.get("sub") or ""), str(claims.get("dashboard") or "")
        origin, jti = str(claims.get("origin") or "").rstrip("/"), str(claims.get("jti") or "")
        now = int(time.time())
        exp = claims.get("exp")
        if not isinstance(exp, int) or exp < now:
            raise self._refuse("the token has expired", cid, sub, did)
        if exp > now + MAX_AHEAD:
            raise self._refuse(f"embed tokens live at most {MAX_AHEAD} seconds", cid, sub, did)
        if did != dashboard:
            raise self._refuse("the token is for another dashboard", cid, sub, did)
        if "*" not in client["dashboards"] and did not in client["dashboards"]:
            raise self._refuse("this client may not embed that dashboard", cid, sub, did)
        if not sub.startswith("urn:li:corpuser:") or not any(fnmatch.fnmatchcase(sub, p) for p in client["users"]):
            raise self._refuse("this client may not sign for that user", cid, sub, did)
        if origin not in client["origins"]:
            raise self._refuse("the token's origin is not one of the client's", cid, sub, did)
        if not 8 <= len(jti) <= 128:
            raise self._refuse("jti: a unique id for this token (8 to 128 characters)", cid, sub, did)
        from sqlalchemy.exc import IntegrityError

        try:
            with self.engine.begin() as conn:
                conn.execute(delete(embed_nonces).where(embed_nonces.c.exp < now - MAX_AHEAD))
                conn.execute(insert(embed_nonces).values(client=cid, jti=jti, exp=exp))
        except IntegrityError:
            raise self._refuse("this token was used already", cid, sub, did)
        self.boards._granted(did)                                  # it exists
        session = secrets.token_urlsafe(32)
        with self.engine.begin() as conn:
            conn.execute(delete(embed_sessions).where(embed_sessions.c.exp < now))
            conn.execute(insert(embed_sessions).values(id_hash=hashlib.sha256(session.encode()).hexdigest(), client=cid,
                                                       sub=sub, dashboard=did, origin=origin, exp=now + self.ttl))
        if self.audit is not None:
            self.audit.record("security", "embed.session_started", sub, f"dashboard:{did}", {"client": cid, "origin": origin})
        return {"session": session, "sub": sub, "origin": origin, "exp": now + self.ttl}

    def session(self, session: str, dashboard: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(embed_sessions).where(
                embed_sessions.c.id_hash == hashlib.sha256((session or "").encode()).hexdigest())).first()
        if r is None or r.exp < int(time.time()) or r.dashboard != dashboard:
            raise EmbedRefused("this embed session is not valid (expired, or for another dashboard)", 401)
        client = self.clients.get(r.client)
        if client is None:
            raise EmbedRefused("this embed client was removed", 401)
        # What the operator allows now, not when the session started.
        if "*" not in client["dashboards"] and dashboard not in client["dashboards"]:
            raise EmbedRefused("this client may no longer embed that dashboard", 403)
        if not any(fnmatch.fnmatchcase(r.sub, p) for p in client["users"]) or r.origin not in client["origins"]:
            raise EmbedRefused("this client may no longer show it to that user or on that site", 403)
        return dict(r._mapping)

    def render(self, session: str, dashboard: str, state: Optional[dict]) -> dict:
        s = self.session(session, dashboard)
        return self.boards.render(s["sub"], dashboard, state, granted=True)

    def values(self, session: str, dashboard: str, cid: str) -> list:
        s = self.session(session, dashboard)
        return self.boards.control_values(s["sub"], dashboard, cid, granted=True)


PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style nonce="{nonce}">
body{{font:14px system-ui,sans-serif;margin:12px;color:#1f2328;background:#fff}}
.tile{{border:1px solid #d0d7de;border-radius:8px;padding:10px;margin:0 0 12px}} h1{{font-size:18px}} h2{{font-size:15px;margin:0 0 8px}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #eaeef2;padding:4px 6px;text-align:left}}
.err{{color:#cf222e}}
</style></head><body><h1>{title}</h1><div id="tiles">Loading…</div>
<script id="cairn-embed" type="application/json">{boot}</script>
<script nonce="{nonce}">
(function () {{
  var boot = JSON.parse(document.getElementById("cairn-embed").textContent);
  function el(tag, text) {{ var e = document.createElement(tag); if (text !== undefined) e.textContent = text; return e; }}
  fetch(boot.render, {{method: "POST", headers: {{"Content-Type": "application/json", "Authorization": "Embed " + boot.session}},
                       body: "{{}}"}})
    .then(function (r) {{ return r.json().then(function (j) {{ return [r.status, j]; }}); }})
    .then(function (sj) {{
      var root = document.getElementById("tiles"); root.textContent = "";
      if (sj[0] !== 200) {{ root.appendChild(el("p", sj[1].detail || "refused")).className = "err"; return; }}
      sj[1].tiles.forEach(function (t, i) {{
        var box = el("div"); box.className = "tile"; box.setAttribute("data-tile", i);
        box.appendChild(el("h2", t.title || ""));
        if (t.error) {{ var p = el("p", t.error); p.className = "err"; box.appendChild(p); root.appendChild(box); return; }}
        var res = t.result || {{}}, table = el("table"), head = el("tr");
        (res.columns || []).forEach(function (c) {{ head.appendChild(el("th", c)); }}); table.appendChild(head);
        (res.rows || []).forEach(function (row) {{
          var tr = el("tr"); row.forEach(function (v) {{ tr.appendChild(el("td", v === null ? "" : String(v))); }});
          table.appendChild(tr);
        }});
        box.appendChild(table); root.appendChild(box);
      }});
    }});
}})();
</script></body></html>"""


def build_embedded_dashboards_router(ed: EmbeddedDashboards) -> Any:
    from starlette.concurrency import run_in_threadpool

    from cairn.sqlworkspace.workspace import WorkspaceError

    router = APIRouter(prefix="/embed/v1/dashboards", tags=["embed"])

    def session_of(request: Request) -> str:
        auth = request.headers.get("authorization") or ""
        if not auth.startswith("Embed "):
            raise HTTPException(401, "an embed session is needed (Authorization: Embed <session>)")
        return auth[6:].strip()

    @router.get("/{did}")
    async def page(did: str, token: str = ""):
        try:
            s = await run_in_threadpool(lambda: ed.open(token, did))
            board = await run_in_threadpool(lambda: ed.boards._granted(did))
        except EmbedRefused as e:
            return HTMLResponse("<!doctype html><title>Refused</title><p>This dashboard cannot be shown here.</p>",
                                status_code=e.status, headers={"Content-Security-Policy": "frame-ancestors 'none'"})
        except WorkspaceError as e:
            return HTMLResponse("<!doctype html><p>No such dashboard.</p>", status_code=e.status)
        boot = json.dumps({"session": s["session"], "render": f"/embed/v1/dashboards/{did}/render"}).replace("<", "\\u003c")
        nonce = secrets.token_urlsafe(16)
        body = PAGE.format(title=html.escape(board["name"]), boot=boot, nonce=nonce)
        return HTMLResponse(body, headers={
            "Content-Security-Policy": f"frame-ancestors {s['origin']}; default-src 'none'; script-src 'nonce-{nonce}'; "
                                       f"style-src 'nonce-{nonce}'; connect-src 'self'; base-uri 'none'",
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @router.post("/{did}/render")
    async def render(did: str, request: Request, body: dict = Body(default={})):
        session = session_of(request)
        try:
            return JSONResponse(await run_in_threadpool(lambda: ed.render(session, did, body)),
                                headers={"Cache-Control": "no-store"})
        except EmbedRefused as e:
            raise HTTPException(e.status, str(e))
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/{did}/controls/{cid}/values")
    async def values(did: str, cid: str, request: Request):
        session = session_of(request)
        try:
            return await run_in_threadpool(lambda: ed.values(session, did, cid))
        except EmbedRefused as e:
            raise HTTPException(e.status, str(e))
        except WorkspaceError as e:
            raise HTTPException(e.status, str(e))

    return router
