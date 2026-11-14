"""``/admin/extensions`` (install, upgrade, rotate, enable, remove: admins) and ``/extensions/ui`` (the
workspace sections of enabled extensions: everyone)."""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from cairn.common.security import Principal
from cairn.extensions.service import ExtensionError, ExtensionService, Manifest

MANAGE = "MANAGE_GLOBAL_SETTINGS"


def parse_manifest(raw: Any) -> Manifest:
    """A manifest from YAML/JSON text or an already-parsed mapping."""
    try:
        doc = yaml.safe_load(raw) if isinstance(raw, (str, bytes)) else raw
        return Manifest.model_validate(doc)
    except (yaml.YAMLError, ValidationError) as e:
        raise ExtensionError(f"invalid manifest: {e}")


def build_router(svc: ExtensionService, current_principal, authorize) -> APIRouter:
    router = APIRouter(tags=["extensions"])

    def _wrap(fn):
        try:
            return fn()
        except ExtensionError as e:
            raise HTTPException(e.status, str(e))

    def _admin(p: Principal):
        if p.via == "extension":
            raise HTTPException(403, "extensions cannot manage extensions")
        authorize(p, MANAGE, None)

    @router.get("/admin/extensions")
    def list_(p: Principal = Depends(current_principal)):
        _admin(p)
        return svc.list()

    @router.put("/admin/extensions")
    async def install(request: Request, p: Principal = Depends(current_principal)):
        _admin(p)
        raw = await request.body()
        if len(raw) > 64 * 1024:
            raise HTTPException(413, "manifest too large")
        return _wrap(lambda: svc.install(parse_manifest(raw.decode()), p.urn))

    @router.post("/admin/extensions/{name}/rotate")
    def rotate(name: str, p: Principal = Depends(current_principal)):
        _admin(p)
        return _wrap(lambda: svc.rotate(name, p.urn))

    @router.post("/admin/extensions/{name}/{state}")
    def enable(name: str, state: str, p: Principal = Depends(current_principal)):
        _admin(p)
        if state not in ("enable", "disable"):
            raise HTTPException(404, "unknown action")
        return _wrap(lambda: svc.set_enabled(name, state == "enable", p.urn))

    @router.delete("/admin/extensions/{name}", status_code=204)
    def remove(name: str, p: Principal = Depends(current_principal)):
        _admin(p)
        _wrap(lambda: svc.remove(name, p.urn))

    @router.get("/extensions/ui")
    def ui(p: Principal = Depends(current_principal)):
        return svc.ui_sections()

    return router
