"""Split control plane and data plane: Cairn hosted (API, UI, catalog metadata, the PDP, the gateway), compute in the
customer's cluster (Trino, Spark and jobs, flows' steps), data in the customer's storage.

A **data plane** is registered here and runs a **connector** in the customer's cluster (``helm/cairn-dataplane``,
:mod:`cairn.dataplane.connector`). The connector enrols like a remote agent (a single-use enrolment token, its own
Ed25519 signing and X25519 encryption keys, optional mTLS to the gateway, key rotation by signed re-enrolment) and then
only ever connects **out**: it long-polls the gateway for calls and posts their answers back. Nothing in the
customer's network listens for Cairn.

* **Calls through the tunnel.** The control plane reaches data-plane compute only by queueing a call (in the
  database, so any replica can) that the connector leases: a Trino HTTP request (query submission and its pages), a
  job run, a storage presign. Each request is sealed to the connector's key and each answer to the gateway's.
* **The PDP stays hosted.** The data plane's Trino asks its connector (``access-control.name=opa``), which asks the
  hosted PDP through the tunnel (``/dataplanes/tunnel/{id}/opa/...``): the same decisions, masks and row filters as
  hosted Trino. A connector that cannot reach Cairn answers 503 and Trino refuses.
* **Fail closed.** No connector seen for ``CAIRN_DATAPLANE_OFFLINE_SECONDS`` (30): the tunnel is down and queries and
  jobs on that data plane are refused with that message. A connector that was revoked, or never enrolled, is refused
  (401) on every call, OPA questions included.
* **Scoped credentials.** Storage keys live only in the customer's cluster (the connector's Secret). When the control
  plane needs to touch data it asks the connector for a presigned URL for one object, one method, at most
  ``CAIRN_DATAPLANE_MAX_PRESIGN_SECONDS`` (900); a job gets a Trino token for its owner valid for its run.
* **Compute per workspace.** ``PUT /v1/compute/workspaces/{id}`` with ``{"compute": "customer", "dataplane": ...}``
  or ``{"compute": "managed"}`` (Cairn's own pool in the hosted cluster, ``CAIRN_MANAGED_COMPUTE``). Workspaces
  without a setting use ``CAIRN_COMPUTE_DEFAULT`` (``managed``; ``customer`` with ``CAIRN_COMPUTE_DEFAULT_DATAPLANE``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import time
import uuid
from typing import Any, Callable, Optional

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, select, update
from sqlalchemy.exc import IntegrityError

from cairn.agent import crypto
from cairn.dataplane.connector import rotation_proof_bytes
from cairn.dataplane.store import (dataplane_calls, dataplane_connectors, dataplane_nonces, dataplane_tokens,
                                   dataplanes, workspace_compute)

logger = logging.getLogger(__name__)
NAME = r"^[a-z][a-z0-9-]{0,62}$"
KINDS = {"trino.http", "job.run", "storage.presign", "ping"}
OPA_KINDS = {"allow": "allow", "batch": "batch", "row-filters": "row_filters", "column-mask": "column_mask"}
MAX_RESULT = 48 << 20


def _now() -> int:
    return int(time.time() * 1000)


def _iso(ms: Optional[int]) -> Optional[str]:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000)) if ms else None


class DataPlaneError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class DataPlaneSpec(BaseModel):
    name: str = Field(pattern=NAME)
    description: str = Field(default="", max_length=1000)
    connection: Optional[str] = Field(default=None, max_length=128, description="the Trino connection it serves")


class ComputeSpec(BaseModel):
    compute: str = Field(pattern=r"^(customer|managed)$")
    dataplane: Optional[str] = Field(default=None, pattern=NAME)


class EnrollRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    signing_public: str
    encryption_public: str
    hostname: str = Field(max_length=255)
    version: str = Field(max_length=64)
    capabilities: dict = Field(default_factory=dict)


class RotateRequest(BaseModel):
    signing_public: str
    encryption_public: str
    proof: str



def _raw_key(key: str) -> bool:
    try:
        return len(crypto.b64d(key)) == 32
    except Exception:  # noqa: BLE001 - not base64
        return False


class DataPlanes:
    def __init__(self, engine, keys, audit=None, trino_access=None, workspaces_of: Optional[Callable[[str], list[str]]] = None,
                 offline_after_s: Optional[float] = None, max_skew_s: int = 300, key_grace_s: Optional[int] = None):
        """``keys``: the gateway's identity (cairn.agent.gateway.GatewayKeys): answers are sealed to it."""
        e = os.environ.get
        self.engine, self.keys, self.audit, self.trino_access = engine, keys, audit, trino_access
        self.workspaces_of = workspaces_of
        self.offline_ms = int(float(offline_after_s if offline_after_s is not None
                                    else e("CAIRN_DATAPLANE_OFFLINE_SECONDS", "30")) * 1000)
        self.max_skew_ms = max_skew_s * 1000
        self.key_grace_ms = int((key_grace_s if key_grace_s is not None else int(e("CAIRN_AGENT_KEY_GRACE_SECONDS", "600"))) * 1000)
        self.managed = e("CAIRN_MANAGED_COMPUTE", "true").strip().lower() in ("1", "true", "yes", "on")
        self.default_compute = e("CAIRN_COMPUTE_DEFAULT", "managed").strip() or "managed"
        self.default_dataplane = e("CAIRN_COMPUTE_DEFAULT_DATAPLANE", "").strip() or None
        self.max_presign_s = int(e("CAIRN_DATAPLANE_MAX_PRESIGN_SECONDS", "900"))
        self.poll_wait_s = float(e("CAIRN_DATAPLANE_POLL_SECONDS", "10"))

    # ---- data planes ---------------------------------------------------------------------------------
    def create(self, spec: DataPlaneSpec, actor: str) -> dict:
        try:
            with self.engine.begin() as conn:
                conn.execute(dataplanes.insert().values(name=spec.name, description=spec.description,
                                                        connection=spec.connection, created_by=actor, created_ms=_now()))
        except IntegrityError:
            raise DataPlaneError(f"data plane {spec.name} already exists", 409)
        self._audit("dataplane.created", actor, spec.name, spec.model_dump())
        return self.get(spec.name)

    def update(self, spec: DataPlaneSpec, actor: str) -> dict:
        with self.engine.begin() as conn:
            n = conn.execute(update(dataplanes).where(dataplanes.c.name == spec.name).values(
                description=spec.description, connection=spec.connection)).rowcount
        if not n:
            raise DataPlaneError(f"no data plane {spec.name}", 404)
        self._audit("dataplane.updated", actor, spec.name, spec.model_dump())
        return self.get(spec.name)

    def _row(self, name: str) -> Any:
        with self.engine.connect() as conn:
            r = conn.execute(select(dataplanes).where(dataplanes.c.name == name)).first()
        if r is None:
            raise DataPlaneError(f"no data plane {name}", 404)
        return r

    def get(self, name: str) -> dict:
        r = self._row(name)
        conns = self.connectors(name)
        online = [c for c in conns if c["status"] == "ONLINE"]
        return {"name": r.name, "description": r.description, "connection": r.connection, "created_by": r.created_by,
                "created_at": _iso(r.created_ms), "tunnel": "up" if online else "down",
                "connectors_online": len(online), "connectors": len([c for c in conns if c["status"] != "REVOKED"]),
                "capabilities": online[0]["capabilities"] if online else {}}

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            names = [r[0] for r in conn.execute(select(dataplanes.c.name).order_by(dataplanes.c.name))]
        return [self.get(n) for n in names]

    def delete(self, name: str, actor: str) -> None:
        self._row(name)
        with self.engine.begin() as conn:
            used = conn.execute(select(workspace_compute.c.workspace).where(
                workspace_compute.c.dataplane == name)).first()
            if used:
                raise DataPlaneError(f"workspace {used[0]} computes on {name}: move it first", 409)
            conn.execute(update(dataplane_connectors).where(dataplane_connectors.c.dataplane == name).values(status="REVOKED"))
            conn.execute(delete(dataplane_tokens).where(dataplane_tokens.c.dataplane == name))
            conn.execute(update(dataplane_calls).where(and_(dataplane_calls.c.dataplane == name,
                                                            dataplane_calls.c.status.in_(["QUEUED", "LEASED"])))
                         .values(status="FAILED", error="the data plane was deleted", finished_ms=_now()))
            conn.execute(delete(dataplanes).where(dataplanes.c.name == name))
        self._audit("dataplane.deleted", actor, name, {})

    def enrollment_token(self, name: str, actor: str, ttl_s: int = 3600) -> dict:
        self._row(name)
        token = "cairn_dpe_" + secrets.token_urlsafe(32)
        expires = _now() + max(60, min(ttl_s, 86_400)) * 1000
        with self.engine.begin() as conn:
            conn.execute(dataplane_tokens.insert().values(token_hash=crypto.token_hash(token), dataplane=name,
                                                          expires_at_ms=expires, created_by=actor))
        self._audit("dataplane.enrollment_token_created", actor, name, {"expires_at_ms": expires})
        return {"token": token, "dataplane": name, "expires_at": _iso(expires)}

    def connectors(self, name: str) -> list[dict]:
        now = _now()
        with self.engine.connect() as conn:
            rows = conn.execute(select(dataplane_connectors).where(dataplane_connectors.c.dataplane == name)
                                .order_by(dataplane_connectors.c.enrolled_at_ms)).fetchall()
        out = []
        for r in rows:
            status = r.status
            if status == "ONLINE" and (r.last_seen_ms or 0) < now - self.offline_ms:
                status = "OFFLINE"
            out.append({"connector_id": r.connector_id, "hostname": r.hostname, "version": r.version, "status": status,
                        "capabilities": r.capabilities or {}, "enrolled_at": _iso(r.enrolled_at_ms),
                        "last_seen": _iso(r.last_seen_ms), "last_ip": r.last_ip})
        return out

    def revoke(self, name: str, connector_id: str, actor: str) -> None:
        with self.engine.begin() as conn:
            n = conn.execute(update(dataplane_connectors).where(and_(
                dataplane_connectors.c.connector_id == connector_id, dataplane_connectors.c.dataplane == name))
                .values(status="REVOKED")).rowcount
            # What it had leased goes back to the queue for another connector of the data plane.
            conn.execute(update(dataplane_calls).where(and_(dataplane_calls.c.connector_id == connector_id,
                                                            dataplane_calls.c.status == "LEASED"))
                         .values(status="QUEUED", connector_id=None, leased_ms=None))
        if not n:
            raise DataPlaneError("no such connector", 404)
        self._audit("dataplane.connector_revoked", actor, connector_id, {"dataplane": name})

    # ---- compute per workspace ------------------------------------------------------------------------
    def set_compute(self, workspace: str, spec: ComputeSpec, actor: str) -> dict:
        if spec.compute == "customer":
            if not spec.dataplane:
                raise DataPlaneError("compute customer needs the data plane (dataplane)", 422)
            self._row(spec.dataplane)
        elif not self.managed:
            raise DataPlaneError("this Cairn has no managed compute pool (CAIRN_MANAGED_COMPUTE)", 422)
        row = {"compute": spec.compute, "dataplane": spec.dataplane if spec.compute == "customer" else None,
               "updated_by": actor, "updated_ms": _now()}
        with self.engine.begin() as conn:
            if conn.execute(select(workspace_compute.c.workspace).where(workspace_compute.c.workspace == workspace)).first():
                conn.execute(update(workspace_compute).where(workspace_compute.c.workspace == workspace).values(**row))
            else:
                conn.execute(workspace_compute.insert().values(workspace=workspace, **row))
        self._audit("dataplane.compute_set", actor, workspace, {"compute": spec.compute, "dataplane": row["dataplane"]})
        return self.get_compute(workspace)

    def get_compute(self, workspace: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(workspace_compute).where(workspace_compute.c.workspace == workspace)).first()
        if r is None:
            return {"workspace": workspace, "compute": self.default_compute,
                    "dataplane": self.default_dataplane if self.default_compute == "customer" else None, "default": True}
        return {"workspace": r.workspace, "compute": r.compute, "dataplane": r.dataplane, "default": False,
                "updated_by": r.updated_by, "updated_at": _iso(r.updated_ms)}

    def list_compute(self) -> list[dict]:
        with self.engine.connect() as conn:
            ids = [r[0] for r in conn.execute(select(workspace_compute.c.workspace).order_by(workspace_compute.c.workspace))]
        return [self.get_compute(w) for w in ids]

    def delete_compute(self, workspace: str, actor: str) -> None:
        with self.engine.begin() as conn:
            n = conn.execute(delete(workspace_compute).where(workspace_compute.c.workspace == workspace)).rowcount
        if not n:
            raise DataPlaneError(f"workspace {workspace} has no compute setting", 404)
        self._audit("dataplane.compute_cleared", actor, workspace, {})

    def compute_for(self, owner: str) -> tuple[str, Optional[str]]:
        """Where ``owner``'s work runs: (``customer``, data plane) or (``managed``, None). The owner's workspaces must
        agree; when they do not, the work is refused rather than placed on one of them."""
        wss: list[str] = []
        if self.workspaces_of is not None:
            try:
                wss = sorted(set(self.workspaces_of(owner)))
            except Exception as e:  # noqa: BLE001 - fail closed
                raise DataPlaneError(f"could not tell {owner}'s workspace: {e}", 503)
        places = {(c["compute"], c["dataplane"]) for c in (self.get_compute(w) for w in wss)} if wss else {
            (self.default_compute, self.default_dataplane if self.default_compute == "customer" else None)}
        if len(places) != 1:
            raise DataPlaneError(f"{owner} is in workspaces that compute in different places ({sorted(map(str, places))}): "
                                 "their work cannot be placed", 409)
        mode, dp = places.pop()
        if mode == "customer" and not dp:
            raise DataPlaneError("compute is customer but no data plane is set (CAIRN_COMPUTE_DEFAULT_DATAPLANE)", 503)
        if mode == "managed" and not self.managed:
            raise DataPlaneError("this Cairn has no managed compute pool", 503)
        return mode, dp

    # ---- the tunnel: connectors' side ---------------------------------------------------------------
    def enroll(self, req: EnrollRequest, ip: Optional[str]) -> dict:
        if not (_raw_key(req.signing_public) and _raw_key(req.encryption_public)):
            raise DataPlaneError("public keys must be raw 32-byte Ed25519/X25519 keys", 400)
        now, th = _now(), crypto.token_hash(req.token)
        cid = f"dpc_{uuid.uuid4().hex[:16]}"
        with self.engine.begin() as conn:
            row = conn.execute(select(dataplane_tokens).where(dataplane_tokens.c.token_hash == th)).first()
            if not row or row.used_by or row.expires_at_ms < now:
                raise DataPlaneError("invalid, expired or already used enrolment token", 401)
            if conn.execute(update(dataplane_tokens).where(and_(dataplane_tokens.c.token_hash == th,
                                                                dataplane_tokens.c.used_by.is_(None)))
                            .values(used_by=cid)).rowcount != 1:
                raise DataPlaneError("enrolment token already used", 401)
            conn.execute(dataplane_connectors.insert().values(
                connector_id=cid, dataplane=row.dataplane, hostname=req.hostname, version=req.version,
                signing_public=req.signing_public, encryption_public=req.encryption_public, status="ONLINE",
                capabilities=req.capabilities, enrolled_at_ms=now, last_seen_ms=now, last_ip=ip))
        self._audit("dataplane.connector_enrolled", cid, row.dataplane, {"hostname": req.hostname, "ip": ip})
        ident = self.keys.identity
        return {"connector_id": cid, "dataplane": row.dataplane, "gateway_encryption_public": ident.encryption_public,
                "gateway_signing_public": ident.signing_public}

    def authenticate(self, method: str, path: str, headers: dict[str, str], body: bytes) -> dict:
        """A connector's signed request: signature by its key, clock skew, nonce, and not revoked."""
        h = {k.lower(): v for k, v in headers.items()}
        cid, ts, nonce, sig = (h.get(crypto.HEADER_AGENT.lower()), h.get(crypto.HEADER_TS.lower()),
                               h.get(crypto.HEADER_NONCE.lower()), h.get(crypto.HEADER_SIG.lower()))
        if not all((cid, ts, nonce, sig)):
            raise DataPlaneError("missing signature headers: this data plane is not enrolled", 401)
        try:
            ts_ms = int(ts) * 1000  # type: ignore[arg-type]
        except ValueError:
            raise DataPlaneError("bad timestamp", 401)
        now = _now()
        if abs(now - ts_ms) > self.max_skew_ms:
            raise DataPlaneError("request timestamp outside allowed clock skew", 401)
        with self.engine.connect() as conn:
            c = conn.execute(select(dataplane_connectors).where(dataplane_connectors.c.connector_id == cid)).first()
            dp = conn.execute(select(dataplanes.c.name).where(dataplanes.c.name == c.dataplane)).first() if c else None
        if c is None or dp is None:
            raise DataPlaneError("unknown connector: this data plane is not enrolled", 401)
        if c.status == "REVOKED":
            raise DataPlaneError("this connector was revoked", 401)
        keys = [c.signing_public]
        if c.previous_signing_public and (c.keys_rotated_at_ms or 0) + self.key_grace_ms > now:
            keys.append(c.previous_signing_public)
        signed_with = None
        for key in keys:
            try:
                crypto.verify_signature(key, method, path, ts, nonce, body, sig)  # type: ignore[arg-type]
                signed_with = key
                break
            except crypto.CryptoError:
                continue
        if signed_with is None:
            raise DataPlaneError("invalid signature", 401)
        try:
            with self.engine.begin() as conn:
                conn.execute(dataplane_nonces.insert().values(nonce=nonce, connector_id=cid, seen_at_ms=now))
                conn.execute(delete(dataplane_nonces).where(dataplane_nonces.c.seen_at_ms < now - 2 * self.max_skew_ms))
        except IntegrityError:
            raise DataPlaneError("replayed request (nonce reuse)", 401)
        return {**dict(c._mapping), "signed_with": signed_with}

    def rotate(self, c: dict, req: RotateRequest) -> dict:
        if not (_raw_key(req.signing_public) and _raw_key(req.encryption_public)):
            raise DataPlaneError("public keys must be raw 32-byte Ed25519/X25519 keys", 400)
        if c.get("signed_with") != c["signing_public"]:
            raise DataPlaneError("only the connector's current key rotates its keys", 401)
        if req.signing_public in (c["signing_public"], c.get("previous_signing_public")):
            raise DataPlaneError("the new signing key must be a new key", 400)
        try:
            crypto.verify_bytes(req.signing_public, rotation_proof_bytes(c["connector_id"], req.signing_public,
                                                                         req.encryption_public), req.proof)
        except crypto.CryptoError:
            raise DataPlaneError("the proof was not made with the new signing key", 400)
        now = _now()
        with self.engine.begin() as conn:
            n = conn.execute(update(dataplane_connectors).where(and_(
                dataplane_connectors.c.connector_id == c["connector_id"],
                dataplane_connectors.c.signing_public == c["signing_public"],
                dataplane_connectors.c.status != "REVOKED")).values(
                previous_signing_public=c["signing_public"], signing_public=req.signing_public,
                encryption_public=req.encryption_public, keys_rotated_at_ms=now)).rowcount
        if n != 1:
            raise DataPlaneError("the connector's keys changed meanwhile, or it was revoked", 409)
        self._audit("dataplane.keys_rotated", c["connector_id"], c["dataplane"], {})
        return {"connector_id": c["connector_id"], "rotated_at_ms": now,
                "previous_key_valid_until_ms": now + self.key_grace_ms}

    def poll(self, c: dict, ip: Optional[str], capabilities: Optional[dict] = None, wait_s: Optional[float] = None,
             max_calls: int = 8) -> dict:
        """Long-poll: the calls queued for the connector's data plane, leased to it and sealed to its key."""
        end = time.monotonic() + max(0.0, min(self.poll_wait_s if wait_s is None else wait_s, 25.0))
        while True:
            now = _now()
            with self.engine.begin() as conn:
                vals: dict = {"last_seen_ms": now, "last_ip": ip}
                if capabilities is not None:
                    vals["capabilities"] = capabilities
                conn.execute(update(dataplane_connectors).where(and_(
                    dataplane_connectors.c.connector_id == c["connector_id"],
                    dataplane_connectors.c.status != "REVOKED")).values(**vals))
                conn.execute(update(dataplane_calls).where(and_(dataplane_calls.c.dataplane == c["dataplane"],
                                                                dataplane_calls.c.status.in_(["QUEUED", "LEASED"]),
                                                                dataplane_calls.c.deadline_ms < now))
                             .values(status="EXPIRED", request={}, finished_ms=now, error="no answer before the deadline"))
                rows = conn.execute(select(dataplane_calls).where(and_(
                    dataplane_calls.c.dataplane == c["dataplane"], dataplane_calls.c.status == "QUEUED"))
                    .order_by(dataplane_calls.c.created_ms).limit(max_calls)).fetchall()
                leased = []
                for r in rows:
                    if conn.execute(update(dataplane_calls).where(and_(dataplane_calls.c.call_id == r.call_id,
                                                                       dataplane_calls.c.status == "QUEUED"))
                                    .values(status="LEASED", connector_id=c["connector_id"], leased_ms=now)).rowcount:
                        leased.append(r)
            if leased or time.monotonic() >= end:
                break
            time.sleep(0.1)
        calls = [{"call_id": r.call_id, "kind": r.kind, "envelope": crypto.seal(
            c["encryption_public"], json.dumps({"kind": r.kind, "request": r.request, "deadline_ms": r.deadline_ms}).encode(),
            crypto.aad_for(c["connector_id"], r.call_id))} for r in leased]
        return {"calls": calls, "server_time_ms": _now()}

    def answer(self, c: dict, call_id: str, envelope: dict) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(dataplane_calls).where(dataplane_calls.c.call_id == call_id)).first()
        if r is None or r.connector_id != c["connector_id"]:
            raise DataPlaneError("no such call for this connector", 404)
        if r.status != "LEASED":
            raise DataPlaneError(f"the call is {r.status}", 409)
        try:
            plain = crypto.open_sealed(self.keys.identity.encryption_key, envelope,
                                       crypto.aad_for(c["connector_id"], call_id, "answer"), MAX_RESULT)
            doc = json.loads(plain)
        except (crypto.CryptoError, ValueError) as e:
            raise DataPlaneError(f"the answer could not be opened: {e}", 400)
        ok = isinstance(doc, dict) and not doc.get("error")
        with self.engine.begin() as conn:
            conn.execute(update(dataplane_calls).where(and_(dataplane_calls.c.call_id == call_id,
                                                            dataplane_calls.c.status == "LEASED")).values(
                status="DONE" if ok else "FAILED", result=json.dumps(doc.get("result")) if ok else None,
                error=None if ok else str((doc or {}).get("error") or "failed")[:2000], finished_ms=_now(), request={}))
        return {"ok": True}

    def opa(self, c: dict, kind: str, doc: Any) -> dict:
        """The data plane's Trino asks the hosted PDP (through its connector)."""
        if self.trino_access is None:
            raise DataPlaneError("Trino access control is not configured on the control plane (CAIRN_TRINO_ACCESS_KEY)", 503)
        method = OPA_KINDS.get(kind)
        inp = doc.get("input") if isinstance(doc, dict) else None
        if method is None or not isinstance(inp, dict):
            raise DataPlaneError("expected {\"input\": ...} for allow, batch, row-filters or column-mask", 400)
        return {"result": getattr(self.trino_access, method)(inp)}

    # ---- the tunnel: control plane's side -----------------------------------------------------------
    def tunnel_up(self, name: str) -> bool:
        with self.engine.connect() as conn:
            r = conn.execute(select(dataplane_connectors.c.connector_id).where(and_(
                dataplane_connectors.c.dataplane == name, dataplane_connectors.c.status == "ONLINE",
                dataplane_connectors.c.last_seen_ms >= _now() - self.offline_ms))).first()
        return r is not None

    def _down(self, name: str) -> DataPlaneError:
        return DataPlaneError(f"the tunnel to data plane {name} is down (no connector has called in for "
                              f"{self.offline_ms // 1000}s): queries and jobs on it are refused until it is back", 503)

    def call(self, name: str, kind: str, request: dict, actor: str, timeout_s: float = 60.0) -> Any:
        """Run one call on a data plane through its tunnel and return its answer; fail closed when the tunnel is
        down, the connector refuses, or it does not answer in time."""
        if kind not in KINDS:
            raise DataPlaneError(f"unknown call {kind}", 400)
        self._row(name)
        if not self.tunnel_up(name):
            raise self._down(name)
        cid, now = f"dpcall_{uuid.uuid4().hex}", _now()
        deadline = now + int(timeout_s * 1000)
        with self.engine.begin() as conn:
            conn.execute(dataplane_calls.insert().values(call_id=cid, dataplane=name, kind=kind, request=request,
                                                         status="QUEUED", actor=actor, created_ms=now, deadline_ms=deadline))
        try:
            while True:
                with self.engine.connect() as conn:
                    r = conn.execute(select(dataplane_calls).where(dataplane_calls.c.call_id == cid)).first()
                if r.status == "DONE":
                    return json.loads(r.result) if r.result else None
                if r.status == "FAILED":
                    raise DataPlaneError(f"data plane {name}: {r.error}", 502)
                if r.status == "EXPIRED" or _now() > deadline:
                    raise DataPlaneError(f"data plane {name} did not answer within {int(timeout_s)}s", 504)
                if not self.tunnel_up(name):
                    raise self._down(name)
                time.sleep(0.05 if _now() - now < 2000 else 0.25)
        finally:
            with self.engine.begin() as conn:      # answers are not kept: neither the request nor the data
                conn.execute(update(dataplane_calls).where(and_(
                    dataplane_calls.c.call_id == cid, dataplane_calls.c.status.in_(["QUEUED", "LEASED"]))).values(
                    status="EXPIRED", finished_ms=_now(), error="the caller stopped waiting"))
                conn.execute(update(dataplane_calls).where(dataplane_calls.c.call_id == cid).values(request={}, result=None))

    def presign(self, name: str, bucket: str, key: str, method: str, ttl_s: int, actor: str) -> dict:
        """A presigned URL for one object, made by the data plane with its own storage key (never sent here)."""
        if method not in ("GET", "PUT"):
            raise DataPlaneError("method GET or PUT", 422)
        ttl_s = max(1, min(int(ttl_s), self.max_presign_s))
        out = self.call(name, "storage.presign", {"bucket": bucket, "key": key, "method": method, "ttl_seconds": ttl_s},
                        actor, 30)
        self._audit("dataplane.presigned", actor, name, {"bucket": bucket, "key": key, "method": method, "ttl": ttl_s})
        return out

    def purge(self, keep_ms: int = 86_400_000) -> int:
        with self.engine.begin() as conn:
            return conn.execute(delete(dataplane_calls).where(and_(
                dataplane_calls.c.status.in_(["DONE", "FAILED", "EXPIRED"]),
                dataplane_calls.c.created_ms < _now() - keep_ms))).rowcount

    def _audit(self, action: str, actor: str, subject: Optional[str], details: dict) -> None:
        if self.audit is not None:
            self.audit.record("dataplane", action, actor, subject, details)


# --------------------------------------------------------------------------- Trino through the tunnel
class TunnelTransport(httpx.BaseTransport):
    """An httpx transport that sends each request to a data plane's Trino through its tunnel (``trino.http``)."""

    def __init__(self, planes: DataPlanes, name: str, actor: str, timeout_s: float = 120.0):
        self.planes, self.name, self.actor, self.timeout_s = planes, name, actor, timeout_s

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        hdrs = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length", "connection")}
        out = self.planes.call(self.name, "trino.http", {
            "method": request.method, "url": str(request.url), "headers": hdrs,
            "body": base64.b64encode(body).decode()}, self.actor, self.timeout_s)
        return httpx.Response(int(out["status"]), headers=out.get("headers") or {},
                              content=base64.b64decode(out.get("body") or ""), request=request)


def tunnel_client(planes: DataPlanes, name: str, actor: str) -> httpx.Client:
    return httpx.Client(transport=TunnelTransport(planes, name, actor), timeout=httpx.Timeout(130.0))


# --------------------------------------------------------------------------- jobs on a data plane
class DataPlaneRunner:
    """The jobs runtime interface (``run``) on a data plane: the run happens in the customer's cluster."""

    def __init__(self, planes: DataPlanes, name: str, image: Optional[str] = None, uid: Optional[int] = None):
        self.planes, self.name, self.image, self.uid = planes, name, image, uid

    def run(self, run_id: str, kind: str, files: dict[str, str], env: dict[str, str], timeout_s: int) -> tuple[int, str]:
        owner = "urn:li:corpuser:" + env.get("CAIRN_USER", "")
        out = self.planes.call(self.name, "job.run", {"run_id": run_id, "kind": kind, "files": files, "env": env,
                                                      "timeout_s": timeout_s, "image": self.image, "uid": self.uid},
                               owner, timeout_s + 300)
        return int(out["rc"]), str(out.get("text") or "")


class ComputeRouter:
    """Jobs (and so notebooks and flows' Python, Spark and dbt steps) run where their owner's workspace computes: on
    its data plane (``customer``) or on Cairn's managed pool, the hosted runtime (``managed``)."""

    def __init__(self, hosted: Any, planes: DataPlanes, egress: Any = None):
        self.hosted, self.planes, self.egress = hosted, planes, egress
        self.image: Optional[str] = None
        self.uid: Optional[int] = None
        self._egress_ws: Optional[str] = None

    def contain_egress(self, workspace: str) -> "ComputeRouter":
        import copy

        other = copy.copy(self)
        other._egress_ws = workspace
        return other

    def run(self, run_id: str, kind: str, files: dict[str, str], env: dict[str, str], timeout_s: int) -> tuple[int, str]:
        from cairn.pipelines.jobs import JobError, with_image

        owner = "urn:li:corpuser:" + env.get("CAIRN_USER", "")
        try:
            mode, dp = self.planes.compute_for(owner)
            if mode == "customer":
                return DataPlaneRunner(self.planes, str(dp), self.image, self.uid).run(run_id, kind, files, env, timeout_s)
        except DataPlaneError as e:
            raise JobError(str(e), e.status)
        if self.hosted is None:
            raise JobError("the managed pool has no job runtime (CAIRN_JOBS_RUNTIME)", 503)
        runner = with_image(self.hosted, self.image, self.uid) if self.image else self.hosted
        if self._egress_ws is not None and self.egress is not None:
            runner = self.egress.contain(runner, self._egress_ws)
        return runner.run(run_id, kind, files, env, timeout_s)
