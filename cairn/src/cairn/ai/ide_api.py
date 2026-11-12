"""``GET /v1/ai/ide-config`` (L156): a client's MCP configuration for this Cairn, exactly what ``cairn ide init`` writes
(cairn.ai.ide.config_for), for Connect your IDE in the UI. Any signed-in person may ask: it holds no credential (each
client signs in with OAuth; the Copilot coding agent with a secret it names)."""

from __future__ import annotations

from typing import Optional

from fastapi import Request  # module level: with postponed annotations a local import becomes a query parameter

from cairn.ai.ide import CLIENTS, DEFAULT_TOOLSETS, IdeError, config_for


def build_router(current_principal):
    from fastapi import APIRouter, Depends, HTTPException, Query

    from cairn.ai.mcp_api import TOOLSETS

    router = APIRouter(tags=["ai"])

    @router.get("/v1/ai/ide-config")
    def ide_config(request: Request, p=Depends(current_principal), client: str = "vscode", toolsets: Optional[str] = None,
                   servers: Optional[str] = None, base: Optional[str] = Query(None, max_length=512)) -> dict:
        sets = None if toolsets is None else [t for t in toolsets.split(",") if t]
        srv = [s for s in (servers or "").split(",") if s]
        try:
            cfg = config_for(base or str(request.base_url), client, sets, srv)
        except IdeError as e:
            raise HTTPException(422, str(e))
        return {**cfg, "clients": list(CLIENTS), "toolsets": list(TOOLSETS), "default_toolsets": list(DEFAULT_TOOLSETS)}

    return router
