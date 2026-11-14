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

from cairn import __version__
from cairn.agent import crypto

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

    def _other(self, key: str) -> Optional[crypto.Identity]:
        d = self.meta.get(key)
        return crypto.Identity.from_dict(d) if isinstance(d, dict) else None

    @property
    def openers(self) -> list:
        """Every key a task may have been sealed to: the current one, one being rotated to, and the one before it
        (a poll answered while the keys changed)."""
        return [i for i in (self.identity, self._other("next_identity"), self._other("previous_identity")) if i]

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
                 http: Optional[httpx.Client] = None, verify: Any = True, cert: Any = None):
        """``cert``: the agent's client certificate and key (``(cert_file, key_file)``) for a gateway that asks for
        one (mTLS)."""
        self.base = gateway_url.rstrip("/")
        self.base_path = urlparse(self.base).path.rstrip("/")
        self.state = state
        self.http = http or httpx.Client(timeout=timeout, verify=tls_context(verify, cert))
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
        self.state.meta = {"agent_id": body["agent_id"], "pool": body["pool"], "enrolled_at": int(time.time()),
                           "gateway_encryption_public": body["gateway_encryption_public"],
                           "gateway_signing_public": body["gateway_signing_public"], "gateway_url": self.base}
        self.state.save()
        return body

    # ---- key rotation ----------------------------------------------------------------
    def rotate_keys(self) -> dict:
        """New keys, without downtime: the new identity is written down first (so a crash cannot lose it), the
        gateway is asked in a request signed with the current key and proven with the new one, then the new keys
        become current and the old ones are kept a while to open what was sealed to them."""
        from cairn.agent.gateway import rotation_proof_bytes

        assert self.state.identity is not None
        with self._seq_lock:
            new = crypto.Identity.generate()
            self.state.meta["next_identity"] = new.to_dict()
            self.state.save()
            proof = crypto.sign_bytes(new, rotation_proof_bytes(self.state.agent_id, new.signing_public,
                                                                new.encryption_public))
            out = self._signed_post(f"/agents/{self.state.agent_id}/keys/rotate", {
                "signing_public": new.signing_public, "encryption_public": new.encryption_public, "proof": proof})
            old = self.state.identity
            self.state.identity = new
            self.state.meta.pop("next_identity", None)
            self.state.meta["previous_identity"] = old.to_dict()
            self.state.meta["keys_rotated_at"] = int(time.time())
            self.state.save()
        return out

    def finish_rotation(self) -> Optional[str]:
        """After a crash during a rotation: the new keys were written down but the answer never came. If the gateway
        took them they sign now, so they become current; otherwise they are dropped. Returns what happened."""
        nxt = self.state._other("next_identity")
        if nxt is None:
            return None
        old = self.state.identity
        self.state.identity = nxt
        try:
            self._signed_post(f"/agents/{self.state.agent_id}/heartbeat", {"running_task_ids": [], "version": __version__})
        except GatewayHTTPError as e:
            if e.status != 401:
                self.state.identity = old
                raise
            self.state.identity = old                       # the gateway never took them
            self.state.meta.pop("next_identity", None)
            self.state.save()
            return "dropped"
        self.state.meta.pop("next_identity", None)
        self.state.meta["previous_identity"] = old.to_dict() if old else None
        self.state.meta["keys_rotated_at"] = int(time.time())
        self.state.save()
        return "completed"

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
        aad = crypto.aad_for(self.state.agent_id, out["task_id"])
        for i, ident in enumerate(self.state.openers):
            try:
                return json.loads(crypto.open_sealed(ident.encryption_key, out["envelope"], aad))
            except crypto.CryptoError:
                if i == len(self.state.openers) - 1:
                    raise
        raise crypto.CryptoError("no key to open the task with")

    def seal_batch(self, run_id: str, seq: int, mcps: list[dict]) -> dict:
        """Encrypt a batch for the gateway (bound to this agent, run and sequence number)."""
        return crypto.seal(self.state.gateway_public, json.dumps(mcps).encode(),
                           crypto.aad_for(self.state.agent_id, run_id, str(seq)))

    def send_batch(self, run_id: str, seq: int, envelope: dict) -> dict:
        return self._signed_post(f"/agents/{self.state.agent_id}/runs/{run_id}/batches",
                                 {"seq": seq, "envelope": envelope})

    def upload_batch(self, run_id: str, seq: int, mcps: list[dict]) -> dict:
        return self.send_batch(run_id, seq, self.seal_batch(run_id, seq, mcps))

    def upload_logs(self, run_id: str, seq: int, lines: list[str]) -> None:
        envelope = crypto.seal(self.state.gateway_public, json.dumps(lines).encode(),
                               crypto.aad_for(self.state.agent_id, run_id, f"log-{seq}"))
        self._signed_post(f"/agents/{self.state.agent_id}/runs/{run_id}/logs", {"seq": seq, "envelope": envelope})

    def complete(self, run_id: str, status: str, report: dict, final_seq: int) -> dict:
        return self._signed_post(f"/agents/{self.state.agent_id}/runs/{run_id}/complete",
                                 {"status": status, "report": report, "final_seq": final_seq})


def tls_context(verify: Any = True, cert: Any = None) -> Any:
    """What httpx takes as ``verify``: an SSL context holding the CA to trust (a file, the system's, or none) and the
    agent's client certificate. Built here because httpx drops a separate ``cert`` when ``verify`` is a file path."""
    import ssl

    if cert is None and not isinstance(verify, str):
        return verify
    if verify is False:
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    else:
        ctx = ssl.create_default_context(cafile=verify if isinstance(verify, str) else None)
    if cert is not None:
        ctx.load_cert_chain(*cert) if isinstance(cert, (tuple, list)) else ctx.load_cert_chain(cert)
    return ctx


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


def is_transient(e: BaseException) -> bool:
    """Worth retrying later: the gateway is unreachable or failing, not refusing."""
    return isinstance(e, httpx.HTTPError) or (isinstance(e, GatewayHTTPError) and e.status >= 500)


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
