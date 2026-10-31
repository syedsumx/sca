"""``/storage``: Cairn Store administration for Cairn admins (bucket usage and settings, quotas, the
store's audit trail). Calls go to the store's admin API with the control plane's admin key."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from cairn.common.security import Principal
from cairn.storage.store_admin import StoreAdmin, StoreAdminError


class QuotaIn(BaseModel):
    max_bytes: Optional[int] = Field(default=None, ge=0)
    max_objects: Optional[int] = Field(default=None, ge=0)


class KeyIn(BaseModel):
    """A store access key's scope. The store generates its secret."""

    model_config = {"extra": "forbid"}
    access_key: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9._-]{3,64}$")
    buckets: Optional[list[str]] = None
    read_only: Optional[bool] = None
    admin: Optional[bool] = None
    policy: Optional[dict[str, Any]] = None
    actor: Optional[str] = None
    ssh_keys: Optional[list[str]] = None
    description: Optional[str] = Field(default=None, max_length=256)


def build_router(store: StoreAdmin, current_principal, is_admin, audit=None) -> APIRouter:
    router = APIRouter(prefix="/storage", tags=["storage"])

    def _admin(p: Principal) -> None:
        if not is_admin(p.urn):
            raise HTTPException(403, "storage administration is for Cairn admins")

    def _call(fn):
        try:
            return fn()
        except StoreAdminError as e:
            raise HTTPException(e.status if e.status < 500 else 502, str(e)) from e

    @router.get("/v1/stats")
    def stats(p: Principal = Depends(current_principal)):
        _admin(p)
        return _call(store.stats)

    @router.get("/v1/buckets")
    def buckets(p: Principal = Depends(current_principal)):
        _admin(p)
        return _call(store.buckets)

    @router.put("/v1/buckets/{bucket}/quota")
    def set_quota(bucket: str, body: QuotaIn, p: Principal = Depends(current_principal)):
        _admin(p)
        if body.max_bytes is None and body.max_objects is None:
            raise HTTPException(400, "give max_bytes, max_objects or both")
        out = _call(lambda: store.set_quota(bucket, body.max_bytes, body.max_objects))
        if audit is not None:
            audit.record("storage", "quota.set", p.urn, bucket, body.model_dump(exclude_none=True))
        return out

    @router.delete("/v1/buckets/{bucket}/quota")
    def delete_quota(bucket: str, p: Principal = Depends(current_principal)):
        _admin(p)
        out = _call(lambda: store.delete_quota(bucket))
        if audit is not None:
            audit.record("storage", "quota.delete", p.urn, bucket, {})
        return out

    @router.get("/v1/keys")
    def keys(p: Principal = Depends(current_principal)):
        _admin(p)
        return _call(store.keys)

    @router.post("/v1/keys", status_code=201)
    def create_key(body: KeyIn, p: Principal = Depends(current_principal)):
        _admin(p)
        out = _call(lambda: store.create_key(body.model_dump(exclude_none=True)))
        if audit is not None:
            audit.record("storage", "key.create", p.urn, out["access_key"],
                         {k: v for k, v in body.model_dump(exclude_none=True).items() if k != "ssh_keys"})
        return out

    @router.patch("/v1/keys/{access_key}")
    def update_key(access_key: str, body: KeyIn, p: Principal = Depends(current_principal)):
        _admin(p)
        spec = body.model_dump(exclude_none=True)
        spec.pop("access_key", None)
        out = _call(lambda: store.update_key(access_key, spec))
        if audit is not None:
            audit.record("storage", "key.update", p.urn, access_key, {k: v for k, v in spec.items() if k != "ssh_keys"})
        return out

    @router.post("/v1/keys/{access_key}/rotate")
    def rotate_key(access_key: str, p: Principal = Depends(current_principal)):
        _admin(p)
        out = _call(lambda: store.rotate_key(access_key))
        if audit is not None:
            audit.record("storage", "key.rotate", p.urn, access_key, {})
        return out

    @router.delete("/v1/keys/{access_key}", status_code=204)
    def delete_key(access_key: str, p: Principal = Depends(current_principal)):
        _admin(p)
        _call(lambda: store.delete_key(access_key))
        if audit is not None:
            audit.record("storage", "key.delete", p.urn, access_key, {})

    @router.get("/v1/audit")
    def audit_entries(after: int = 0, limit: int = 200, p: Principal = Depends(current_principal)):
        _admin(p)
        return _call(lambda: store.audit(after=after, limit=min(limit, 1000)))

    @router.get("/v1/audit/verify")
    def verify(p: Principal = Depends(current_principal)):
        _admin(p)
        return _call(store.verify_audit)

    return router
