"""Remote agent gateway: enrolment, signed agent protocol, task leasing, MCP forwarding.

The gateway is the only component exposed to private networks. Agents never hold a
Cairn Core token: they send *encrypted* MCP batches here, and the gateway validates each MCP
against the pool policy before forwarding it to Cairn Core with its own credentials.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from cairn.agent import crypto
from cairn.agent.store import agents, enrollment_tokens, nonces, pools, tasks
from cairn.common.audit import AuditLog
from cairn.common.core import MetadataGraph
from cairn.common.proposals import aspect_value, make_mcp

logger = logging.getLogger(__name__)

_SECRET_KEY = re.compile(r"(pass(word)?|secret|token|private_?key|api_?key|credential|client_secret)$", re.I)
_SECRET_REF = re.compile(r"^\$\{[A-Za-z0-9_]+\}$")
_DATASET_PLATFORM = re.compile(r"urn:li:dataPlatform:([A-Za-z0-9_\-]+)")
OFFLINE_AFTER_MS = 90_000
_PROTECTED_ENTITY_TYPES = {
    "corpuser", "corpGroup", "dataHubPolicy", "dataHubRole", "dataHubSecret", "dataHubAccessToken",
    "dataHubExecutionRequest", "dataHubIngestionSource", "globalSettings",
}


class GatewayError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---- request/response models ------------------------------------------------------
class PoolSpec(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9\-]{1,62}$")
    description: str = ""
    allowed_platforms: list[str] = Field(default_factory=list, description="empty = any platform")
    allowed_aspects: Optional[list[str]] = None
    max_concurrency: int = Field(default=2, ge=1, le=64)
    allow_profiling: bool = True


class EnrollRequest(BaseModel):
    token: str
    signing_public: str
    encryption_public: str
    hostname: str = Field(max_length=255)
    version: str = Field(max_length=64)


class EnrollResponse(BaseModel):
    agent_id: str
    pool: str
    gateway_encryption_public: str
    gateway_signing_public: str
    heartbeat_interval_seconds: int = 15


class HeartbeatRequest(BaseModel):
    running_task_ids: list[str] = Field(default_factory=list)
    version: Optional[str] = None


class CompleteRequest(BaseModel):
    status: str = Field(pattern=r"^(SUCCESS|FAILURE|CANCELLED)$")
    report: dict[str, Any] = Field(default_factory=dict)
    final_seq: int = -1


class EnqueueRequest(BaseModel):
    pool: str
    recipe: dict[str, Any]
    execution_request_urn: Optional[str] = None
    max_attempts: int = Field(default=3, ge=1, le=10)


def _iso(ms: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat() if ms else None


def _now() -> int:
    return int(time.time() * 1000)


def find_inline_secrets(recipe: Any, path: str = "") -> list[str]:
    """Paths of secret-looking keys whose value is not a ``${VAR}`` reference."""
    found: list[str] = []
    if isinstance(recipe, dict):
        for k, v in recipe.items():
            p = f"{path}.{k}" if path else k
            if isinstance(v, (dict, list)):
                found += find_inline_secrets(v, p)
            elif _SECRET_KEY.search(str(k)) and v not in (None, "") and not _SECRET_REF.match(str(v)):
                found.append(p)
    elif isinstance(recipe, list):
        for i, v in enumerate(recipe):
            found += find_inline_secrets(v, f"{path}[{i}]")
    return found


class GatewayKeys:
    """The gateway's own identity; agents encrypt batches to its X25519 key.

    Loaded (or created) on first use, so processes that only use the admin side of
    :class:`GatewayService`, such as ``cairn api``, never touch the key material.
    """

    def __init__(self, key_dir: str):
        self.path = os.path.join(key_dir, "gateway-identity.json")
        self._identity: Optional[crypto.Identity] = None

    @property
    def identity(self) -> crypto.Identity:
        if self._identity is None:
            if os.path.exists(self.path):
                self._identity, _ = crypto.Identity.load(self.path)
            else:
                identity = crypto.Identity.generate()
                identity.save(self.path)
                self._identity = identity
        return self._identity


class GatewayService:
    def __init__(self, engine: Engine, keys: GatewayKeys, graph: Optional[MetadataGraph],
                 audit: Optional[AuditLog] = None, max_skew_seconds: int = 300, lease_seconds: int = 300):
        self.engine = engine
        self.keys = keys
        self.graph = graph
        self.audit = audit
        self.max_skew_ms = max_skew_seconds * 1000
        self.lease_ms = lease_seconds * 1000

    # ---- admin -----------------------------------------------------------------------
    def create_pool(self, spec: PoolSpec, actor: str) -> dict:
        try:
            with self.engine.begin() as conn:
                conn.execute(pools.insert().values(
                    name=spec.name, description=spec.description, allowed_platforms=spec.allowed_platforms,
                    allowed_aspects=spec.allowed_aspects, max_concurrency=spec.max_concurrency,
                    allow_profiling=spec.allow_profiling, created_at=datetime.now(timezone.utc).isoformat()))
        except IntegrityError:
            raise GatewayError(f"pool {spec.name} already exists", 409)
        self._audit("pool.created", actor, spec.name, spec.model_dump())
        return self.get_pool(spec.name)

    def get_pool(self, name: str) -> dict:
        with self.engine.connect() as conn:
            row = conn.execute(select(pools).where(pools.c.name == name)).first()
            if not row:
                raise GatewayError(f"pool {name} not found", 404)
            online = conn.execute(select(func.count()).select_from(agents).where(and_(
                agents.c.pool == name, agents.c.status == "ONLINE",
                agents.c.last_heartbeat_ms >= _now() - OFFLINE_AFTER_MS))).scalar()
        d = dict(row._mapping)
        d["agents_online"] = int(online or 0)
        return d

    def list_pools(self) -> list[dict]:
        with self.engine.connect() as conn:
            names = [r[0] for r in conn.execute(select(pools.c.name).order_by(pools.c.name))]
        return [self.get_pool(n) for n in names]

    def create_enrollment_token(self, pool: str, actor: str, ttl_seconds: int = 3600) -> dict:
        self.get_pool(pool)
        token = "cairn_enr_" + secrets.token_urlsafe(32)
        expires = _now() + ttl_seconds * 1000
        with self.engine.begin() as conn:
            conn.execute(enrollment_tokens.insert().values(
                token_hash=crypto.token_hash(token), pool=pool, expires_at_ms=expires, created_by=actor))
        self._audit("enrollment_token.created", actor, pool, {"expires_at_ms": expires})
        return {"token": token, "pool": pool, "expires_at": _iso(expires)}

    def list_agents(self) -> list[dict]:
        now = _now()
        with self.engine.connect() as conn:
            rows = conn.execute(select(agents).order_by(agents.c.pool, agents.c.hostname)).fetchall()
        out = []
        for r in rows:
            m = dict(r._mapping)
            status = m["status"]
            if status == "ONLINE" and (m["last_heartbeat_ms"] or 0) < now - OFFLINE_AFTER_MS:
                status = "OFFLINE"
            out.append({"agent_id": m["agent_id"], "pool": m["pool"], "hostname": m["hostname"],
                        "version": m["version"], "status": status, "last_heartbeat": _iso(m["last_heartbeat_ms"]),
                        "enrolled_at": _iso(m["enrolled_at_ms"]), "running_tasks": m["running_tasks"],
                        "last_ip": m["last_ip"]})
        return out

    def revoke_agent(self, agent_id: str, actor: str) -> None:
        with self.engine.begin() as conn:
            res = conn.execute(update(agents).where(agents.c.agent_id == agent_id).values(status="REVOKED"))
            # Return the revoked agent's in-flight work to the queue.
            conn.execute(update(tasks).where(and_(tasks.c.agent_id == agent_id,
                                                  tasks.c.status.in_(["LEASED", "RUNNING"])))
                         .values(status="QUEUED", agent_id=None, lease_expires_ms=None, run_id=None))
        if not res.rowcount:
            raise GatewayError("agent not found", 404)
        self._audit("agent.revoked", actor, agent_id, {})

    def enqueue(self, req: EnqueueRequest, actor: str) -> dict:
        self.get_pool(req.pool)
        leaked = find_inline_secrets(req.recipe)
        if leaked:
            raise GatewayError(f"recipe contains inline credentials at {leaked}; use ${{SECRET}} references "
                               "resolved by the in-VPC agent", 422)
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        try:
            with self.engine.begin() as conn:
                conn.execute(tasks.insert().values(
                    task_id=task_id, pool=req.pool, kind="ingest", recipe=req.recipe,
                    execution_request_urn=req.execution_request_urn, status="QUEUED", attempts=0,
                    max_attempts=req.max_attempts, created_at_ms=_now(), last_seq=-1,
                    mcps_forwarded=0, mcps_rejected=0, logs=[]))
        except IntegrityError:
            raise GatewayError("execution request already enqueued", 409)
        self._audit("task.enqueued", actor, task_id, {"pool": req.pool, "er": req.execution_request_urn})
        return {"task_id": task_id, "status": "QUEUED"}

    def list_runs(self, limit: int = 200) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(select(tasks).order_by(tasks.c.created_at_ms.desc()).limit(limit)).fetchall()
        return [{"run_id": r.run_id, "task_id": r.task_id, "pool": r.pool, "agent_id": r.agent_id,
                 "status": r.status, "attempts": r.attempts, "started_at": _iso(r.started_at_ms),
                 "finished_at": _iso(r.finished_at_ms), "mcps_forwarded": r.mcps_forwarded,
                 "mcps_rejected": r.mcps_rejected, "execution_request_urn": r.execution_request_urn}
                for r in rows]

    # ---- agent protocol --------------------------------------------------------------
    def enroll(self, req: EnrollRequest, ip: Optional[str]) -> EnrollResponse:
        now = _now()
        th = crypto.token_hash(req.token)
        for key in (req.signing_public, req.encryption_public):
            if len(crypto.b64d(key)) != 32:
                raise GatewayError("public keys must be raw 32-byte Ed25519/X25519 keys", 400)
        agent_id = f"agt_{uuid.uuid4().hex[:16]}"
        with self.engine.begin() as conn:
            row = conn.execute(select(enrollment_tokens).where(enrollment_tokens.c.token_hash == th)).first()
            if not row or row.used_by or row.expires_at_ms < now:
                raise GatewayError("invalid, expired or already used enrollment token", 401)
            # Single-use: conditional update wins the race between concurrent enrolments.
            res = conn.execute(update(enrollment_tokens).where(and_(
                enrollment_tokens.c.token_hash == th, enrollment_tokens.c.used_by.is_(None))).values(used_by=agent_id))
            if res.rowcount != 1:
                raise GatewayError("enrollment token already used", 401)
            conn.execute(agents.insert().values(
                agent_id=agent_id, pool=row.pool, hostname=req.hostname, version=req.version,
                signing_public=req.signing_public, encryption_public=req.encryption_public, status="ONLINE",
                enrolled_at_ms=now, last_heartbeat_ms=now, last_ip=ip, running_tasks=0))
        self._audit("agent.enrolled", agent_id, row.pool, {"hostname": req.hostname, "ip": ip, "version": req.version})
        return EnrollResponse(agent_id=agent_id, pool=row.pool,
                              gateway_encryption_public=self.keys.identity.encryption_public,
                              gateway_signing_public=self.keys.identity.signing_public)

    def authenticate(self, method: str, path: str, headers: dict[str, str], body: bytes) -> dict:
        """Verify agent signature, clock skew and nonce freshness; returns the agent row."""
        h = {k.lower(): v for k, v in headers.items()}
        agent_id = h.get(crypto.HEADER_AGENT.lower())
        ts, nonce, sig = (h.get(crypto.HEADER_TS.lower()), h.get(crypto.HEADER_NONCE.lower()),
                          h.get(crypto.HEADER_SIG.lower()))
        if not all((agent_id, ts, nonce, sig)):
            raise GatewayError("missing signature headers", 401)
        try:
            ts_ms = int(ts) * 1000  # type: ignore[arg-type]
        except ValueError:
            raise GatewayError("bad timestamp", 401)
        now = _now()
        if abs(now - ts_ms) > self.max_skew_ms:
            raise GatewayError("request timestamp outside allowed clock skew", 401)
        with self.engine.connect() as conn:
            agent = conn.execute(select(agents).where(agents.c.agent_id == agent_id)).first()
        if not agent or agent.status == "REVOKED":
            raise GatewayError("unknown or revoked agent", 401)
        try:
            crypto.verify_signature(agent.signing_public, method, path, ts, nonce, body, sig)  # type: ignore[arg-type]
        except crypto.CryptoError:
            raise GatewayError("invalid signature", 401)
        try:
            with self.engine.begin() as conn:
                conn.execute(nonces.insert().values(nonce=nonce, agent_id=agent_id, seen_at_ms=now))
                conn.execute(delete(nonces).where(nonces.c.seen_at_ms < now - 2 * self.max_skew_ms))
        except IntegrityError:
            raise GatewayError("replayed request (nonce reuse)", 401)
        return dict(agent._mapping)

    def heartbeat(self, agent: dict, req: HeartbeatRequest, ip: Optional[str]) -> dict:
        now = _now()
        with self.engine.begin() as conn:
            conn.execute(update(agents).where(agents.c.agent_id == agent["agent_id"]).values(
                last_heartbeat_ms=now, last_ip=ip, running_tasks=len(req.running_task_ids),
                status="ONLINE", **({"version": req.version} if req.version else {})))
            if req.running_task_ids:
                conn.execute(update(tasks).where(and_(
                    tasks.c.task_id.in_(req.running_task_ids), tasks.c.agent_id == agent["agent_id"],
                    tasks.c.status.in_(["LEASED", "RUNNING"]))).values(lease_expires_ms=now + self.lease_ms))
            cancelled = [r[0] for r in conn.execute(select(tasks.c.task_id).where(and_(
                tasks.c.agent_id == agent["agent_id"], tasks.c.status == "CANCELLED",
                tasks.c.task_id.in_(req.running_task_ids or [""]))))]
        if agent.get("last_ip") and ip and agent["last_ip"] != ip:
            self._audit("agent.ip_changed", agent["agent_id"], agent["pool"], {"from": agent["last_ip"], "to": ip})
        return {"ok": True, "cancel_task_ids": cancelled, "server_time_ms": now}

    def poll(self, agent: dict) -> Optional[dict]:
        """Lease the oldest runnable task in the agent's pool; returns a sealed envelope."""
        now = _now()
        pool = self.get_pool(agent["pool"])
        with self.engine.begin() as conn:
            # Expired leases go back to the queue (agent crashed or lost connectivity).
            conn.execute(update(tasks).where(and_(
                tasks.c.pool == agent["pool"], tasks.c.status.in_(["LEASED", "RUNNING"]),
                tasks.c.lease_expires_ms < now)).values(status="QUEUED", agent_id=None, run_id=None))
            conn.execute(update(tasks).where(and_(tasks.c.status == "QUEUED",
                                                  tasks.c.attempts >= tasks.c.max_attempts))
                         .values(status="FAILURE", finished_at_ms=now))
            active = conn.execute(select(func.count()).select_from(tasks).where(and_(
                tasks.c.pool == agent["pool"], tasks.c.status.in_(["LEASED", "RUNNING"])))).scalar() or 0
            if active >= pool["max_concurrency"]:
                return None
            candidate = conn.execute(select(tasks).where(and_(
                tasks.c.pool == agent["pool"], tasks.c.status == "QUEUED"))
                .order_by(tasks.c.created_at_ms).limit(1)).first()
            if not candidate:
                return None
            run_id = f"run_{uuid.uuid4().hex[:16]}"
            res = conn.execute(update(tasks).where(and_(
                tasks.c.task_id == candidate.task_id, tasks.c.status == "QUEUED")).values(
                status="LEASED", agent_id=agent["agent_id"], run_id=run_id, attempts=candidate.attempts + 1,
                lease_expires_ms=now + self.lease_ms, started_at_ms=now, last_seq=-1))
            if res.rowcount != 1:
                return None  # another agent won the race
        recipe = dict(candidate.recipe)
        if not pool.get("allow_profiling", True):
            recipe.setdefault("source", {}).setdefault("config", {})["profiling"] = {"enabled": False}
        task = {"task_id": candidate.task_id, "run_id": run_id, "kind": candidate.kind, "recipe": recipe,
                "execution_request_urn": candidate.execution_request_urn, "lease_seconds": self.lease_ms // 1000}
        sealed = crypto.seal(agent["encryption_public"], json.dumps(task).encode(),
                             crypto.aad_for(agent["agent_id"], candidate.task_id))
        if candidate.execution_request_urn:
            self._update_execution_request(candidate.execution_request_urn, "RUNNING", None, now, None)
        return {"task_id": candidate.task_id, "envelope": sealed}

    def _task_for_run(self, agent: dict, run_id: str) -> Any:
        with self.engine.connect() as conn:
            task = conn.execute(select(tasks).where(tasks.c.run_id == run_id)).first()
        if not task or task.agent_id != agent["agent_id"]:
            raise GatewayError("unknown run for this agent", 404)
        if task.status not in ("LEASED", "RUNNING"):
            raise GatewayError(f"run is {task.status}", 409)
        return task

    def ingest_batch(self, agent: dict, run_id: str, seq: int, envelope: dict) -> dict:
        task = self._task_for_run(agent, run_id)
        if seq <= task.last_seq:
            raise GatewayError(f"sequence {seq} not greater than last accepted {task.last_seq}", 409)
        plaintext = crypto.open_sealed(self.keys.identity.encryption_key, envelope,
                                       crypto.aad_for(agent["agent_id"], run_id, str(seq)))
        mcps = json.loads(plaintext)
        if not isinstance(mcps, list):
            raise GatewayError("batch must be a JSON list of MCPs", 400)
        pool = self.get_pool(agent["pool"])
        accepted, rejected = self.filter_mcps(mcps, pool)
        if accepted and self.graph is not None:
            self.graph.emit(accepted)
        with self.engine.begin() as conn:
            conn.execute(update(tasks).where(tasks.c.task_id == task.task_id).values(
                status="RUNNING", last_seq=seq, mcps_forwarded=task.mcps_forwarded + len(accepted),
                mcps_rejected=task.mcps_rejected + len(rejected), lease_expires_ms=_now() + self.lease_ms))
        if rejected:
            self._audit("batch.rejected_mcps", agent["agent_id"], run_id,
                        {"count": len(rejected), "sample": rejected[:5]})
        return {"accepted": len(accepted), "rejected": len(rejected)}

    @staticmethod
    def filter_mcps(mcps: list[dict], pool: dict) -> tuple[list[dict], list[str]]:
        allowed_platforms = set(pool.get("allowed_platforms") or [])
        allowed_aspects = set(pool.get("allowed_aspects") or [])
        accepted, rejected = [], []
        for mcp in mcps:
            urn = str(mcp.get("entityUrn", ""))
            aspect = mcp.get("aspectName")
            reason = None
            if not urn.startswith("urn:li:") or mcp.get("changeType", "UPSERT") not in ("UPSERT", "CREATE", "PATCH"):
                reason = "invalid urn or change type"
            elif urn.split(":", 3)[2] in _PROTECTED_ENTITY_TYPES:
                reason = "identity/security entities may not be written by remote agents"
            elif allowed_aspects and aspect not in allowed_aspects:
                reason = f"aspect {aspect} not allowed"
            elif allowed_platforms:
                platforms = set(_DATASET_PLATFORM.findall(urn))
                if aspect == "dataPlatformInstance":
                    platforms |= set(_DATASET_PLATFORM.findall(json.dumps(aspect_value(mcp))))
                if platforms and not platforms <= allowed_platforms:
                    reason = f"platform(s) {sorted(platforms - allowed_platforms)} not allowed for pool"
            if reason:
                rejected.append(f"{urn}#{aspect}: {reason}")
            else:
                accepted.append(mcp)
        return accepted, rejected

    def append_logs(self, agent: dict, run_id: str, envelope: dict, seq: int) -> None:
        task = self._task_for_run(agent, run_id)
        lines = json.loads(crypto.open_sealed(self.keys.identity.encryption_key, envelope,
                                              crypto.aad_for(agent["agent_id"], run_id, f"log-{seq}")))
        logs = (task.logs or []) + [str(x)[:2000] for x in lines]
        with self.engine.begin() as conn:
            conn.execute(update(tasks).where(tasks.c.task_id == task.task_id).values(logs=logs[-2000:]))

    def complete(self, agent: dict, run_id: str, req: CompleteRequest) -> dict:
        task = self._task_for_run(agent, run_id)
        now = _now()
        status = req.status
        # A run that reports SUCCESS but whose final batches never arrived is a failure.
        if status == "SUCCESS" and req.final_seq != task.last_seq:
            status = "FAILURE"
            req.report["cairn_error"] = f"agent reported final_seq={req.final_seq}, gateway has {task.last_seq}"
        new_status = status
        if status == "FAILURE" and task.attempts < task.max_attempts:
            new_status = "QUEUED"  # retry
        with self.engine.begin() as conn:
            conn.execute(update(tasks).where(tasks.c.task_id == task.task_id).values(
                status=new_status, finished_at_ms=now if new_status != "QUEUED" else None, report=req.report,
                lease_expires_ms=None, **({"agent_id": None, "run_id": None} if new_status == "QUEUED" else {})))
        if task.execution_request_urn and new_status != "QUEUED":
            self._update_execution_request(task.execution_request_urn, status, req.report, task.started_at_ms,
                                           now - (task.started_at_ms or now))
        self._audit("run.completed", agent["agent_id"], run_id,
                    {"status": status, "forwarded": task.mcps_forwarded, "retrying": new_status == "QUEUED"})
        return {"status": new_status}

    def cancel(self, task_id: str, actor: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(tasks).where(and_(tasks.c.task_id == task_id,
                                                  or_(tasks.c.status == "QUEUED", tasks.c.status == "LEASED",
                                                      tasks.c.status == "RUNNING")))
                         .values(status="CANCELLED", finished_at_ms=_now()))
        self._audit("task.cancelled", actor, task_id, {})

    # ---- helpers ---------------------------------------------------------------------
    def _update_execution_request(self, urn: str, status: str, report: Optional[dict], start_ms: Optional[int],
                                  duration_ms: Optional[int]) -> None:
        if self.graph is None:
            return
        result: dict[str, Any] = {"status": status}
        if start_ms:
            result["startTimeMs"] = start_ms
        if duration_ms is not None:
            result["durationMs"] = duration_ms
        if report:
            result["report"] = json.dumps(report)[:100_000]
            result["structuredReport"] = {"type": "CLI_INGEST", "serializedValue": json.dumps(report),
                                          "contentType": "application/json"}
        try:
            self.graph.emit([make_mcp(urn, "dataHubExecutionRequestResult", result)])
        except Exception as e:
            logger.warning("failed to update execution request %s: %s", urn, e)

    def _audit(self, action: str, actor: str, subject: Optional[str], details: dict) -> None:
        if self.audit:
            self.audit.record("agents", action, actor, subject, details)
