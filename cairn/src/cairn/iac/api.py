"""``/v1/iac``: the resource registry (what can be kept as code), what of it this workspace holds, and any of it
exported as Terraform or a bundle, for the person asking.

Inventory and export read Cairn's own API in-process with the caller's credentials, so they show exactly what the
caller may read, through the same permission checks as every other request.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from cairn.iac import registry

FORWARD = ("authorization", "cookie", "x-cairn-actor")


class _InProcess:
    """Calls this app's own API as the caller (their credentials forwarded), from a worker thread."""

    def __init__(self, request: Request):
        self.app = request.app
        self.headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD}

    def call(self, method: str, path: str, body: Any = None) -> Any:
        import anyio
        import httpx

        async def go():
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://cairn.internal", timeout=60) as c:
                return await c.request(method, path, json=body, headers=self.headers)

        r = anyio.from_thread.run(go)
        if r.status_code >= 300:
            err = RuntimeError(f"{method} {path}: {r.status_code}")
            err.status = r.status_code          # type: ignore[attr-defined]
            raise err
        return r.json() if r.content else None


def build_iac_router(current_principal) -> APIRouter:
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/iac", tags=["iac"])

    @router.get("/schema")
    def schema(_p=Depends(current_principal)):
        """Every resource type that can be kept as code: its API, fields and Terraform type."""
        return registry.schema()

    @router.get("/inventory")
    async def inventory(request: Request, p=Depends(current_principal)):
        """How many of each resource type this workspace holds that the caller may read (null: off here)."""
        from cairn.iac.export import collect

        caller = _InProcess(request)

        def work():
            errors: list = []
            items = collect(caller.call, errors=errors)
            counts: dict[str, Optional[int]] = {k.name: 0 for k in registry.kinds()}
            for kind, _ in errors:
                counts[kind] = None
            for k, _ in items:
                counts[k.name] = (counts[k.name] or 0) + 1
            return {"kinds": counts}

        return await run_in_threadpool(work)

    @router.get("/export")
    async def export(request: Request, format: str = "terraform", kind: Optional[str] = None,  # noqa: A002
                     id: Optional[str] = None, bundle: str = "exported", p=Depends(current_principal)):  # noqa: A002
        """Resources as Terraform (resource and import blocks) or a bundle (``format=yaml``): one (``kind`` and
        ``id``, Terraform's id), one kind, or everything the caller may read. Secrets are variables, never values."""
        from cairn.iac.export import ExportError
        from cairn.iac.export import export as run

        if format not in ("terraform", "yaml"):
            raise HTTPException(422, "format: terraform or yaml")
        if id is not None and kind is None:
            raise HTTPException(422, "id needs its kind")
        if kind is not None:
            try:
                registry.kind(kind)
            except KeyError:
                raise HTTPException(404, f"no resource type {kind}")
        caller = _InProcess(request)
        try:
            text = await run_in_threadpool(lambda: run(caller.call, format, [kind] if kind else None, bundle, None, id))
        except ExportError as e:
            raise HTTPException(404, str(e))
        media = "text/x-terraform" if format == "terraform" else "application/yaml"
        return PlainTextResponse(text, media_type=f"{media}; charset=utf-8")

    return router
