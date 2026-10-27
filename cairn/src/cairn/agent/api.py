"""HTTP surface of the agent gateway.

* ``/agents/...`` admin endpoints: Cairn Core-authenticated users with MANAGE_INGESTION.
* ``/agents/enroll`` + ``/agents/{id}/...`` protocol endpoints: Ed25519-signed by the agent.
  The signed path is the request path *relative to the gateway mount point*, so the
  gateway can sit behind a path-prefixing reverse proxy.
"""

from __future__ import annotations

import ipaddress
import json
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from cairn.agent.crypto import CryptoError
from cairn.agent.gateway import (
    CompleteRequest,
    EnqueueRequest,
    EnrollRequest,
    GatewayError,
    GatewayService,
    HeartbeatRequest,
    PoolSpec,
)
from cairn.common.security import Principal


def _trusted_proxies() -> list:
    raw = os.environ.get("CAIRN_GATEWAY_TRUSTED_PROXIES", "")
    return [ipaddress.ip_network(c.strip(), strict=False) for c in raw.split(",") if c.strip()]


def _client_ip(request: Request, trusted: Optional[list] = None) -> Optional[str]:
    """The agent's address. ``X-Forwarded-For`` counts only when the peer is a trusted proxy
    (``CAIRN_GATEWAY_TRUSTED_PROXIES``, CIDRs); otherwise an agent could choose what is audited."""
    peer = request.client.host if request.client else None
    trusted = _trusted_proxies() if trusted is None else trusted
    fwd = request.headers.get("x-forwarded-for")
    if fwd and peer and trusted:
        try:
            if any(ipaddress.ip_address(peer) in net for net in trusted):
                return fwd.split(",")[0].strip()
        except ValueError:
            pass
    return peer


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
        except (ValueError, KeyError, TypeError, ValidationError) as e:   # malformed payloads (JSON, fields)
            raise HTTPException(400, f"malformed request: {type(e).__name__}") from None

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
            return _wrap(lambda: service.ingest_batch(agent, run_id, *_seq_envelope(body)))

        @router.post("/{agent_id}/runs/{run_id}/logs", status_code=204)
        async def logs(agent_id: str, run_id: str, request: Request):
            agent, body = await _agent(request)
            _same(agent, agent_id)
            seq, envelope = _wrap(lambda: _seq_envelope(body))
            _wrap(lambda: service.append_logs(agent, run_id, envelope, seq))

        @router.post("/{agent_id}/runs/{run_id}/complete")
        async def complete(agent_id: str, run_id: str, request: Request):
            agent, body = await _agent(request)
            _same(agent, agent_id)
            return _wrap(lambda: service.complete(agent, run_id, CompleteRequest.model_validate_json(body)))

    return router


def _seq_envelope(body: bytes) -> tuple[int, dict]:
    payload = json.loads(body)
    seq, envelope = payload["seq"], payload["envelope"]
    if not isinstance(seq, int) or isinstance(seq, bool) or not isinstance(envelope, dict):
        raise ValueError("seq must be an integer and envelope an object")
    return seq, envelope


def _same(agent: dict, agent_id: str) -> None:
    if agent["agent_id"] != agent_id:
        raise HTTPException(403, "signature does not belong to this agent")
