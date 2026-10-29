"""HTTP for AI agents: OAuth 2.1 for MCP clients, agent administration, and a person's connected agents.

Discovery follows the MCP authorization spec: the MCP endpoint answers 401 with
``WWW-Authenticate: Bearer resource_metadata=…``; ``/.well-known/oauth-protected-resource/…``
names this authorization server, whose metadata is at ``/.well-known/oauth-authorization-server/…``
(and under the issuer). The issuer is ``<public url>/cairn/api/oauth``.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import time
from typing import Optional
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from cairn.common.security import AuthenticationError, Principal
from cairn.identity.agents import (
    SCOPES, AgentError, AgentIn, AgentPatch, AgentService, ClientRegistration, describe,
)

MANAGE = "MANAGE_GLOBAL_SETTINGS"
CONSENT_TTL_S = 600


def _oauth_error(e: AgentError) -> JSONResponse:
    return JSONResponse({"error": e.oauth_error, "error_description": str(e)}, status_code=e.status,
                        headers={"Cache-Control": "no-store"})


def _with_query(uri: str, params: dict) -> str:
    parts = urlsplit(uri)
    q = parts.query + ("&" if parts.query else "") + urlencode({k: v for k, v in params.items() if v is not None})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, q, ""))


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · Cairn</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#1d2330;--muted:#5d6676;--line:#dde1e7;--accent:#2f5bd3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--card:#1b2029;--ink:#e8ebf0;--muted:#9aa3b2;--line:#2c3340;--accent:#7b9cf0}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}}
main{{max-width:460px;margin:8vh auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:28px}}
h1{{font-size:20px;margin:0 0 6px}} p{{color:var(--muted);margin:0 0 16px}}
label{{display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-top:1px solid var(--line)}}
.row{{display:flex;gap:10px;margin-top:20px}} button{{flex:1;padding:10px;border-radius:8px;font-size:15px;
border:1px solid var(--line);background:transparent;color:var(--ink);cursor:pointer}}
button.primary{{background:var(--accent);border-color:var(--accent);color:#fff}} code{{font-size:13px}}
</style></head><body><main><div class="card">{body}</div></main></body></html>"""
    return HTMLResponse(doc, status_code=status, headers={
        "Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"})


async def _form(request: Request) -> dict[str, list[str]]:
    raw = (await request.body()).decode("utf-8", "replace")
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return {}
        return {k: v if isinstance(v, list) else [str(v)] for k, v in data.items()}
    return parse_qs(raw, keep_blank_values=True)


def build_router(svc: AgentService, services, current_principal, authorize) -> APIRouter:
    router = APIRouter(tags=["agents"])
    secret = (services.settings.service_token or "cairn-dev") + "|oauth-consent"

    def public() -> str:
        return services.settings.active.public_url.rstrip("/")

    def issuer() -> str:
        return f"{public()}/cairn/api/oauth"

    def metadata() -> dict:
        i = issuer()
        return {"issuer": i, "authorization_endpoint": f"{i}/authorize", "token_endpoint": f"{i}/token",
                "registration_endpoint": f"{i}/register", "revocation_endpoint": f"{i}/revoke",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"],
                "scopes_supported": list(SCOPES), "service_documentation": f"{public()}/docs/context-and-ai.html"}

    # ---- discovery ------------------------------------------------------------------------------
    @router.get("/.well-known/oauth-protected-resource{rest:path}", include_in_schema=False)
    def protected_resource(rest: str = ""):
        return {"resource": f"{public()}{rest or '/cairn/mcp/mcp'}", "authorization_servers": [issuer()],
                "scopes_supported": list(SCOPES), "bearer_methods_supported": ["header"], "resource_name": "Cairn"}

    @router.get("/.well-known/oauth-authorization-server{rest:path}", include_in_schema=False)
    def authorization_server(rest: str = ""):
        return metadata()

    @router.get("/oauth/.well-known/oauth-authorization-server", include_in_schema=False)
    def authorization_server_under_issuer():
        return metadata()

    # ---- registration, authorization, tokens ------------------------------------------------------
    @router.post("/oauth/register", status_code=201)
    def register(req: ClientRegistration):
        try:
            return svc.register_client(req)
        except AgentError as e:
            return _oauth_error(e)

    def _signed(*parts: str) -> str:
        return hmac.new(secret.encode(), "\x1f".join(parts).encode(), hashlib.sha256).hexdigest()

    def _person(request: Request) -> Optional[Principal]:
        headers = {k: v for k, v in request.headers.items() if k.lower() != "authorization"}
        try:
            p = services.identity.resolve(headers, dict(request.cookies))   # a browser session, never a token
        except AuthenticationError:
            return None
        return p if p.via in ("session", "dev") else None

    @router.get("/oauth/authorize", include_in_schema=False)
    def authorize_page(request: Request, response_type: str = "", client_id: str = "", redirect_uri: str = "",
                       code_challenge: str = "", code_challenge_method: str = "", state: Optional[str] = None,
                       scope: Optional[str] = None, resource: Optional[str] = None):
        try:
            client = svc.get(client_id)
        except AgentError:
            return _page("Unknown agent", "<h1>Unknown agent</h1><p>This sign-in link names an agent Cairn "
                         "does not know. Start again from the agent.</p>", 400)
        if redirect_uri not in client["redirect_uris"]:
            return _page("Wrong address", "<h1>This sign-in link is not valid</h1><p>The agent asked to "
                         "return to an address it did not register.</p>", 400)
        if response_type != "code":
            return RedirectResponse(_with_query(redirect_uri, {"error": "unsupported_response_type", "state": state}), 302)
        try:
            client, scopes = svc.check_authorization(client_id, redirect_uri, code_challenge, code_challenge_method, scope)
        except AgentError as e:
            return RedirectResponse(_with_query(redirect_uri, {"error": e.oauth_error, "error_description": str(e),
                                                               "state": state}), 302)
        person = _person(request)
        if person is None:
            back = f"/cairn/api/oauth/authorize?{request.url.query}"
            return RedirectResponse(f"/login?redirect_uri={quote(back, safe='')}", 302)
        ts = str(int(time.time()))
        fields = {"client_id": client_id, "redirect_uri": redirect_uri, "code_challenge": code_challenge,
                  "state": state or "", "resource": resource or "", "ts": ts}
        fields["csrf"] = _signed(person.urn, *fields.values())
        hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(v)}">' for k, v in fields.items())
        boxes = "".join(f'<label><input type="checkbox" name="scope" value="{d["scope"]}" checked> '
                        f'<span><b>{d["scope"].replace("_", " ")}</b><br>{html.escape(d["text"])}</span></label>'
                        for d in describe(scopes))
        site = urlsplit(redirect_uri).netloc or urlsplit(redirect_uri).scheme
        body = (f"<h1>Connect {html.escape(client['name'])}</h1>"
                f"<p>It will act as you, never with more access than you have, and only for what you allow "
                f"here. It returns to <code>{html.escape(site)}</code>. You can disconnect it any time in "
                f"Settings → AI agents.</p><form method=\"post\" action=\"/cairn/api/oauth/authorize\">"
                f"{hidden}{boxes}<div class=\"row\"><button name=\"decision\" value=\"deny\">Cancel</button>"
                f"<button class=\"primary\" name=\"decision\" value=\"allow\">Allow</button></div></form>")
        return _page(f"Connect {client['name']}", body)

    @router.post("/oauth/authorize", include_in_schema=False)
    async def authorize_decision(request: Request):
        form = await _form(request)
        f = {k: (form.get(k) or [""])[0] for k in
             ("client_id", "redirect_uri", "code_challenge", "state", "resource", "ts", "csrf", "decision")}
        person = _person(request)
        expected = _signed(person.urn if person else "", f["client_id"], f["redirect_uri"], f["code_challenge"],
                           f["state"], f["resource"], f["ts"])
        fresh = f["ts"].isdigit() and time.time() - int(f["ts"]) < CONSENT_TTL_S
        if person is None or not fresh or not hmac.compare_digest(expected, f["csrf"]):
            return _page("Sign-in expired", "<h1>This page expired</h1><p>Start the connection again from the "
                         "agent.</p>", 400)
        try:
            client, allowed = svc.check_authorization(f["client_id"], f["redirect_uri"], f["code_challenge"], "S256",
                                                      None)
        except AgentError as e:
            return _page("Not allowed", f"<h1>Cannot connect</h1><p>{html.escape(str(e))}</p>", 400)
        state = f["state"] or None
        if f["decision"] != "allow":
            return RedirectResponse(_with_query(f["redirect_uri"], {"error": "access_denied", "state": state}), 302)
        scopes = [s for s in allowed if s in form.get("scope", [])]
        if not scopes:
            return RedirectResponse(_with_query(f["redirect_uri"], {"error": "access_denied", "state": state}), 302)
        code = svc.issue_code(f["client_id"], person.urn, scopes, f["redirect_uri"], f["code_challenge"],
                              f["resource"] or None)
        return RedirectResponse(_with_query(f["redirect_uri"], {"code": code, "state": state, "iss": issuer()}), 302)

    @router.post("/oauth/token")
    async def token(request: Request):
        form = {k: v[0] for k, v in (await _form(request)).items() if v}
        try:
            if form.get("grant_type") == "authorization_code":
                out = svc.exchange_code(form.get("code", ""), form.get("client_id", ""), form.get("redirect_uri", ""),
                                        form.get("code_verifier", ""))
            elif form.get("grant_type") == "refresh_token":
                out = svc.refresh(form.get("refresh_token", ""), form.get("client_id", ""))
            else:
                raise AgentError("grant_type must be authorization_code or refresh_token", 400,
                                 "unsupported_grant_type")
        except AgentError as e:
            return _oauth_error(e)
        return JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    @router.post("/oauth/revoke")
    async def revoke(request: Request):
        form = await _form(request)
        svc.revoke_token((form.get("token") or [""])[0])
        return Response(status_code=200)

    # ---- administration ----------------------------------------------------------------------------
    def _admin(p: Principal) -> None:
        authorize(p, MANAGE, None)

    def _wrap(fn):
        try:
            return fn()
        except AgentError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/admin/agents")
    def list_agents(p: Principal = Depends(current_principal)):
        _admin(p)
        return svc.list()

    @router.post("/admin/agents", status_code=201)
    def create_agent(spec: AgentIn, p: Principal = Depends(current_principal)):
        """An agent for an automation; the key in the answer is shown once."""
        _admin(p)
        return _wrap(lambda: svc.create_key_agent(spec, p.urn))

    @router.patch("/admin/agents/{agent_id}")
    def update_agent(agent_id: str, patch: AgentPatch, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.update(agent_id, patch, p.urn))

    @router.post("/admin/agents/{agent_id}/rotate")
    def rotate_agent(agent_id: str, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.rotate_key(agent_id, p.urn))

    @router.delete("/admin/agents/{agent_id}", status_code=204)
    def delete_agent(agent_id: str, p: Principal = Depends(current_principal)):
        _admin(p)
        _wrap(lambda: svc.delete(agent_id, p.urn))

    # ---- a person's own connections ------------------------------------------------------------------
    @router.get("/me/agents")
    def my_agents(p: Principal = Depends(current_principal)):
        return svc.connected(p.urn)

    @router.delete("/me/agents/{agent_id}")
    def disconnect(agent_id: str, p: Principal = Depends(current_principal)):
        return {"revoked": _wrap(lambda: svc.disconnect(agent_id, p.urn))}

    return router
