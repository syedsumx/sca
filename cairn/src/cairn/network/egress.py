"""Network controls for user code: an admin-set egress allowlist for jobs, notebooks, flows' Python, Spark and dbt
steps, sandboxes and agent apps (``/v1/network/egress-rules``).

A rule allows a **host** (``api.example.com`` or ``*.example.com``) or a **CIDR** (``10.20.0.0/16``), on ``ports``
(80 and 443 when none are given), for one workspace or for all (``workspace: "*"``). A run's rules are the global ones
and those of its owner's workspace (when they belong to exactly one).

Enforced (``CAIRN_EGRESS_ENFORCE=true``) means fail closed: no rule, no egress, except Cairn itself and the
lakehouse.

* **Kubernetes.** User code pods are labelled ``cairn.io/user-code: "true"`` and ``cairn.io/workspace: <id>``. Before
  a run Cairn applies the workspace's NetworkPolicy (``cairn-egress-<workspace>``) in the run's namespace: egress only
  to DNS (kube-dns), Cairn's namespace (``CAIRN_EGRESS_K8S_CAIRN_NAMESPACE``: the API, the lakehouse and the egress
  proxy) and the workspace's CIDR rules on their ports. Host rules need layer 7: the run's ``HTTP(S)_PROXY`` points at
  Cairn's egress proxy (:mod:`cairn.network.proxy`) with a credential carrying the rules.
* **Docker.** The run's container joins only ``CAIRN_EGRESS_DOCKER_NETWORK``, an internal network (no route out) that
  the egress proxy also joins; the proxy is its only way out.
* **Audit.** The proxy logs every refused attempt (``egress.denied``, one JSON line with the workspace, run, client,
  target and reason); a NetworkPolicy drop is the CNI's to log.

Settings: ``CAIRN_EGRESS_ENFORCE``, ``CAIRN_EGRESS_PROXY_URL`` (``http://cairn-egress-proxy:3128``),
``CAIRN_EGRESS_PROXY_KEY`` (or ``…_KEY_FILE``; shared with the proxy, at least 16 bytes), ``CAIRN_EGRESS_DOCKER_NETWORK``,
``CAIRN_EGRESS_K8S_CAIRN_NAMESPACE`` (``cairn``), ``CAIRN_EGRESS_K8S_DNS`` (network policy peers for DNS, JSON).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
import time
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import BigInteger, Column, String, Table, delete, select

from cairn.common.db import JSONText, metadata
from cairn.network import proxy as egress_proxy

logger = logging.getLogger(__name__)
PRIVILEGE = "MANAGE_SYSTEM_OPERATIONS"
HOST = re.compile(r"^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
USER_CODE = "cairn.io/user-code"
WORKSPACE = "cairn.io/workspace"

egress_rules = Table(
    "cairn_egress_rules", metadata,
    Column("name", String(64), primary_key=True),
    Column("workspace", String(64), nullable=False, index=True),
    Column("host", String(255), nullable=True),
    Column("cidr", String(64), nullable=True),
    Column("ports", JSONText, nullable=False),
    Column("description", String(1000), nullable=False),
    Column("updated_by", String(256), nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)


class EgressError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class EgressRule(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    workspace: str = Field(default="*", pattern=r"^(\*|[a-z0-9][a-z0-9-]{1,62})$")
    host: Optional[str] = Field(default=None, max_length=253)
    cidr: Optional[str] = Field(default=None, max_length=64)
    ports: list[int] = Field(default_factory=list, max_length=32)
    description: str = Field(default="", max_length=1000)

    @field_validator("host")
    @classmethod
    def _host(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        v = v.lower().rstrip(".")
        if not HOST.match(v) or v.count("*") > 1:
            raise ValueError("a host name, or *.domain")
        return v

    @field_validator("cidr")
    @classmethod
    def _cidr(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        try:
            net = ipaddress.ip_network(v, strict=False)
        except ValueError:
            raise ValueError("a CIDR, e.g. 10.20.0.0/16")
        if net.prefixlen == 0:
            raise ValueError("0.0.0.0/0 would allow everything: list what user code needs")
        return str(net)

    @field_validator("ports")
    @classmethod
    def _ports(cls, v: list[int]) -> list[int]:
        if any(p < 1 or p > 65535 for p in v):
            raise ValueError("ports are 1-65535")
        return sorted(set(v))

    @model_validator(mode="after")
    def _one(self) -> "EgressRule":
        if bool(self.host) == bool(self.cidr):
            raise ValueError("give a host or a cidr (one of them)")
        return self


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _key_from_env() -> bytes:
    path = os.environ.get("CAIRN_EGRESS_PROXY_KEY_FILE", "").strip()
    if path:
        with open(path, "rb") as fh:
            return fh.read().strip()
    return os.environ.get("CAIRN_EGRESS_PROXY_KEY", "").encode()


class Egress:
    def __init__(self, engine, audit=None, workspaces_of: Optional[Callable[[str], list[str]]] = None,
                 enforce: Optional[bool] = None, proxy: Optional[str] = None, key: Optional[bytes] = None,
                 docker_network: Optional[str] = None, cairn_namespace: Optional[str] = None,
                 dns_peers: Optional[list] = None):
        self.engine, self.audit, self.workspaces_of = engine, audit, workspaces_of
        self.enforced = _env_bool("CAIRN_EGRESS_ENFORCE") if enforce is None else enforce
        self.proxy = (proxy if proxy is not None else os.environ.get("CAIRN_EGRESS_PROXY_URL", "")).strip()
        self.key = key if key is not None else _key_from_env()
        self.docker_network = (docker_network if docker_network is not None
                               else os.environ.get("CAIRN_EGRESS_DOCKER_NETWORK", "")).strip()
        self.cairn_namespace = (cairn_namespace or os.environ.get("CAIRN_EGRESS_K8S_CAIRN_NAMESPACE", "cairn")).strip()
        raw = os.environ.get("CAIRN_EGRESS_K8S_DNS", "")
        self.dns_peers = dns_peers if dns_peers is not None else (json.loads(raw) if raw else [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
            "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}])
        if self.enforced and (not self.proxy or len(self.key) < 16):
            logger.error("egress is enforced but CAIRN_EGRESS_PROXY_URL or CAIRN_EGRESS_PROXY_KEY (16+ bytes) is not "
                         "set: user code gets no egress except Cairn and the lakehouse")

    # ---- rules ---------------------------------------------------------------------------------------
    @staticmethod
    def _out(r: Any) -> dict:
        return {"name": r.name, "workspace": r.workspace, "host": r.host, "cidr": r.cidr, "ports": list(r.ports or []),
                "description": r.description, "updated_by": r.updated_by, "updated_ms": r.updated_ms}

    def list(self, workspace: Optional[str] = None) -> list[dict]:
        q = select(egress_rules).order_by(egress_rules.c.workspace, egress_rules.c.name)
        if workspace:
            q = q.where(egress_rules.c.workspace.in_(["*", workspace]))
        with self.engine.connect() as conn:
            return [self._out(r) for r in conn.execute(q)]

    def get(self, name: str) -> dict:
        with self.engine.connect() as conn:
            r = conn.execute(select(egress_rules).where(egress_rules.c.name == name)).first()
        if r is None:
            raise EgressError(f"no egress rule {name}", 404)
        return self._out(r)

    def put(self, rule: EgressRule, actor: str, create: bool = False) -> dict:
        row = {"workspace": rule.workspace, "host": rule.host, "cidr": rule.cidr, "ports": rule.ports,
               "description": rule.description, "updated_by": actor, "updated_ms": int(time.time() * 1000)}
        with self.engine.begin() as conn:
            exists = conn.execute(select(egress_rules.c.name).where(egress_rules.c.name == rule.name)).first()
            if exists and create:
                raise EgressError(f"egress rule {rule.name} already exists", 409)
            if exists:
                conn.execute(egress_rules.update().where(egress_rules.c.name == rule.name).values(**row))
            else:
                conn.execute(egress_rules.insert().values(name=rule.name, **row))
        self._audit("network.egress_rule_set", actor, rule.name, rule.model_dump())
        return self.get(rule.name)

    def delete(self, name: str, actor: str) -> None:
        with self.engine.begin() as conn:
            n = conn.execute(delete(egress_rules).where(egress_rules.c.name == name)).rowcount
        if not n:
            raise EgressError(f"no egress rule {name}", 404)
        self._audit("network.egress_rule_deleted", actor, name, {})

    # ---- what a run gets -----------------------------------------------------------------------------
    def workspace_of(self, owner: str) -> str:
        """The owner's workspace when they belong to exactly one, else ``*`` (only the global rules)."""
        if self.workspaces_of is None:
            return "*"
        try:
            mine = sorted(set(self.workspaces_of(owner)))
        except Exception:  # noqa: BLE001 - fail closed: the global rules only
            return "*"
        return mine[0] if len(mine) == 1 else "*"

    def effective(self, workspace: str) -> list[dict]:
        return [{k: v for k, v in (("host", r["host"]), ("cidr", r["cidr"]), ("ports", r["ports"])) if v}
                for r in self.list(None if workspace == "*" else workspace) if r["workspace"] in ("*", workspace)]

    def credential(self, workspace: str, run: str, ttl_s: int) -> str:
        if len(self.key) < 16:
            raise EgressError("the egress proxy has no key (CAIRN_EGRESS_PROXY_KEY, at least 16 bytes)", 503)
        return egress_proxy.issue(self.key, workspace, run, self.effective(workspace), ttl_s)

    def proxy_env(self, workspace: str, run: str, ttl_s: int) -> dict[str, str]:
        """``HTTP(S)_PROXY`` for one run (empty when egress is not enforced). Refuses when it is enforced but the
        proxy is not set up: the run would otherwise have no rules applied."""
        if not self.enforced:
            return {}
        if not self.proxy:
            raise EgressError("egress is enforced but no egress proxy is set (CAIRN_EGRESS_PROXY_URL)", 503)
        url = egress_proxy.proxy_url(self.proxy, self.credential(workspace, run, ttl_s))
        return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "http_proxy": url, "https_proxy": url,
                "NO_PROXY": "", "no_proxy": "", "CAIRN_WORKSPACE": "" if workspace == "*" else workspace}

    def check(self, workspace: str, host: str, port: int, always: str = "") -> dict:
        """What the proxy would decide for ``host:port`` now (resolved here)."""
        try:
            addresses = [i[4][0] for i in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
        except OSError:
            addresses = []
        ok, why = egress_proxy.decide(self.effective(workspace), egress_proxy.parse_always(
            always or os.environ.get("CAIRN_EGRESS_ALWAYS", "")), host, port, addresses, _env_bool("CAIRN_EGRESS_ALLOW_PRIVATE"))
        return {"workspace": workspace, "host": host, "port": port, "addresses": addresses, "allowed": ok, "reason": why}

    # ---- Kubernetes ----------------------------------------------------------------------------------
    @staticmethod
    def policy_name(workspace: str) -> str:
        return "cairn-egress-" + ("all" if workspace == "*" else workspace)

    def networkpolicy(self, workspace: str, namespace: str) -> dict:
        """The NetworkPolicy for a workspace's user code pods in ``namespace``."""
        selector = {USER_CODE: "true", WORKSPACE: "all" if workspace == "*" else workspace}
        egress: list[dict] = [
            {"to": self.dns_peers, "ports": [{"port": 53, "protocol": "UDP"}, {"port": 53, "protocol": "TCP"}]},
            {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": self.cairn_namespace}}}]},
        ]
        for r in self.effective(workspace):
            if r.get("cidr"):
                egress.append({"to": [{"ipBlock": {"cidr": r["cidr"]}}],
                               "ports": [{"port": p, "protocol": "TCP"} for p in (r.get("ports") or [80, 443])]})
        return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                "metadata": {"name": self.policy_name(workspace), "namespace": namespace,
                             "labels": {"app.kubernetes.io/managed-by": "cairn", "app.kubernetes.io/part-of": "cairn"}},
                "spec": {"podSelector": {"matchLabels": selector}, "policyTypes": ["Egress"], "egress": egress}}

    def pod_labels(self, workspace: str) -> dict[str, str]:
        return {USER_CODE: "true", WORKSPACE: "all" if workspace == "*" else workspace}

    def contain(self, runner: Any, workspace: str) -> Any:
        """The job runtime with the workspace's egress applied: Kubernetes (labels and NetworkPolicy) or Docker (the
        internal network). A runtime it cannot contain is refused when egress is enforced."""
        if not self.enforced:
            return runner
        import copy

        from cairn.pipelines.jobs import DockerRunner, KubeRunner

        other = copy.copy(runner)
        if isinstance(runner, KubeRunner):
            other.pod_labels = self.pod_labels(workspace)
            pol = self.networkpolicy(workspace, runner.namespace)
            path = f"/apis/networking.k8s.io/v1/namespaces/{runner.namespace}/networkpolicies/{pol['metadata']['name']}"
            if runner._call("GET", path, ok404=True) is None:
                runner._call("POST", f"/apis/networking.k8s.io/v1/namespaces/{runner.namespace}/networkpolicies", pol)
            else:
                runner._call("PUT", path, pol)
            return other
        if isinstance(runner, DockerRunner):
            if not self.docker_network:
                raise EgressError("egress is enforced but CAIRN_EGRESS_DOCKER_NETWORK (an internal network the egress "
                                  "proxy joins) is not set", 503)
            other.network = self.docker_network
            return other
        contain = getattr(runner, "contain_egress", None)     # a data plane enforces its own network rules
        if contain is not None:
            return contain(workspace)
        raise EgressError(f"egress is enforced and {type(runner).__name__} cannot be contained", 503)

    def _audit(self, action: str, actor: str, subject: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("network", action, actor, subject, details)


def build_egress_router(e: Egress, current_principal, authorize) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException
    from pydantic import ValidationError
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/network/egress-rules", tags=["network"])

    async def call(fn, *a):
        try:
            return await run_in_threadpool(fn, *a)
        except EgressError as ex:
            raise HTTPException(ex.status, str(ex))

    def rule(body: dict, name: Optional[str] = None) -> EgressRule:
        try:
            return EgressRule(**{**body, **({"name": name} if name else {})})
        except ValidationError as ex:
            raise HTTPException(422, "; ".join(str(x.get("msg")) for x in ex.errors()))
        except ValueError as ex:
            raise HTTPException(422, str(ex))

    @router.get("")
    async def list_(workspace: Optional[str] = None, p=Depends(current_principal)):
        authorize(p, PRIVILEGE, None)
        rules = await call(e.list, workspace)
        return {"rules": rules, "enforced": e.enforced, "proxy": e.proxy, "docker_network": e.docker_network}

    @router.post("", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, PRIVILEGE, None)
        return await call(e.put, rule(body), p.urn, True)

    @router.get("/effective")
    async def effective(workspace: str = "*", p=Depends(current_principal)):
        authorize(p, PRIVILEGE, None)
        return {"workspace": workspace, "rules": await call(e.effective, workspace)}

    @router.get("/networkpolicy")
    async def networkpolicy(workspace: str = "*", namespace: str = "cairn-jobs", p=Depends(current_principal)):
        authorize(p, PRIVILEGE, None)
        return await call(e.networkpolicy, workspace, namespace)

    @router.post("/check")
    async def check(workspace: str = Body("*"), host: str = Body(..., max_length=253), port: int = Body(443, ge=1, le=65535),
                    p=Depends(current_principal)):
        authorize(p, PRIVILEGE, None)
        return await call(e.check, workspace, host, port)

    @router.get("/{name}")
    async def get(name: str, p=Depends(current_principal)):
        authorize(p, PRIVILEGE, None)
        return await call(e.get, name)

    @router.put("/{name}")
    async def put(name: str, body: dict = Body(...), p=Depends(current_principal)):
        authorize(p, PRIVILEGE, None)
        return await call(e.put, rule(body, name), p.urn)

    @router.delete("/{name}", status_code=204)
    async def drop(name: str, p=Depends(current_principal)):
        authorize(p, PRIVILEGE, None)
        await call(e.delete, name, p.urn)

    return router
