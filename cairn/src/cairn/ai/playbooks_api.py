"""``/governance/playbooks``: turn the built-in playbooks on or off, see their runs, run one by hand."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cairn.ai.playbooks import PlaybookError, PlaybookService
from cairn.common.security import Principal

MANAGE = "MANAGE_GLOBAL_SETTINGS"


class EnabledIn(BaseModel):
    enabled: bool


class RunIn(BaseModel):
    urn: str = Field(pattern=r"^urn:li:")
    details: dict = Field(default_factory=dict)       # e.g. {"removed": [...]}, {"request_id": ...}


def build_router(svc: PlaybookService, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/governance/playbooks", tags=["playbooks"])

    def _admin(p: Principal) -> None:
        authorize(p, MANAGE, None)

    def _wrap(fn):
        try:
            return fn()
        except PlaybookError as e:
            raise HTTPException(e.status, str(e))

    @router.get("")
    def list_playbooks(p: Principal = Depends(current_principal)):
        _admin(p)
        return svc.list()

    @router.get("/runs")
    def list_runs(playbook: Optional[str] = None, limit: int = 50, p: Principal = Depends(current_principal)):
        _admin(p)
        return svc.runs(playbook, min(max(limit, 1), 200))

    @router.post("/{pid}/enabled")
    def set_enabled(pid: str, body: EnabledIn, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.set_enabled(pid, body.enabled, p.urn))

    @router.post("/{pid}/run")
    def run(pid: str, body: RunIn, p: Principal = Depends(current_principal)):
        """Run a playbook now on one asset, as if its trigger had fired (to try it out)."""
        _admin(p)
        from cairn.ai.playbooks import CATALOG

        if pid not in CATALOG:
            raise HTTPException(404, "no such playbook")
        return _wrap(lambda: svc.run(pid, body.urn, {"type": CATALOG[pid]["trigger"], "urn": body.urn,
                                                    "details": body.details}))

    return router
