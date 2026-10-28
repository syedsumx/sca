"""``/governance/tag-sync``: push catalog classifications to warehouse tags."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cairn.common.security import Principal
from cairn.governance.tag_sync import TagSyncError, TagSyncService

MANAGE = "MANAGE_GLOBAL_SETTINGS"


class SyncRequest(BaseModel):
    dry_run: bool = True
    urns: Optional[list[str]] = None


def build_router(tag_sync: TagSyncService, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/governance/tag-sync", tags=["tag-sync"])

    def _wrap(fn):
        try:
            return fn()
        except TagSyncError as e:
            raise HTTPException(e.status, str(e))
        except ValueError as e:      # a target file that does not validate
            raise HTTPException(400, f"tag-sync configuration: {e}")

    @router.get("")
    def targets(p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        return _wrap(tag_sync.overview)

    @router.post("/{target_id}/sync")
    def sync(target_id: str, body: SyncRequest, p: Principal = Depends(current_principal)):
        """Dry run by default: returns the plan (SET/UNSET per table or column, with its SQL)."""
        authorize(p, MANAGE, None)
        return _wrap(lambda: tag_sync.sync(target_id, p.urn, dry_run=body.dry_run, urns=body.urns))

    return router
