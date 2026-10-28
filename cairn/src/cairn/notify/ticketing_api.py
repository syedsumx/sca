"""``/notify/ticketing`` (systems, admin) and ``/notify/inbound/<system>`` (ticket webhooks, public:
authenticated by the system's shared secret or body signature)."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from cairn.common.security import Principal
from cairn.notify.ticketing import InboundError, TicketingService


def build_router(ticketing: TicketingService, current_principal, authorize) -> APIRouter:
    router = APIRouter(tags=["notify"])

    @router.get("/notify/ticketing")
    def systems(p: Principal = Depends(current_principal)):
        authorize(p, "MANAGE_GLOBAL_SETTINGS", None)
        return ticketing.overview()

    @router.post("/notify/inbound/{system_id}")
    async def inbound(system_id: str, request: Request, secret: Optional[str] = None):
        raw = await request.body()
        if len(raw) > 1 << 20:
            raise HTTPException(413, "body too large")
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise HTTPException(400, "expected a JSON body")
        try:
            return ticketing.inbound(system_id, raw, body, secret or request.headers.get("x-cairn-secret"),
                                     request.headers.get("x-hub-signature"))
        except InboundError as e:
            raise HTTPException(e.status, str(e))

    return router
