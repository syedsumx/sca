"""HTTP surface of the agent gateway.

* ``/agents/...`` admin endpoints: DataHub-authenticated users with MANAGE_INGESTION.
* ``/agents/enroll`` + ``/agents/{id}/...`` protocol endpoints: Ed25519-signed by the agent.
  The signed path is the request path *relative to the gateway mount point*, so the
  gateway can sit behind a path-prefixing reverse proxy.
"""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from datahub_enterprise.agent.crypto import CryptoError
from datahub_enterprise.agent.gateway import (
    CompleteRequest,
    EnqueueRequest,
    EnrollRequest,
    GatewayError,
    GatewayService,
    HeartbeatRequest,
    PoolSpec,
)
from datahub_enterprise.common.security import Principal


def _client_ip(request: Request) -> Optional[str]:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _relative_path(request: Request) -> str:
    root = request.scope.get("root_path", "") or ""
    path = request.url.path
    return path[len(root):] if root and path.startswith(root) else path


def build_router(service: GatewayService, current_principal=None, authorize=None, admin: bool = True,
                 protocol: bool = True) -> APIRouter:
    router = APIRouter(prefix="/agents", tags=["agents"])

    def _wrap(fn):
        try:
            return fn()
        except GatewayError as e:
            raise HTTPException(e.status, str(e))
        except CryptoError as e:
            raise HTTPException(400, str(e))

    async def _agent(request: Request) -> tuple[dict, bytes]:
        body = await request.body()
        agent = _wrap(lambda: service.authenticate(request.method, _relative_path(request),
                                                   dict(request.headers), body))
        return agent, body

    if admin and current_principal is not None:
        @router.get("")
        def list_agents(p: Principal = Depends(current_principal)):
            authorize(p, "MANAGE_INGESTION", None)
            return service.list_agents()

        @router.get("/pools")
        def list_pools(p: Principal = Depends(current_principal)):
            authorize(p, "MANAGE_INGESTION", None)
            return service.list_pools()

        @router.post("/pools", status_code=201)
        def create_pool(spec: PoolSpec, p: Principal = Depends(current_principal)):
            authorize(p, "MANAGE_INGESTION", None)
            return _wrap(lambda: service.create_pool(spec, p.urn))

        @router.post("/pools/{pool}/enrollment-tokens", status_code=201)
        def create_token(pool: str, ttl_seconds: int = 3600, p: Principal = Depends(current_principal)):
            authorize(p, "MANAGE_INGESTION", None)
            return _wrap(lambda: service.create_enrollment_token(pool, p.urn, min(ttl_seconds, 86_400)))

        @router.get("/runs")
        def list_runs(limit: int = 200, p: Principal = Depends(current_principal)):
            authorize(p, "MANAGE_INGESTION", None)
            return service.list_runs(min(limit, 1000))

        @router.post("/tasks", status_code=201)
        def enqueue(req: EnqueueRequest, p: Principal = Depends(current_principal)):
            authorize(p, "MANAGE_INGESTION", None)
            return _wrap(lambda: service.enqueue(req, p.urn))

        @router.post("/tasks/{task_id}/cancel", status_code=204)
        def cancel(task_id: str, p: Principal = Depends(current_principal)):
            authorize(p, "MANAGE_INGESTION", None)
            service.cancel(task_id, p.urn)

        @router.post("/{agent_id}/revoke", status_code=204)
        def revoke(agent_id: str, p: Principal = Depends(current_principal)):
            authorize(p, "MANAGE_INGESTION", None)
            _wrap(lambda: service.revoke_agent(agent_id, p.urn))

    if protocol:
        @router.post("/enroll")
        def enroll(req: EnrollRequest, request: Request):
            return _wrap(lambda: service.enroll(req, _client_ip(request)))

        @router.post("/{agent_id}/heartbeat")
        async def heartbeat(agent_id: str, request: Request):
            agent, body = await _agent(request)
            _same(agent, agent_id)
            return _wrap(lambda: service.heartbeat(agent, HeartbeatRequest.model_validate_json(body or b"{}"),
                                                   _client_ip(request)))

        @router.post("/{agent_id}/tasks/poll")
        async def poll(agent_id: str, request: Request):
            agent, _ = await _agent(request)
            _same(agent, agent_id)
            task = _wrap(lambda: service.poll(agent))
            return task or {"task_id": None}

        @router.post("/{agent_id}/runs/{run_id}/batches")
        async def batch(agent_id: str, run_id: str, request: Request):
            agent, body = await _agent(request)
            _same(agent, agent_id)
            payload = json.loads(body)
            return _wrap(lambda: service.ingest_batch(agent, run_id, int(payload["seq"]), payload["envelope"]))

        @router.post("/{agent_id}/runs/{run_id}/logs", status_code=204)
        async def logs(agent_id: str, run_id: str, request: Request):
            agent, body = await _agent(request)
            _same(agent, agent_id)
            payload = json.loads(body)
            _wrap(lambda: service.append_logs(agent, run_id, payload["envelope"], int(payload["seq"])))

        @router.post("/{agent_id}/runs/{run_id}/complete")
        async def complete(agent_id: str, run_id: str, request: Request):
            agent, body = await _agent(request)
            _same(agent, agent_id)
            return _wrap(lambda: service.complete(agent, run_id, CompleteRequest.model_validate_json(body)))

    return router


def _same(agent: dict, agent_id: str) -> None:
    if agent["agent_id"] != agent_id:
        raise HTTPException(403, "signature does not belong to this agent")
