"""Routers: ``/notify`` (inbox, settings, routes, signed links) and ``/integrations/slack``."""

from __future__ import annotations

import html
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from datahub_enterprise.common.security import Principal
from datahub_enterprise.notify.hub import Event, NotificationHub, NotifySettings, Route
from datahub_enterprise.notify.slack import SlackAuthError, SlackInteractions


class ReadRequest(BaseModel):
    ids: list[str]


class TestRequest(BaseModel):
    channel: str = "in_app"


class ActionRequest(BaseModel):
    comment: Optional[str] = None


def build_router(hub: NotificationHub, slack: Optional[SlackInteractions], current_principal, authorize) -> APIRouter:
    router = APIRouter(tags=["notify"])

    @router.get("/notify/inbox")
    def inbox(unread: bool = False, limit: int = 50, p: Principal = Depends(current_principal)):
        return hub.inbox(p.urn, unread, min(limit, 200))

    @router.get("/notify/inbox/count")
    def inbox_count(p: Principal = Depends(current_principal)):
        return {"unread": hub.unread_count(p.urn)}

    @router.post("/notify/inbox/read")
    def read(req: ReadRequest, p: Principal = Depends(current_principal)):
        return {"updated": hub.mark_read(p.urn, req.ids)}

    @router.get("/notify/settings", response_model=NotifySettings)
    def get_settings(p: Principal = Depends(current_principal)):
        return hub.get_settings(p.urn)

    @router.put("/notify/settings", response_model=NotifySettings)
    def put_settings(settings: NotifySettings, p: Principal = Depends(current_principal)):
        return hub.put_settings(p.urn, settings)

    @router.get("/notify/routes", response_model=list[Route])
    def get_routes(p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return hub.list_routes()

    @router.put("/notify/routes", response_model=list[Route])
    def put_routes(routes: list[Route], p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return hub.put_routes(routes)

    @router.post("/notify/test")
    def test(req: TestRequest, p: Principal = Depends(current_principal)):
        ids = hub.publish(Event(type="subscription.update", title="Test notification",
                                body=f"This is a test message on channel '{req.channel}'.", recipients=[p.urn]))
        return {"queued": len(ids)}

    # ---- signed one-click links (Teams / email) --------------------------------------
    @router.get("/notify/actions/{token}", response_class=HTMLResponse)
    def confirm_page(token: str, p: Principal = Depends(current_principal)):
        try:
            claims = hub.verify_action(token)
        except PermissionError as e:
            return HTMLResponse(_page("Link not valid", str(e)), status_code=400)
        if claims["r"] != p.urn:
            return HTMLResponse(_page("Wrong account", "This link was issued to a different DataHub user."), 403)
        verb = html.escape(claims["a"].capitalize())
        what = html.escape(f"{claims['k'].replace('_', ' ')} {claims['o']}")
        form = (f"<form method='post'><textarea name='comment' placeholder='Optional comment' rows='3'></textarea>"
                f"<button type='submit'>{verb}</button></form>")
        return HTMLResponse(_page(f"{verb} {what}?", "Confirm to continue.", form))

    @router.post("/notify/actions/{token}")
    async def execute_link(token: str, request: Request, p: Principal = Depends(current_principal)):
        comment = None
        if request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            comment = (await request.form()).get("comment") or None
        try:
            message = hub.consume_action_token(token, p.urn, comment)  # type: ignore[arg-type]
        except PermissionError as e:
            return HTMLResponse(_page("Not allowed", str(e)), status_code=403)
        except (ValueError, KeyError) as e:
            return HTMLResponse(_page("Could not complete", str(e)), status_code=409)
        return HTMLResponse(_page("Done", message))

    @router.post("/notify/actions/inline/{kind}/{action}/{object_id}")
    def execute_inline(kind: str, action: str, object_id: str, body: Optional[ActionRequest] = None,
                       p: Principal = Depends(current_principal)):
        try:
            return {"message": hub.execute_action(kind, action, object_id, p.urn, body.comment if body else None)}
        except PermissionError as e:
            raise HTTPException(403, str(e))
        except (ValueError, KeyError) as e:
            raise HTTPException(409, str(e))

    # ---- Slack ------------------------------------------------------------------------
    @router.post("/integrations/slack/interactions")
    async def slack_interactions(request: Request):
        if slack is None:
            raise HTTPException(404, "Slack integration is not configured")
        body = await request.body()
        try:
            return JSONResponse(slack.handle_interaction(dict(request.headers), body))
        except SlackAuthError as e:
            raise HTTPException(401, str(e))

    @router.post("/integrations/slack/commands")
    async def slack_commands(request: Request):
        if slack is None:
            raise HTTPException(404, "Slack integration is not configured")
        body = await request.body()
        try:
            return JSONResponse(slack.handle_command(dict(request.headers), body))
        except SlackAuthError as e:
            raise HTTPException(401, str(e))

    return router


def _page(title: str, message: str, extra: str = "") -> str:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>
body{{font-family:system-ui,sans-serif;max-width:520px;margin:12vh auto;padding:0 16px;color:#1f2937}}
h1{{font-size:1.4rem}} textarea{{width:100%;margin:12px 0;font:inherit}}
button{{background:#1d4ed8;color:#fff;border:0;border-radius:8px;padding:10px 18px;font:inherit;cursor:pointer}}
</style></head><body><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>{extra}</body></html>"""
