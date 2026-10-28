"""``/admin/licence``: the licence state for everyone (banners), its details and installing it for admins."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cairn.common.licence import LicenceError, LicenceService
from cairn.common.security import Principal

MANAGE = "MANAGE_GLOBAL_SETTINGS"


class LicenceIn(BaseModel):
    key: str = Field(min_length=10, max_length=8192)


def build_router(licence: LicenceService, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/admin/licence", tags=["admin"])

    @router.get("")
    def state(p: Principal = Depends(current_principal)):
        st = licence.state()
        if authorize(p, MANAGE, None, raise_on_deny=False):
            return st
        return {k: st[k] for k in ("status", "read_only", "days_left")} | {"over_seats": st.get("over_seats", False)}

    @router.put("")
    def install(body: LicenceIn, p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        try:
            licence.install(body.key, p.urn)
        except LicenceError as e:
            raise HTTPException(422, str(e))
        return licence.state()

    @router.delete("")
    def remove(p: Principal = Depends(current_principal)):
        authorize(p, MANAGE, None)
        licence.remove(p.urn)
        return licence.state()

    return router
