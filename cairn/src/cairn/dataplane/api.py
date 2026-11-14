"""HTTP surface of split deployments.

* ``/v1/dataplanes`` and ``/v1/compute/workspaces``: operators (``MANAGE_SYSTEM_OPERATIONS``) on Cairn's API.
* ``/dataplanes/tunnel/...``: the connectors' protocol, on the gateway (``cairn gateway``: the one component exposed to
  customer networks, with its TLS and optional mTLS). Every call but enrolment is Ed25519-signed by the connector; the
  signed path is relative to the gateway's mount point.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from cairn.agent.api import _client_ip, _relative_path
from cairn.agent.crypto import CryptoError
from cairn.dataplane.service import ComputeSpec, DataPlaneError, DataPlanes, DataPlaneSpec, EnrollRequest, RotateRequest

PRIVILEGE = "MANAGE_SYSTEM_OPERATIONS"


async def _call(fn, *a):
    try:
        return await run_in_threadpool(fn, *a)
    except DataPlaneError as e:
        raise HTTPException(e.status, str(e))
    except CryptoError as e:
        raise HTTPException(400, str(e))
    except (ValidationError, ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"malformed request: {type(e).__name__}") from None


def build_dataplanes_router(planes: DataPlanes, current_principal, authorize) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["dataplanes"])

    def op(p):
        authorize(p, PRIVILEGE, None)
        return p

    @router.get("/dataplanes")
    async def list_(p=Depends(current_principal)):
        op(p)
        return await _call(planes.list)

    @router.post("/dataplanes", status_code=201)
    async def create(spec: DataPlaneSpec, p=Depends(current_principal)):
        return await _call(planes.create, spec, op(p).urn)

    @router.get("/dataplanes/{name}")
    async def get(name: str, p=Depends(current_principal)):
        op(p)
        return await _call(planes.get, name)

    @router.put("/dataplanes/{name}")
    async def put(name: str, body: dict = Body(...), p=Depends(current_principal)):
        op(p)
        try:
            spec = DataPlaneSpec(**{**body, "name": name})
        except ValidationError as e:
            raise HTTPException(422, "; ".join(str(x.get("msg")) for x in e.errors()))
        return await _call(planes.update, spec, p.urn)

    @router.delete("/dataplanes/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        await _call(planes.delete, name, op(p).urn)

    @router.post("/dataplanes/{name}/enrollment-tokens", status_code=201)
    async def token(name: str, ttl_seconds: int = 3600, p=Depends(current_principal)):
        return await _call(planes.enrollment_token, name, op(p).urn, ttl_seconds)

    @router.get("/dataplanes/{name}/connectors")
    async def connectors(name: str, p=Depends(current_principal)):
        op(p)
        await _call(planes.get, name)
        return await _call(planes.connectors, name)

    @router.post("/dataplanes/{name}/connectors/{connector_id}/revoke", status_code=204)
    async def revoke(name: str, connector_id: str, p=Depends(current_principal)):
        await _call(planes.revoke, name, connector_id, op(p).urn)

    @router.post("/dataplanes/{name}/ping")
    async def ping(name: str, p=Depends(current_principal)):
        return await _call(planes.call, name, "ping", {}, op(p).urn, 20)

    @router.post("/dataplanes/{name}/storage/presign")
    async def presign(name: str, bucket: str = Body(..., max_length=63), key: str = Body(..., max_length=1024),
                      method: str = Body("GET"), ttl_seconds: int = Body(300, ge=1, le=86_400),
                      p=Depends(current_principal)):
        return await _call(planes.presign, name, bucket, key, method, ttl_seconds, op(p).urn)

    @router.get("/compute/workspaces")
    async def computes(p=Depends(current_principal)):
        op(p)
        return {"workspaces": await _call(planes.list_compute), "managed_pool": planes.managed,
                "default": {"compute": planes.default_compute, "dataplane": planes.default_dataplane}}

    @router.get("/compute/workspaces/{workspace}")
    async def compute(workspace: str, p=Depends(current_principal)):
        op(p)
        return await _call(planes.get_compute, workspace)

    @router.put("/compute/workspaces/{workspace}")
    async def set_compute(workspace: str, spec: ComputeSpec, p=Depends(current_principal)):
        return await _call(planes.set_compute, workspace, spec, op(p).urn)

    @router.delete("/compute/workspaces/{workspace}", status_code=204)
    async def clear_compute(workspace: str, p=Depends(current_principal)):
        await _call(planes.delete_compute, workspace, op(p).urn)

    return router


def build_tunnel_router(planes: DataPlanes) -> APIRouter:
    """The connectors' side (mounted on the gateway app)."""
    router = APIRouter(prefix="/dataplanes/tunnel", tags=["dataplanes"])

    async def connector(request: Request, connector_id: str) -> tuple[dict, dict]:
        body = await request.body()
        c = await _call(planes.authenticate, request.method, _relative_path(request), dict(request.headers), body)
        if c["connector_id"] != connector_id:
            raise HTTPException(401, "signed by another connector")
        try:
            import json

            doc = json.loads(body) if body else {}
        except ValueError:
            raise HTTPException(400, "the body is not JSON")
        return c, doc if isinstance(doc, dict) else {}

    @router.post("/enroll")
    async def enroll(req: EnrollRequest, request: Request):
        return await _call(planes.enroll, req, _client_ip(request))

    @router.post("/{connector_id}/poll")
    async def poll(connector_id: str, request: Request):
        c, doc = await connector(request, connector_id)
        caps = doc.get("capabilities") if isinstance(doc.get("capabilities"), dict) else None
        wait: Optional[float] = doc.get("wait_s") if isinstance(doc.get("wait_s"), (int, float)) else None
        return await _call(planes.poll, c, _client_ip(request), caps, wait)

    @router.post("/{connector_id}/calls/{call_id}/answer")
    async def answer(connector_id: str, call_id: str, request: Request):
        c, doc = await connector(request, connector_id)
        if not isinstance(doc.get("envelope"), dict):
            raise HTTPException(400, "expected {\"envelope\": ...}")
        return await _call(planes.answer, c, call_id, doc["envelope"])

    @router.post("/{connector_id}/opa/{kind}")
    async def opa(connector_id: str, kind: str, request: Request):
        c, doc = await connector(request, connector_id)
        return await _call(planes.opa, c, kind, doc)

    @router.post("/{connector_id}/keys/rotate")
    async def rotate(connector_id: str, request: Request):
        c, doc = await connector(request, connector_id)
        try:
            req = RotateRequest(**doc)
        except ValidationError:
            raise HTTPException(400, "signing_public, encryption_public and proof")
        return await _call(planes.rotate, c, req)

    return router
