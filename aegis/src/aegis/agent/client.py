"""Agent-side gateway client: enrolment, signed calls, encrypted uploads."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from aegis import __version__
from aegis.agent import crypto

IDENTITY_FILE = "agent-identity.json"


class AgentState:
    """Identity + enrolment metadata persisted (0600) in the agent state directory."""

    def __init__(self, state_dir: str):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, IDENTITY_FILE)
        self.identity: Optional[crypto.Identity] = None
        self.meta: dict[str, Any] = {}
        if os.path.exists(self.path):
            self.identity, self.meta = crypto.Identity.load(self.path)

    @property
    def enrolled(self) -> bool:
        return self.identity is not None and bool(self.meta.get("agent_id"))

    @property
    def agent_id(self) -> str:
        return self.meta["agent_id"]

    @property
    def gateway_public(self) -> str:
        return self.meta["gateway_encryption_public"]

    def save(self) -> None:
        assert self.identity is not None
        self.identity.save(self.path, self.meta)


class GatewayClient:
    def __init__(self, gateway_url: str, state: AgentState, timeout: float = 60.0,
                 http: Optional[httpx.Client] = None, verify: Any = True):
        self.base = gateway_url.rstrip("/")
        self.base_path = urlparse(self.base).path.rstrip("/")
        self.state = state
        self.http = http or httpx.Client(timeout=timeout, verify=verify)
        self._seq_lock = threading.Lock()

    # ---- enrolment -------------------------------------------------------------------
    def enroll(self, token: str, hostname: Optional[str] = None) -> dict:
        identity = crypto.Identity.generate()
        resp = self.http.post(f"{self.base}/agents/enroll", json={
            "token": token, "signing_public": identity.signing_public,
            "encryption_public": identity.encryption_public,
            "hostname": hostname or socket.gethostname(), "version": __version__,
        })
        _raise(resp)
        body = resp.json()
        self.state.identity = identity
        self.state.meta = {"agent_id": body["agent_id"], "pool": body["pool"],
                           "gateway_encryption_public": body["gateway_encryption_public"],
                           "gateway_signing_public": body["gateway_signing_public"], "gateway_url": self.base}
        self.state.save()
        return body

    # ---- signed calls ----------------------------------------------------------------
    def _signed_post(self, rel_path: str, payload: dict) -> Any:
        assert self.state.identity is not None
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = crypto.sign_request(self.state.identity, self.state.agent_id, "POST", rel_path, body)
        headers["Content-Type"] = "application/json"
        resp = self.http.post(f"{self.base}{rel_path}", content=body, headers=headers)
        _raise(resp)
        return resp.json() if resp.content else None

    def heartbeat(self, running_task_ids: list[str]) -> dict:
        return self._signed_post(f"/agents/{self.state.agent_id}/heartbeat",
                                 {"running_task_ids": running_task_ids, "version": __version__})

    def poll(self) -> Optional[dict]:
        out = self._signed_post(f"/agents/{self.state.agent_id}/tasks/poll", {})
        if not out or not out.get("task_id"):
            return None
        assert self.state.identity is not None
        plaintext = crypto.open_sealed(self.state.identity.encryption_key, out["envelope"],
                                       crypto.aad_for(self.state.agent_id, out["task_id"]))
        return json.loads(plaintext)

    def upload_batch(self, run_id: str, seq: int, mcps: list[dict]) -> dict:
        envelope = crypto.seal(self.state.gateway_public, json.dumps(mcps).encode(),
                               crypto.aad_for(self.state.agent_id, run_id, str(seq)))
        return self._signed_post(f"/agents/{self.state.agent_id}/runs/{run_id}/batches",
                                 {"seq": seq, "envelope": envelope})

    def upload_logs(self, run_id: str, seq: int, lines: list[str]) -> None:
        envelope = crypto.seal(self.state.gateway_public, json.dumps(lines).encode(),
                               crypto.aad_for(self.state.agent_id, run_id, f"log-{seq}"))
        self._signed_post(f"/agents/{self.state.agent_id}/runs/{run_id}/logs", {"seq": seq, "envelope": envelope})

    def complete(self, run_id: str, status: str, report: dict, final_seq: int) -> dict:
        return self._signed_post(f"/agents/{self.state.agent_id}/runs/{run_id}/complete",
                                 {"status": status, "report": report, "final_seq": final_seq})


class GatewayHTTPError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"gateway returned {status}: {message}")
        self.status = status


def _raise(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise GatewayHTTPError(resp.status_code, str(detail)[:500])


def with_backoff(fn, attempts: int = 6, base: float = 1.0, cap: float = 60.0, retry_on=(httpx.HTTPError,)):
    """Retry transient transport errors and 5xx with capped exponential backoff."""
    delay = base
    for i in range(attempts):
        try:
            return fn()
        except GatewayHTTPError as e:
            if e.status < 500 or i == attempts - 1:
                raise
        except retry_on:
            if i == attempts - 1:
                raise
        time.sleep(delay)
        delay = min(cap, delay * 2)
    return None  # pragma: no cover
