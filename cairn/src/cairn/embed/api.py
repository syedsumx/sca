"""``/embed/v1`` router. Token endpoints are public (token-authenticated) and CORS/CSP-aware."""

from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from cairn.common.security import Principal
from cairn.embed.service import EmbedError, EmbedService, TokenRequest, origin_allowed, origin_of


def build_router(svc: EmbedService, current_principal, can_view) -> APIRouter:
    router = APIRouter(prefix="/embed/v1", tags=["embed"])

    def _headers(request: Request) -> dict:
        frame_ancestors = " ".join(svc.allowed_origins) if svc.allowed_origins else "'none'"
        h = {"Content-Security-Policy": f"frame-ancestors {frame_ancestors}", "Cache-Control": "private, max-age=60",
             "Referrer-Policy": "no-referrer"}
        origin = request.headers.get("origin")
        if origin and origin_allowed(origin, svc.allowed_origins):
            h["Access-Control-Allow-Origin"] = origin
            h["Vary"] = "Origin"
        return h

    def _claims(request: Request, token: str, urn: str, scope: str) -> dict:
        try:
            return svc.verify(token, urn, scope, request.headers.get("origin") or origin_of(request.headers.get("referer")))
        except EmbedError as e:
            raise HTTPException(e.status, str(e))

    def _json(request: Request, payload) -> JSONResponse:
        return JSONResponse(payload, headers=_headers(request))

    @router.post("/tokens", status_code=201)
    def mint(req: TokenRequest, p: Principal = Depends(current_principal)):
        urn = req.urn or (svc.lookup(req.lookup_url) if req.lookup_url else None)
        if not urn:
            raise HTTPException(422, "provide urn, or a lookup_url that resolves to exactly one asset")
        if not can_view(p)(urn):
            raise HTTPException(403, "you cannot view this asset")
        try:
            return svc.mint(p.urn, req, urn)
        except EmbedError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/tokens")
    def tokens(p: Principal = Depends(current_principal)):
        return svc.list_tokens(p.urn)

    @router.delete("/tokens/{jti}", status_code=204)
    def revoke(jti: str, p: Principal = Depends(current_principal)):
        if not svc.revoke(p.urn, jti):
            raise HTTPException(404, "token not found")

    @router.get("/assets/{urn:path}")
    def asset(urn: str, request: Request, token: str = Query(...)):
        claims = _claims(request, token, urn, "card")
        return _json(request, _wrap(lambda: svc.card(claims, urn)))

    @router.get("/quality/{urn:path}")
    def quality(urn: str, request: Request, token: str = Query(...)):
        claims = _claims(request, token, urn, "quality")
        return _json(request, _wrap(lambda: svc.quality(claims, urn)))

    @router.get("/lineage/{urn:path}")
    def lineage(urn: str, request: Request, token: str = Query(...), depth: int = Query(2, ge=1, le=3)):
        claims = _claims(request, token, urn, "lineage")
        return _json(request, _wrap(lambda: svc.lineage(claims, urn, depth)))

    @router.get("/lookup")
    def lookup(url: str, p: Principal = Depends(current_principal)):
        urn = svc.lookup(url)
        return {"urn": urn if urn and can_view(p)(urn) else None}

    @router.get("/frame/{kind}", response_class=HTMLResponse)
    def frame(kind: str, request: Request, urn: str, token: str):
        if kind not in ("card", "quality", "lineage"):
            raise HTTPException(404, "unknown embed kind")
        _claims(request, token, urn, kind)
        return HTMLResponse(svc.frame_html(kind, urn, token), headers=_headers(request))

    @router.get("/sdk.js")
    def sdk():
        return RedirectResponse(f"{svc.public_url}/cairn-ui/embed/sdk.js", status_code=302)

    return router


def _wrap(fn):
    try:
        return fn()
    except EmbedError as e:
        raise HTTPException(e.status, str(e))


__all__ = ["build_router"]
