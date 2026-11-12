"""Managed Apache NiFi: governed data connectors (L152).

**Deployments** (``POST /v1/nifi/deployments``, ``CREATE_NIFI_DEPLOYMENT``): a NiFi 2 per team and environment, on
Docker (:mod:`cairn.nifi.docker_runtime`) or Kubernetes (:mod:`cairn.nifi.k8s_runtime`), from the upstream image.
Everything else on ``urn:li:nifiDeployment:<name>`` is authorised there: ``VIEW_NIFI_DEPLOYMENT`` (status, logs, the
canvas read-only), ``DEPLOY_NIFI_FLOW`` (flows on it, changing the canvas) and ``MANAGE_NIFI_DEPLOYMENT`` (scaling,
upgrades, NiFi's controller settings, deletion). Every decision fails closed.

**Nobody reaches NiFi but Cairn.** NiFi serves HTTPS only, on an address only Cairn reaches (Docker: the host's
loopback; Kubernetes: a NetworkPolicy admitting Cairn's namespace), and accepts one user, whose password Cairn derives
and never shows. People open the canvas through Cairn (``/v1/nifi/deployments/<name>/ui/``): Cairn signs them in
(its session), decides the privilege for each request (reads need any of the three; changes ``DEPLOY_NIFI_FLOW``;
NiFi's own settings ``MANAGE_NIFI_DEPLOYMENT``), audits every change, and forwards it with Cairn's NiFi token. NiFi's
sign-in and token endpoints are not reachable through it.

**Flows as code** (``POST /v1/nifi/flows``, ``DEPLOY_NIFI_FLOW`` on the deployment): a NiFi flow definition kept in
Cairn (:mod:`cairn.nifi.flowdef`: secrets in it are refused), deployed by Cairn through NiFi's REST API into a process
group of its own, with a parameter context whose sensitive values come only from secret scopes, read as the flow's
owner when it is deployed. Only the owner changes what a flow does; whoever may manage the deployment may stop or
remove it.

**Landing in the lakehouse** (``sink``): NiFi 2.12's ``PutIcebergRecord`` with its Iceberg REST catalog client against
Cairn Catalog, signed in as the flow's owner with a lakehouse session of theirs (OAuth 2 client credentials: tokens
for an hour, renewed by the client, each renewal checked; storage keys vended per table by the catalog). Cairn refuses
the deploy when the owner may not write the table, rotates the session before it reaches its maximum length and
ends it when the flow goes. Never a store key, never a superuser.

**In Cairn Core** (:mod:`cairn.nifi.core`): each flow is a dataFlow with its owner, team and domain, its process
groups dataJobs with the datasets they read and write; its activity (NiFi's status counters, bulletins) becomes run
events every ``CAIRN_NIFI_SYNC_SECONDS`` (Core's dataProcessInstances, and runs in pipeline observability).
"""

from __future__ import annotations

import json
import logging
import re
import secrets as pysecrets
import threading
import time
import uuid
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, insert, select, update
from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.airflow_deploy.spec import SpecError, check_name
from cairn.common.db import metadata
from cairn.databases.docker_runtime import RuntimeError_
from cairn.nifi import core, flowdef
from cairn.nifi.api import NiFi, NiFiError
from cairn.nifi.keys import Keys
from cairn.nifi.spec import check_deployment, check_flow, check_flow_name, supported_versions

logger = logging.getLogger(__name__)

deployments_table = Table(
    "cairn_nifi_deployments", metadata,
    Column("name", String(31), primary_key=True),
    Column("uid", String(16), nullable=False),
    Column("runtime", String(16), nullable=False),
    Column("spec", Text, nullable=False),
    Column("state", String(16), nullable=False),         # creating ready updating failed deleting
    Column("image", String(255), nullable=True),
    Column("port", Integer, nullable=True),
    Column("ca_pem", Text, nullable=True),               # the deployment's CA: what Cairn trusts (public)
    Column("error", Text, nullable=True),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
flows_table = Table(
    "cairn_nifi_flows", metadata,
    Column("name", String(63), primary_key=True),
    Column("deployment", String(31), nullable=False, index=True),
    Column("owner", String(512), nullable=False),
    Column("spec", Text, nullable=False),                # everything but the definition
    Column("definition", Text, nullable=False),          # NiFi's flow definition, as given (no secrets in it)
    Column("version", Integer, nullable=False),          # changes of the definition or settings
    Column("deployed_version", Integer, nullable=True),
    Column("state", String(16), nullable=False),         # deploying running stopped failed
    Column("group_id", String(64), nullable=True),       # NiFi's process group
    Column("lake_session", String(64), nullable=True),   # the owner's lakehouse session the sink signs in with
    Column("lake_session_s", BigInteger, nullable=True),
    Column("bulletin_cursor", BigInteger, nullable=True),
    Column("synced_ms", BigInteger, nullable=True),
    Column("error", Text, nullable=True),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)

VIEW = ("VIEW_NIFI_DEPLOYMENT", "DEPLOY_NIFI_FLOW", "MANAGE_NIFI_DEPLOYMENT")
READ_METHODS = ("GET", "HEAD", "OPTIONS")
# The parts of a flow a change through the canvas may touch (DEPLOY_NIFI_FLOW, and the flow's owner).
FLOW_PARTS = {"processors", "connections", "controller-services", "input-ports", "output-ports", "funnels", "labels",
              "remote-process-groups", "process-groups", "flowfile-queues", "snippets", "versions", "flow"}
# Reads that hand out what flowed through NiFi, of every flow on it.
DATA_READS = {"provenance", "provenance-events", "data-transfer"}


class NiFiServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def urn(name: str) -> str:
    return f"urn:li:nifiDeployment:{name}"


def flow_resource(name: str) -> str:
    return f"urn:li:nifiFlow:{name}"


class NiFiService:
    def __init__(self, engine, allowed: Callable[[str, str, Optional[str]], bool], audit=None,
                 keys: Optional[Keys] = None, runtime=None, cairn_url: str = "", public_url: str = "",
                 proxy_prefix: str = "/cairn/api", catalog_url: str = "", store_url: str = "", secret_scopes=None,
                 lakehouse_sessions=None, lakehouse=None, graph=None, openlineage=None, pipelines=None,
                 table_urn: Optional[Callable[[str], str]] = None, env: str = "PROD", sync_every_s: int = 300,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.engine, self.allowed, self.audit, self.keys, self.runtime = engine, allowed, audit, keys, runtime
        self.cairn_url, self.public_url = cairn_url.rstrip("/"), public_url.rstrip("/")
        self.proxy_prefix = "/" + proxy_prefix.strip("/") if proxy_prefix.strip("/") else ""
        self.catalog_url, self.store_url = catalog_url.rstrip("/"), store_url.rstrip("/")
        self.secret_scopes, self.lakehouse_sessions, self.lakehouse = secret_scopes, lakehouse_sessions, lakehouse
        self.graph, self.openlineage, self.pipelines = graph, openlineage, pipelines
        self.table_urn = table_urn or (lambda t: f"urn:li:dataset:(urn:li:dataPlatform:iceberg,{t},PROD)")
        self.env, self.sync_every_s, self.clock = env, sync_every_s, clock
        self._clients: dict[str, NiFi] = {}
        self._locks: dict[str, threading.Lock] = {}

    # ---- helpers -------------------------------------------------------------------------------------------------
    def _decide(self, actor: str, privilege: str, target: Optional[str]) -> bool:
        try:
            return bool(self.allowed(actor, privilege, target))
        except Exception:  # noqa: BLE001 - fail closed
            logger.warning("NiFi decision failed (%s %s %s)", actor, privilege, target, exc_info=True)
            return False

    def _need(self, actor: str, privilege: str, target: Optional[str]) -> None:
        if not self._decide(actor, privilege, target):
            raise NiFiServiceError(f"{privilege} is needed" + (f" on {target}" if target else ""), 403)

    def _may(self, actor: str, privileges: tuple[str, ...], target: str) -> bool:
        return any(self._decide(actor, p, target) for p in privileges)

    def _see(self, actor: str, name: str) -> None:
        if not self._may(actor, VIEW, urn(name)):
            raise NiFiServiceError("no such deployment", 404)

    def _audit(self, action: str, actor: str, subject: str, details: dict, conn=None) -> None:
        if self.audit is not None:
            self.audit.record("pipelines", action, actor, subject, details, **({"conn": conn} if conn is not None else {}))

    def _runtime(self):
        if self.runtime is None:
            raise NiFiServiceError("managed NiFi is not enabled here (CAIRN_NIFI_RUNTIME)", 503)
        return self.runtime

    def _rt(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except RuntimeError_ as e:
            raise NiFiServiceError(str(e), e.status) from e
        except NiFiError as e:
            raise NiFiServiceError(str(e), e.status) from e
        except SpecError as e:
            raise NiFiServiceError(str(e), e.status) from e

    def _row(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(deployments_table).where(deployments_table.c.name == name)).first()
        if row is None:
            raise NiFiServiceError("no such deployment", 404)
        return row

    def _set(self, name: str, **values) -> None:
        values["updated_ms"] = self.clock()
        with self.engine.begin() as c:
            c.execute(update(deployments_table).where(deployments_table.c.name == name).values(**values))

    def _claim(self, name: str, to: str, frm: tuple[str, ...] = ("ready",)) -> str:
        row = self._row(name)
        with self.engine.begin() as c:
            n = c.execute(update(deployments_table).where(deployments_table.c.name == name,
                                                          deployments_table.c.state == row.state,
                                                          deployments_table.c.state.in_(frm))
                          .values(state=to, updated_ms=self.clock())).rowcount
        if n != 1:
            raise NiFiServiceError(f"{name} is {self._row(name).state}: try again when it is ready", 409)
        return row.state

    def _context_path(self, name: str) -> str:
        return f"{self.proxy_prefix}/v1/nifi/deployments/{name}/ui"

    def _public_host(self) -> tuple[str, str, int]:
        from urllib.parse import urlsplit

        u = urlsplit(self.public_url or "http://localhost")
        port = u.port or (443 if u.scheme == "https" else 80)
        return u.scheme or "http", u.hostname or "localhost", port

    def _dep(self, row, spec: Optional[dict] = None) -> dict:
        """What the runtime needs: names, settings and files (the keys)."""
        rt = self._runtime()
        spec = spec or json.loads(row.spec)
        dep: dict[str, Any] = {"name": row.name, "uid": row.uid, "spec": spec, "port": row.port}
        scheme, host, port = self._public_host()
        dep["proxy_hosts"] = [*rt.proxy_hosts(dep), host, f"{host}:{port}"]
        dep["context_path"] = self._context_path(row.name)
        dep["files"] = self.keys.files(row.name, row.uid, rt.tls_hosts(dep))
        return dep

    def client(self, row) -> NiFi:
        """Cairn's NiFi client for a deployment (its CA pinned, its single user's password)."""
        key = f"{row.name}:{row.uid}"
        c = self._clients.get(key)
        if c is None:
            if self.keys is None or not row.ca_pem:
                raise NiFiServiceError(f"{row.name} has no keys yet", 409)
            dep = {"name": row.name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec)}
            c = NiFi(self._runtime().api_url(dep), self.keys.secrets(row.name, row.uid)["password"],
                     row.ca_pem.encode())
            self._clients[key] = c
        return c

    def canvas_url(self, name: str) -> str:
        return f"{self.public_url}{self._context_path(name)}/nifi/"

    # ---- deployments ---------------------------------------------------------------------------------------------
    def versions(self) -> tuple[str, ...]:
        pinned = supported_versions()
        offered = getattr(self.runtime, "images", None)
        return tuple(v for v in pinned if not offered or v in offered) or pinned

    def _view(self, row) -> dict:
        spec = json.loads(row.spec)
        with self.engine.connect() as c:
            flows = c.execute(select(flows_table.c.name).where(flows_table.c.deployment == row.name)).all()
        return {"name": row.name, "urn": urn(row.name), "runtime": row.runtime, "state": row.state,
                "image": row.image, "error": row.error, "created_by": row.created_by, "created_ms": row.created_ms,
                "updated_ms": row.updated_ms, "canvas_url": self.canvas_url(row.name) if row.state == "ready" else None,
                "flows": sorted(f.name for f in flows),
                **{k: spec.get(k) for k in ("workspace", "team", "environment", "nifi_version", "resources", "domain")}}

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(deployments_table).order_by(deployments_table.c.name)).all()
        return [self._view(r) for r in rows if self._may(actor, VIEW, urn(r.name))]

    def get(self, actor: str, name: str) -> dict:
        row = self._row(name)
        self._see(actor, name)
        return self._view(row)

    def _ready(self, row) -> Callable[[], bool]:
        def ready() -> bool:
            c = self.client(self._row(row.name))
            c.token(fresh=True)
            c.root()
            return True
        return ready

    def create(self, actor: str, body: dict) -> dict:
        rt = self._runtime()
        name = self._rt(check_name, body.get("name"))
        self._need(actor, "CREATE_NIFI_DEPLOYMENT", None)
        spec = self._rt(check_deployment, body, None, self.versions())
        image = self._rt(rt.image_for, spec["nifi_version"])
        if self.keys is None:
            raise NiFiServiceError("managed NiFi needs CAIRN_NIFI_DEPLOY_KEY", 503)
        uid = pysecrets.token_hex(6)
        now = self.clock()
        port = self._rt(rt.allocate_port) if hasattr(rt, "allocate_port") else None
        with self.engine.begin() as c:
            if c.execute(select(deployments_table.c.name).where(deployments_table.c.name == name)).first():
                raise NiFiServiceError(f"{name} exists", 409)
            c.execute(insert(deployments_table).values(
                name=name, uid=uid, runtime=rt.name, spec=json.dumps(spec), state="creating", image=None, port=port,
                ca_pem=None, error=None, created_by=actor, created_ms=now, updated_ms=now))
            self._audit("nifi_deployment.creating", actor, urn(name), {"runtime": rt.name, **{
                k: spec[k] for k in ("environment", "nifi_version", "team")}}, conn=c)
        try:
            row = self._row(name)
            dep = self._dep(row, spec)
            ca = self.keys.tls(name, uid, rt.tls_hosts(dep))["ca_pem"].decode()
            self._set(name, ca_pem=ca)
            where = self._rt(rt.create, dep, image)
            self._set(name, image=where.get("image"))
            if rt.name == "docker":                         # Kubernetes: ready once its pod is (run_due)
                self._rt(rt.wait_ready, dep, self._ready(row))
                self._set(name, state="ready", error=None)
        except NiFiServiceError as e:
            self._set(name, state="failed", error=str(e)[:2000])
            self._audit("nifi_deployment.failed", actor, urn(name), {"error": str(e)[:300]})
            raise
        self._audit("nifi_deployment.created", actor, urn(name), {"image": image})
        return self.get(actor, name)

    def update(self, actor: str, name: str, body: dict) -> dict:
        """Scaling (resources), upgrades (nifi_version), team and domain. A new size or version makes NiFi again
        from the same volumes: its flows stay."""
        row = self._row(name)
        self._need(actor, "MANAGE_NIFI_DEPLOYMENT", urn(name))
        old = json.loads(row.spec)
        if "environment" in body and body["environment"] != old["environment"]:
            raise NiFiServiceError("a deployment's environment is fixed", 400)
        spec = self._rt(check_deployment, {k: v for k, v in body.items() if k != "name"}, old, self.versions())
        restart = spec["resources"] != old["resources"] or spec["nifi_version"] != old["nifi_version"]
        if not restart:
            self._set(name, spec=json.dumps(spec))
            self._audit("nifi_deployment.updated", actor, urn(name), {k: body[k] for k in body if k != "name"})
            return self.get(actor, name)
        rt = self._runtime()
        self._claim(name, "updating", ("ready", "failed"))
        try:
            dep = self._dep(row, spec)
            where = self._rt(rt.recreate, dep, self._rt(rt.image_for, spec["nifi_version"]))
            self._set(name, spec=json.dumps(spec), image=where.get("image"))
            self._clients.pop(f"{name}:{row.uid}", None)
            if rt.name == "docker":                         # Kubernetes: the pod rolls on its own
                self._rt(rt.wait_ready, dep, self._ready(row))
            self._set(name, state="ready", error=None)
        except NiFiServiceError as e:
            self._set(name, state="failed", error=str(e)[:2000])
            raise
        self._audit("nifi_deployment.scaled", actor, urn(name), {
            "resources": spec["resources"], "nifi_version": spec["nifi_version"], "from": old["nifi_version"]})
        return self.get(actor, name)

    def delete(self, actor: str, name: str) -> None:
        """The deployment, its flows (their lakehouse sessions ended, their Core entities marked removed) and every
        container or the namespace."""
        row = self._row(name)
        self._need(actor, "MANAGE_NIFI_DEPLOYMENT", urn(name))
        self._claim(name, "deleting", ("ready", "failed", "creating", "updating"))
        with self.engine.connect() as c:
            flows = c.execute(select(flows_table).where(flows_table.c.deployment == name)).all()
        for f in flows:
            self._forget_flow(actor, f)
        try:
            self._rt(self._runtime().delete, {"name": name, "uid": row.uid, "spec": json.loads(row.spec)})
        except NiFiServiceError:
            self._set(name, state="failed")
            raise
        self._clients.pop(f"{name}:{row.uid}", None)
        with self.engine.begin() as c:
            c.execute(delete(deployments_table).where(deployments_table.c.name == name))
            self._audit("nifi_deployment.deleted", actor, urn(name), {"flows": [f.name for f in flows]}, conn=c)

    def status(self, actor: str, name: str) -> dict:
        row = self._row(name)
        self._see(actor, name)
        dep = {"name": name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec)}
        out: dict[str, Any] = {"name": name, "state": row.state, "image": row.image, "error": row.error}
        problems: list[str] = []
        try:
            out["runtime_state"] = self._rt(self._runtime().state, dep)
        except NiFiServiceError as e:
            out["runtime_state"] = {"error": str(e)}
            problems.append("the runtime does not answer")
        if row.state != "ready":
            out.update(health=row.state, problems=problems)
            return out
        try:
            c = self.client(row)
            about = c.about()
            out["nifi"] = {"version": about.get("version"), "title": about.get("title")}
            st = c.status(c.root())
            out["counts"] = {k: st.get(k) for k in ("flowFilesIn", "flowFilesOut", "flowFilesReceived",
                                                    "flowFilesSent", "queuedCount", "activeThreadCount")}
            errors = [b for b in c.bulletins(limit=50) if (b.get("bulletin") or {}).get("level") == "ERROR"]
            out["error_bulletins"] = len(errors)
            if errors:
                problems.append(f"{len(errors)} error bulletin(s)")
        except (NiFiError, NiFiServiceError) as e:
            out.update(health="down", problems=[*problems, f"NiFi does not answer: {str(e)[:200]}"])
            return out
        out.update(health="degraded" if problems else "healthy", problems=problems)
        return out

    def logs(self, actor: str, name: str, tail: int = 200) -> dict:
        row = self._row(name)
        self._see(actor, name)
        if not 1 <= int(tail) <= 5000:
            raise NiFiServiceError("tail is 1 to 5000")
        dep = {"name": name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec)}
        text = self._rt(self._runtime().logs, dep, int(tail))
        self._audit("nifi_deployment.logs_read", actor, urn(name), {})
        return {"name": name, "log": text}

    # ---- flows ---------------------------------------------------------------------------------------------------
    def _flow_row(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(flows_table).where(flows_table.c.name == name)).first()
        if row is None:
            raise NiFiServiceError("no such flow", 404)
        return row

    def _set_flow(self, name: str, **values) -> None:
        values["updated_ms"] = self.clock()
        with self.engine.begin() as c:
            c.execute(update(flows_table).where(flows_table.c.name == name).values(**values))

    def _flow_view(self, row, definition: bool = False) -> dict:
        spec = json.loads(row.spec)
        out = {"name": row.name, "urn": core.flow_urn(row.name, self.env), "deployment": row.deployment,
               "owner": row.owner, "state": row.state, "version": row.version,
               "deployed_version": row.deployed_version, "group_id": row.group_id, "error": row.error,
               "synced_ms": row.synced_ms, "created_by": row.created_by, "created_ms": row.created_ms,
               "updated_ms": row.updated_ms, **{k: spec.get(k) for k in (
                   "description", "domain", "parameters", "secrets", "sink", "lineage", "running")}}
        if definition:
            out["definition"] = json.loads(row.definition)
        return out

    def _may_see_flow(self, actor: str, row) -> bool:
        return actor == row.owner or self._may(actor, VIEW, urn(row.deployment))

    def flows(self, actor: str, deployment: Optional[str] = None, full: bool = False) -> list[dict]:
        """The flows the person sees (``full``: with their definitions, as everything as code reads them)."""
        q = select(flows_table).order_by(flows_table.c.name)
        if deployment:
            q = q.where(flows_table.c.deployment == deployment)
        with self.engine.connect() as c:
            rows = c.execute(q).all()
        return [self._flow_view(r, definition=full) for r in rows if self._may_see_flow(actor, r)]

    def flow(self, actor: str, name: str) -> dict:
        row = self._flow_row(name)
        if not self._may_see_flow(actor, row):
            raise NiFiServiceError("no such flow", 404)
        return self._flow_view(row, definition=True)

    def _checked(self, body: dict, base: Optional[dict] = None) -> tuple[dict, dict]:
        spec = self._rt(check_flow, body, base)
        definition = self._rt(flowdef.check_definition, spec.pop("definition"))
        return spec, definition

    def create_flow(self, actor: str, body: dict) -> dict:
        name = self._rt(check_flow_name, body.get("name"))
        spec, definition = self._checked({k: v for k, v in body.items() if k != "name"})
        drow = self._row(spec["deployment"])
        self._need(actor, "DEPLOY_NIFI_FLOW", urn(drow.name))
        if not actor.startswith("urn:li:corpuser:"):
            raise NiFiServiceError("a flow's owner is a person or pipeline user (urn:li:corpuser:...)", 403)
        now = self.clock()
        with self.engine.begin() as c:
            if c.execute(select(flows_table.c.name).where(flows_table.c.name == name)).first():
                raise NiFiServiceError(f"the flow {name} exists", 409)
            c.execute(insert(flows_table).values(
                name=name, deployment=drow.name, owner=actor, spec=json.dumps(spec), definition=json.dumps(definition),
                version=1, deployed_version=None, state="deploying", group_id=None, lake_session=None,
                lake_session_s=None, bulletin_cursor=None, synced_ms=None, error=None, created_by=actor,
                created_ms=now, updated_ms=now))
            self._audit("nifi_flow.created", actor, flow_resource(name), {
                "deployment": drow.name, "sink": spec["sink"], "secrets": [f"{s['scope']}/{s['key']}"
                                                                            for s in spec["secrets"]]}, conn=c)
        try:
            self.deploy(actor, name)
        except NiFiServiceError as e:                      # recorded as failed, with why: the flow stays to be fixed
            if self._flow_row(name).state == "deploying":
                self._set_flow(name, state="failed", error=str(e)[:2000])
        return self.flow(actor, name)

    def update_flow(self, actor: str, name: str, body: dict) -> dict:
        """A new definition or settings, deployed at once. Only the owner changes what a flow does (it lands data as
        them)."""
        row = self._flow_row(name)
        self._need(actor, "DEPLOY_NIFI_FLOW", urn(row.deployment))
        if actor != row.owner:
            raise NiFiServiceError(f"only the flow's owner ({row.owner}) changes it: it lands data as them", 403)
        base = {**json.loads(row.spec), "definition": json.loads(row.definition)}
        spec, definition = self._checked({k: v for k, v in body.items() if k != "name"}, base)
        with self.engine.begin() as c:
            c.execute(update(flows_table).where(flows_table.c.name == name).values(
                spec=json.dumps(spec), definition=json.dumps(definition), version=row.version + 1,
                updated_ms=self.clock()))
            self._audit("nifi_flow.updated", actor, flow_resource(name), {
                "version": row.version + 1, "fields": sorted(k for k in body if k != "name")}, conn=c)
        self.deploy(actor, name)
        return self.flow(actor, name)

    def _secret_values(self, owner: str, spec: dict) -> dict[str, str]:
        refs = spec.get("secrets") or []
        if not refs:
            return {}
        if self.secret_scopes is None or not getattr(self.secret_scopes, "enabled", False):
            raise NiFiServiceError("this flow references secrets, and secret scopes are not configured here "
                                   "(CAIRN_SECRETS_KEY)", 503)
        out = {}
        for ref in refs:
            try:
                out[ref["parameter"]] = self.secret_scopes.read(owner, ref["scope"], ref["key"])
            except Exception as e:  # noqa: BLE001 - fail closed, whatever the reason
                why = str(e) or type(e).__name__
                raise NiFiServiceError(f"secret {ref['scope']}/{ref['key']} cannot be read by the flow's owner "
                                       f"{owner} ({why}): the flow is not deployed without it", 409)
        return out

    def _check_sink(self, owner: str, sink: dict) -> None:
        """The owner may write the table, and read it unrestricted (NiFi loads it to append): else refused now,
        rather than by the catalog at the first record."""
        if self.lakehouse_sessions is None or not self.catalog_url or not self.store_url:
            raise NiFiServiceError("a lakehouse sink needs Cairn Catalog's and Cairn Store's addresses as NiFi reaches "
                                   "them (CAIRN_NIFI_CATALOG_URL, CAIRN_NIFI_STORE_URL) and lakehouse sessions (Trino "
                                   "sign-in, CAIRN_TRINO_ACCESS_KEY)", 503)
        if self.lakehouse is None:
            return                                          # the catalog decides at the first record
        t = {"namespace": sink["namespace"].split("."), "name": sink["table"]}
        try:
            d = (self.lakehouse.decide(owner, [t]).get("tables") or [{}])[0]
        except Exception:  # noqa: BLE001 - fail closed
            logger.warning("lakehouse decision for a NiFi sink failed", exc_info=True)
            d = {}
        table = f"{sink['namespace']}.{sink['table']}"
        if not d.get("write"):
            raise NiFiServiceError(f"the flow's owner {owner} may not write {table}: Cairn's policies decide what "
                                   "the flow may land, as for any of the owner's own writes", 403)
        if d.get("restricted") or not d.get("read"):
            raise NiFiServiceError(f"masks or row filters apply to {owner} on {table}, so its files cannot be handed "
                                   "to NiFi: land it as someone they do not apply to, or into another table", 403)

    def _lake_credential(self, actor: str, row, spec: dict) -> dict[str, str]:
        """The owner's lakehouse session for the sink: a new one each deploy (the previous one ended after the
        switch), as sensitive parameters."""
        out = self.lakehouse_sessions.issue(row.owner)
        sid, secret = out["credential"].split(":", 1)
        self._audit("nifi_flow.lakehouse_session", actor, flow_resource(row.name), {
            "owner": row.owner, "session": sid, "table": f"{spec['sink']['namespace']}.{spec['sink']['table']}"})
        return {"cairn.lakehouse.client_id": sid, "cairn.lakehouse.client_secret": secret}

    def _cairn_params(self, spec: dict) -> dict[str, str]:
        sink = spec.get("sink")
        if not sink:
            return {}
        return {"cairn.lakehouse.catalog_uri": self.catalog_url,
                "cairn.lakehouse.token_uri": f"{self.cairn_url}/v1/lakehouse/oauth/tokens",
                "cairn.lakehouse.store_endpoint": self.store_url,
                "cairn.lakehouse.namespace": sink["namespace"], "cairn.lakehouse.table": sink["table"],
                "cairn.lakehouse.client_id": "", "cairn.lakehouse.client_secret": ""}

    def _end_session(self, actor: str, sid: Optional[str]) -> None:
        if sid and self.lakehouse_sessions is not None and hasattr(self.lakehouse_sessions, "end"):
            try:
                self.lakehouse_sessions.end(sid, actor)
            except Exception:  # noqa: BLE001 - it ends at its maximum length anyway
                logger.warning("could not end lakehouse session %s", sid, exc_info=True)

    def deploy(self, actor: str, name: str) -> dict:
        """The flow into its deployment: its definition (with Cairn's parameter context) uploaded into a process
        group of its own, replacing the one deployed before; sensitive parameters set from secret scopes (read as the
        owner) and, for a lakehouse sink, the owner's lakehouse session; its services enabled and, unless it is
        meant to be stopped, started. Anything that cannot be had: not deployed, and why is recorded."""
        row = self._flow_row(name)
        self._need(actor, "DEPLOY_NIFI_FLOW", urn(row.deployment))
        drow = self._row(row.deployment)
        if drow.state != "ready":
            raise NiFiServiceError(f"{drow.name} is {drow.state}", 409)
        spec, definition = json.loads(row.spec), json.loads(row.definition)
        lock = self._locks.setdefault(name, threading.Lock())
        with lock:
            new_sid: Optional[str] = None
            gid: Optional[str] = None
            nifi: Optional[NiFi] = None
            old_removed = False
            try:
                values = self._secret_values(row.owner, spec)
                cparams = self._cairn_params(spec)
                if spec.get("sink"):
                    self._check_sink(row.owner, spec["sink"])
                prepared = self._rt(flowdef.prepare, definition, name, spec["parameters"],
                                    [s["parameter"] for s in spec["secrets"]], cparams)
                nifi = self.client(drow)
                for old in {row.group_id, (self._rt(nifi.find_group, f"cairn-{name}") or {}).get("id")} - {None}:
                    self._rt(nifi.remove, old)
                    old_removed = True
                if spec.get("sink"):
                    values.update(self._lake_credential(actor, row, spec))
                    new_sid = values["cairn.lakehouse.client_id"]
                entity = self._rt(nifi.upload, prepared, f"cairn-{name}")
                gid = entity["id"]
                self._set_flow(name, group_id=gid)
                ctx = ((entity.get("component") or {}).get("parameterContext") or {}).get("id")
                if values:
                    if not ctx:
                        raise NiFiServiceError("NiFi made the flow's group without its parameter context", 502)
                    self._rt(nifi.set_parameters, ctx, values, True)
                self._rt(nifi.services, gid, "ENABLED")
                invalid = self._rt(nifi.wait_valid, gid)
                if spec.get("running", True):
                    if invalid:
                        raise NiFiServiceError(f"processors are not valid: {' | '.join(invalid)[:1500]}", 409)
                    self._rt(nifi.schedule, gid, "RUNNING")
            except Exception as e:  # noqa: BLE001 - any failure: nothing half-made is left, and why is recorded
                if gid and nifi is not None:
                    try:
                        nifi.remove(gid)
                    except Exception:  # noqa: BLE001
                        logger.warning("flow %s: its failed group %s was left in NiFi", name, gid)
                self._end_session(actor, new_sid)
                if old_removed:                        # the old group is gone: so is the use of its credential
                    self._end_session(actor, row.lake_session)
                # Every failure is logged with its reason (NiFi's own answer included), not only the unexpected
                # ones: the flow's error column is the only other place it is kept.
                logger.warning("flow %s: deploy failed: %s", name, str(e)[:2000] or type(e).__name__,
                               exc_info=not isinstance(e, NiFiServiceError))
                self._set_flow(name, state="failed", error=str(e)[:2000] or type(e).__name__, group_id=None,
                               **({"lake_session": None} if old_removed else {}))
                self._audit("nifi_flow.deploy_failed", actor, flow_resource(name), {"error": str(e)[:300]})
                raise
            old_sid = row.lake_session
            self._set_flow(name, state="running" if spec.get("running", True) else "stopped", error=None,
                           deployed_version=row.version, lake_session=new_sid,
                           lake_session_s=int(self.clock() / 1000) if new_sid else None, synced_ms=self.clock())
            self._end_session(actor, old_sid)
        self._register(name)
        self._audit("nifi_flow.deployed", actor, flow_resource(name), {
            "deployment": drow.name, "version": row.version, "group": gid,
            "sensitive_parameters": sorted(values), "sink": spec.get("sink")})
        return self._flow_view(self._flow_row(name))

    def set_running(self, actor: str, name: str, running: bool) -> dict:
        row = self._flow_row(name)
        if actor != row.owner:
            self._need(actor, "MANAGE_NIFI_DEPLOYMENT", urn(row.deployment))
        else:                                          # an owner whose right was taken away no longer runs it
            self._need(actor, "DEPLOY_NIFI_FLOW", urn(row.deployment))
        if not row.group_id:
            raise NiFiServiceError(f"{name} is not deployed ({row.state})", 409)
        nifi = self.client(self._row(row.deployment))
        if running:
            invalid = self._rt(nifi.invalid, row.group_id)
            if invalid:
                raise NiFiServiceError(f"processors are not valid: {' | '.join(invalid)[:1500]}", 409)
        self._rt(nifi.schedule, row.group_id, "RUNNING" if running else "STOPPED")
        spec = {**json.loads(row.spec), "running": running}
        self._set_flow(name, spec=json.dumps(spec), state="running" if running else "stopped")
        self._audit("nifi_flow.started" if running else "nifi_flow.stopped", actor, flow_resource(name), {})
        return self._flow_view(self._flow_row(name))

    def _forget_flow(self, actor: str, row) -> None:
        if row.group_id:
            try:
                drow = self._row(row.deployment)
                if drow.state in ("ready", "deleting", "updating"):
                    self.client(drow).remove(row.group_id)
            except (NiFiError, NiFiServiceError) as e:
                logger.warning("flow %s: its group was not removed from NiFi: %s", row.name, e)
        self._end_session(actor, row.lake_session)
        if self.graph is not None:
            try:
                core.unregister(self.graph, row.name, list(self._paths(row)), self.env)
            except Exception:  # noqa: BLE001 - the flow goes; its entities are marked when Core answers again
                logger.warning("flow %s: Core did not take its removal", row.name, exc_info=True)
        with self.engine.begin() as c:
            c.execute(delete(flows_table).where(flows_table.c.name == row.name))
            self._audit("nifi_flow.deleted", actor, flow_resource(row.name), {"deployment": row.deployment}, conn=c)

    def delete_flow(self, actor: str, name: str) -> None:
        row = self._flow_row(name)
        if actor != row.owner:
            self._need(actor, "MANAGE_NIFI_DEPLOYMENT", urn(row.deployment))
        else:
            self._need(actor, "DEPLOY_NIFI_FLOW", urn(row.deployment))
        self._forget_flow(actor, row)

    def flow_status(self, actor: str, name: str) -> dict:
        row = self._flow_row(name)
        if not self._may_see_flow(actor, row):
            raise NiFiServiceError("no such flow", 404)
        out: dict[str, Any] = {"name": name, "state": row.state, "error": row.error}
        if not row.group_id:
            return out
        nifi = self.client(self._row(row.deployment))
        st = self._rt(nifi.status, row.group_id)
        out["counts"] = {k: st.get(k) for k in ("flowFilesIn", "flowFilesOut", "flowFilesReceived", "flowFilesSent",
                                                "bytesReceived", "bytesSent", "queuedCount", "activeThreadCount")}
        procs = []
        for snap in _processors(st):
            procs.append({"name": snap.get("name"), "type": snap.get("type"), "run_status": snap.get("runStatus"),
                          "in": snap.get("flowFilesIn"), "out": snap.get("flowFilesOut")})
        out["processors"] = procs
        ids = _group_ids(st) | {row.group_id}
        out["bulletins"] = [_bulletin(b) for b in self._rt(nifi.bulletins, None, 200)
                            if b.get("groupId") in ids][-50:]
        return out

    # ---- Cairn Core ----------------------------------------------------------------------------------------------
    def _paths(self, row) -> dict[tuple[str, ...], dict[str, list[str]]]:
        spec = json.loads(row.spec)
        inferred = flowdef.lineage(json.loads(row.definition), spec.get("parameters") or {})
        return core.groups_io(inferred, spec.get("lineage") or {}, spec.get("sink"),
                              core.Datasets(self.table_urn, self.env))

    def _register(self, name: str) -> None:
        if self.graph is None:
            return
        row = self._flow_row(name)
        drow = self._row(row.deployment)
        dspec, spec = json.loads(drow.spec), json.loads(row.spec)
        try:
            core.register(self.graph, name, row.deployment, row.owner, spec, self._paths(row), dspec.get("team"),
                          spec.get("domain") or dspec.get("domain"),
                          {"cairn.nifi.version": row.deployed_version, "cairn.nifi.group": row.group_id,
                           "cairn.nifi.canvas": self.canvas_url(row.deployment)}, self.env)
        except Exception:  # noqa: BLE001 - deployed; Core gets it at the next sync
            logger.warning("flow %s: Core did not take it", name, exc_info=True)

    def sync(self, name: str) -> Optional[dict]:
        """One window of the flow's activity as a run: NiFi's five-minute counters and the bulletins since the last
        window. A window with nothing in it is no run. Errors in bulletins fail it."""
        row = self._flow_row(name)
        if not row.group_id or row.state not in ("running", "stopped"):
            return None
        drow = self._row(row.deployment)
        if drow.state != "ready":
            return None
        nifi = self.client(drow)
        st = self._rt(nifi.status, row.group_id)
        ids = _group_ids(st) | {row.group_id}
        # NiFi's bulletins are one board for every flow on it: the cursor moves over all of them, a page at a time,
        # so a busy neighbour cannot hold it back and hide this flow's errors.
        bulletins, cursor = [], row.bulletin_cursor if row.bulletin_cursor is not None else -1
        for _ in range(20):
            page = self._rt(nifi.bulletins, cursor if cursor >= 0 else None, 500)
            if not page:
                break
            cursor = max([cursor, *[int(b.get("id") or 0) for b in page]])
            bulletins += [b for b in page if b.get("groupId") in ids]
            if len(page) < 500:
                break
        now = self.clock()
        start = row.synced_ms or now - self.sync_every_s * 1000
        counts = {k: _num(st.get(k)) for k in ("flowFilesIn", "flowFilesOut", "flowFilesReceived", "flowFilesSent",
                                               "bytesIn", "bytesOut", "bytesReceived", "bytesSent", "queuedCount")}
        procs = _processors(st)
        # What the flow's processors did (a group's own in and out count only what crosses its ports).
        counts["processorFlowFilesIn"] = sum(_num(p.get("flowFilesIn")) for p in procs)
        counts["processorFlowFilesOut"] = sum(_num(p.get("flowFilesOut")) for p in procs)
        written = counts["processorFlowFilesIn"] + counts["processorFlowFilesOut"]
        self._set_flow(name, synced_ms=now, bulletin_cursor=cursor if cursor >= 0 else None)
        if not bulletins and not written and not any(counts[k] for k in ("flowFilesReceived", "flowFilesSent")):
            return None
        errors = [b for b in bulletins if (b.get("bulletin") or {}).get("level") == "ERROR"]
        failed = bool(errors)
        error = ((errors[0].get("bulletin") or {}).get("message") or "")[:2000] if errors else ""
        run_id = str(uuid.uuid4())
        spec = json.loads(row.spec)
        ds = core.Datasets(self.table_urn, self.env)
        inferred = flowdef.lineage(json.loads(row.definition), spec.get("parameters") or {})
        ol_io: dict[tuple[str, ...], dict[str, list[dict]]] = {(): {"inputs": [], "outputs": []}}
        for path, io in inferred.items():
            for side in ("inputs", "outputs"):
                items = [x for x in (ds.ol(d) for d in io[side]) if x]
                ol_io[()][side] += items
                if path:
                    ol_io.setdefault(path, {"inputs": [], "outputs": []})[side] += items
        for side in ("inputs", "outputs"):
            ol_io[()][side] += [x for x in (core.ol_dataset_of(u, self.env) for u in (spec.get("lineage") or {})
                                            .get(side, [])) if x]
        if spec.get("sink"):
            x = ds.ol({"namespace": "lakehouse", "name": f"{spec['sink']['namespace']}.{spec['sink']['table']}"})
            if x:
                ol_io[()]["outputs"].append(x)
        for io in ol_io.values():
            for side in ("inputs", "outputs"):
                io[side] = [dict(t) for t in {tuple(sorted(d.items())) for d in io[side]}]
        if self.openlineage is not None:
            names = _group_paths(st, row.group_id)
            bad = {names.get(str(b.get("groupId")), ()) for b in errors} | ({()} if failed else set())
            for ev in core.run_events(name, row.deployment, run_id, start, now, bad, ol_io, error):
                try:
                    self.openlineage.handle(ev, core.ACTOR)
                except Exception:  # noqa: BLE001 - the run is still recorded in observability
                    logger.warning("flow %s: a run event was not taken", name, exc_info=True)
        paths = self._paths(row)
        run = {"run_id": run_id, "state": "failed" if failed else "success", "start_ms": start, "end_ms": now,
               "counts": counts, "bulletins": [_bulletin(b) for b in bulletins[-20:]]}
        if self.pipelines is not None:
            try:
                self.pipelines.record("nifi", name, run_id, run["state"], start_ms=start, end_ms=now,
                                      inputs=paths[()]["inputs"], outputs=paths[()]["outputs"],
                                      error=error or None, detail={"deployment": row.deployment, **counts,
                                                                   "bulletins": run["bulletins"],
                                                                   "owner": row.owner})
            except Exception:  # noqa: BLE001 - recorded in Core already
                logger.warning("flow %s: pipeline observability did not take the run", name, exc_info=True)
        self._audit("nifi_flow.run", core.ACTOR, flow_resource(name), {"run": run_id, "state": run["state"], **counts})
        return run

    def sync_now(self, actor: str, name: str) -> dict:
        row = self._flow_row(name)
        if not self._may_see_flow(actor, row):
            raise NiFiServiceError("no such flow", 404)
        return {"run": self.sync(name)}

    # ---- the scheduler -------------------------------------------------------------------------------------------
    def _rotate(self, row) -> bool:
        """A sink's lakehouse session renewed before it reaches its maximum length (NiFi restarts the catalog
        client with the new credential), the old one ended. Under the flow's lock, as deploy is, on the row as it is
        once the lock is held: a rotation never records a credential a redeploy has just replaced."""
        with self._locks.setdefault(row.name, threading.Lock()):
            try:
                row = self._flow_row(row.name)
            except NiFiServiceError:
                return False
            return self._rotate_locked(row)

    def _rotate_locked(self, row) -> bool:
        spec = json.loads(row.spec)
        if not spec.get("sink") or not row.lake_session or not row.group_id or self.lakehouse_sessions is None:
            return False
        most = int(getattr(self.lakehouse_sessions, "max_session", 7 * 86400))
        if int(self.clock() / 1000) - int(row.lake_session_s or 0) < max(most - 86400, most // 2):
            return False
        drow = self._row(row.deployment)
        nifi = self.client(drow)
        entity = self._rt(nifi.group, row.group_id)
        ctx = ((entity.get("component") or {}).get("parameterContext") or {}).get("id")
        values = self._lake_credential(core.ACTOR, row, spec)
        try:
            self._rt(nifi.set_parameters, ctx, values, True)
        except NiFiServiceError:
            self._end_session(core.ACTOR, values["cairn.lakehouse.client_id"])
            raise
        self._set_flow(row.name, lake_session=values["cairn.lakehouse.client_id"],
                       lake_session_s=int(self.clock() / 1000))
        self._end_session(core.ACTOR, row.lake_session)
        self._audit("nifi_flow.credential_rotated", core.ACTOR, flow_resource(row.name), {})
        return True

    def reconcile(self, name: str) -> bool:
        """Kubernetes: a deployment being made becomes ready once its pod is and Cairn signs in to NiFi."""
        row = self._row(name)
        if row.state != "creating":
            return False
        dep = {"name": name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec)}
        try:
            if not self.runtime.state(dep).get("ready") or not self._ready(row)():
                return False
        except Exception as e:  # noqa: BLE001 - not up yet
            logger.info("NiFi deployment %s: not ready yet (%s)", name, type(e).__name__)
            return False
        self._set(name, state="ready", error=None)
        self._audit("nifi_deployment.ready", core.ACTOR, urn(name), {})
        return True

    def run_due(self) -> int:
        """Deployments the cluster is still making, lakehouse sessions rotated, and each running flow's activity
        synced as runs (every sync_every_s)."""
        if self.runtime is None:
            return 0
        did = 0
        now = self.clock()
        with self.engine.connect() as c:
            making = c.execute(select(deployments_table.c.name).where(deployments_table.c.state == "creating")).all()
        for d in making:
            if getattr(self.runtime, "name", "") == "kubernetes" and self.reconcile(d.name):
                did += 1
        with self.engine.connect() as c:
            rows = c.execute(select(flows_table).where(flows_table.c.state.in_(("running", "stopped")))).all()
        for row in rows:
            try:
                if self._rotate(row):
                    did += 1
                if now - (row.synced_ms or 0) >= self.sync_every_s * 1000:
                    self.sync(row.name)
                    did += 1
            except (NiFiServiceError, NiFiError) as e:
                logger.warning("NiFi flow %s: %s", row.name, e)
            except Exception:  # noqa: BLE001 - one flow's failure never stops the others' syncs and rotations
                logger.exception("NiFi flow %s: sync or rotation failed", row.name)
        return did

    # ---- the canvas, through Cairn -------------------------------------------------------------------------------
    def proxy_privilege(self, method: str, path: str) -> Optional[str]:
        """The privilege a request to NiFi through Cairn needs (None: never forwarded). Decided on whole path
        segments: the parts of a flow (its processors, connections, ports, services, queues, groups) need
        DEPLOY_NIFI_FLOW and, for a change, the flow's owner (see proxy); anything NiFi-wide, and anything not known
        to be part of a flow, needs MANAGE_NIFI_DEPLOYMENT. Reads that hand out what flowed through NiFi
        (provenance, queued content, data transfer) need MANAGE_NIFI_DEPLOYMENT too."""
        segs = path.strip("/").split("/")
        if segs[:1] != ["nifi-api"]:
            return "VIEW" if method in READ_METHODS else None   # the canvas's own files: read only
        top = segs[1] if len(segs) > 1 else ""
        if top == "access":
            return None                                    # NiFi's own sign-in: Cairn signs in, never the browser
        if method in READ_METHODS:
            if top in DATA_READS or (top == "flowfile-queues" and "content" in segs):
                return "MANAGE_NIFI_DEPLOYMENT"
            return "VIEW"
        if top in FLOW_PARTS:
            return "DEPLOY_NIFI_FLOW"
        return "MANAGE_NIFI_DEPLOYMENT"                    # NiFi-wide (controller, tenants, policies, reporting
                                                           # tasks, parameter contexts and providers, ...) or unknown

    def _target_group(self, nifi: NiFi, segs: list[str], body: bytes) -> Optional[str]:
        """The process group a change to a part of a flow lands in (None: not one Cairn can tell)."""
        top = segs[1]
        if top in ("process-groups", "flow", "versions"):
            if "process-groups" in segs[1:]:
                i = segs.index("process-groups", 1)
                gid = segs[i + 1] if len(segs) > i + 1 else ""
                return self._rt(nifi.root) if gid == "root" else (gid or None)
            return None
        if top == "snippets":
            try:
                return str(((json.loads(body or b"{}").get("snippet") or {}).get("parentGroupId")) or "") or None
            except (ValueError, AttributeError):
                return None
        if len(segs) < 3:
            return None
        kind, cid = ("connections", segs[2]) if top == "flowfile-queues" else (top, segs[2])
        got = self._rt(nifi.call, "GET", f"/{kind}/{cid}")
        return str((got.get("component") or {}).get("parentGroupId") or "") or None

    def _owning_flow(self, nifi: NiFi, deployment: str, gid: str):
        """The Cairn flow whose process group holds ``gid`` (None: none does, e.g. the root group)."""
        with self.engine.connect() as c:
            rows = c.execute(select(flows_table).where(flows_table.c.deployment == deployment)).all()
        by_group = {r.group_id: r for r in rows if r.group_id}
        root = self._rt(nifi.root)
        cur: Optional[str] = gid
        for _ in range(64):
            if cur is None or cur == root:
                return None
            if cur in by_group:
                return by_group[cur]
            cur = (self._rt(nifi.group, cur).get("component") or {}).get("parentGroupId")
        return None

    def _may_change_part(self, actor: str, row, nifi: NiFi, method: str, segs: list[str], body: bytes) -> None:
        """A change to a part of a flow: by the flow's owner (with DEPLOY_NIFI_FLOW, already decided), or by someone
        with MANAGE_NIFI_DEPLOYMENT. A part Cairn cannot place in a flow, the root group, or the removal of a flow's
        own group (Cairn's delete does that) needs MANAGE_NIFI_DEPLOYMENT."""
        target = urn(row.name)
        if self._decide(actor, "MANAGE_NIFI_DEPLOYMENT", target):
            return
        gid = self._target_group(nifi, segs, body)
        flow = self._owning_flow(nifi, row.name, gid) if gid else None
        if flow is None:
            raise NiFiServiceError("this part of NiFi is not in a flow of yours: MANAGE_NIFI_DEPLOYMENT is needed", 403)
        if flow.owner != actor:
            raise NiFiServiceError(f"{flow.name} is {flow.owner}'s flow: only its owner changes it", 403)
        if method == "DELETE" and segs[1] == "process-groups" and len(segs) == 3 and gid == flow.group_id:
            raise NiFiServiceError("a flow's own group is removed by deleting the flow in Cairn", 403)

    def proxy(self, actor: str, name: str, method: str, path: str, query: str, headers: dict, body: bytes,
              cookie_auth: bool, same_origin: bool) -> tuple[int, dict, bytes]:
        import httpx

        row = self._row(name)
        if not proxy_path_ok(path):
            # Decided on one path and forwarded to another: "/nifi-api/flow/../controller" would pass as a flow
            # change (DEPLOY_NIFI_FLOW) and reach NiFi's settings (MANAGE_NIFI_DEPLOYMENT). Refused, never resolved.
            raise NiFiServiceError("this path is not forwarded to NiFi", 400)
        need = self.proxy_privilege(method, path)
        target = urn(name)
        if need is None:
            if path.rstrip("/").endswith("access/logout"):
                return 200, {"content-type": "text/plain"}, b""
            if path.strip("/").startswith("nifi-api/access"):
                raise NiFiServiceError("NiFi's own sign-in is not reachable: you are signed in through Cairn", 403)
            raise NiFiServiceError("this path is not forwarded to NiFi", 403)
        if need == "VIEW":
            if not self._may(actor, VIEW, target):
                raise NiFiServiceError("no such deployment", 404)
        else:
            if cookie_auth and not same_origin:
                raise NiFiServiceError("changes to NiFi come from Cairn's own pages (same origin)", 403)
            self._need(actor, need, target)
        if row.state != "ready":
            raise NiFiServiceError(f"{name} is {row.state}", 409)
        nifi = self.client(row)
        if need == "DEPLOY_NIFI_FLOW":
            self._may_change_part(actor, row, nifi, method, path.strip("/").split("/"), body)
        scheme, host, port = self._public_host()
        fwd = {k: v for k, v in headers.items() if k.lower() in (
            "accept", "accept-language", "content-type", "if-none-match", "if-modified-since", "cache-control")}
        fwd.update({"Authorization": f"Bearer {self._rt(nifi.token)}",
                    "X-ProxyScheme": scheme, "X-ProxyHost": host, "X-ProxyPort": str(port),
                    "X-ProxyContextPath": self._context_path(name)})
        url = nifi.base + "/" + path.lstrip("/") + (f"?{query}" if query else "")
        try:
            with httpx.Client(verify=nifi.verify, timeout=120, trust_env=False) as c:
                r = c.request(method, url, content=body or None, headers=fwd)
        except httpx.HTTPError as e:
            raise NiFiServiceError(f"NiFi does not answer: {type(e).__name__}", 503)
        if method not in READ_METHODS:
            self._audit("nifi_deployment.canvas_changed", actor, target, {
                "method": method, "path": "/" + path.lstrip("/")[:300], "status": r.status_code})
        out = {k: v for k, v in r.headers.items() if k.lower() in (
            "content-type", "cache-control", "etag", "last-modified", "location", "content-disposition")}
        return r.status_code, out, r.content


def proxy_path_ok(path: str) -> bool:
    """Whether a path may be forwarded to NiFi as it is. The privilege is decided on this exact path, so it must be
    the path NiFi resolves: no dot or empty segments, no backslashes or control characters, and no percent-encoding
    at all (the router has decoded it once; anything still encoded, "%63ontroller" or "%2e%2e", would be decoded
    again by NiFi into a path other than the one decided)."""
    p = str(path)
    if "%" in p or "\\" in p or any(ord(ch) < 32 or ord(ch) == 127 for ch in p):
        return False
    segs = p.strip("/").split("/")
    return not (any(s in (".", "..") for s in segs) or "//" in p.strip("/"))


def _num(v: Any) -> int:
    """A NiFi counter: a number, or text NiFi formats (``"1,234"``, ``"1,234 (12 MB)"``)."""
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    m = re.match(r"\s*([\d,]+)", str(v or ""))
    return int(m.group(1).replace(",", "")) if m else 0


def _processors(st: dict) -> list[dict]:
    out = [(p.get("processorStatusSnapshot") or p) for p in st.get("processorStatusSnapshots") or []]
    for g in st.get("processGroupStatusSnapshots") or []:
        out += _processors(g.get("processGroupStatusSnapshot") or g)
    return out


def _group_ids(st: dict) -> set[str]:
    out = {st["id"]} if st.get("id") else set()
    for g in st.get("processGroupStatusSnapshots") or []:
        out |= _group_ids(g.get("processGroupStatusSnapshot") or g)
    return out


def _group_paths(st: dict, root: str, path: tuple[str, ...] = ()) -> dict[str, tuple[str, ...]]:
    """NiFi's group ids under the flow's group, as the paths of names Cairn names their jobs by."""
    out = {root: path} if root else {}
    for g in st.get("processGroupStatusSnapshots") or []:
        snap = g.get("processGroupStatusSnapshot") or g
        if snap.get("id"):
            out.update(_group_paths(snap, snap["id"], (*path, str(snap.get("name") or snap["id"]))))
    return out


def _bulletin(b: dict) -> dict:
    x = b.get("bulletin") or {}
    return {"id": b.get("id"), "level": x.get("level"), "source": x.get("sourceName"), "message": (x.get("message") or "")[:1000],
            "timestamp": x.get("timestamp") or b.get("timestamp")}


def build_nifi_router(svc: NiFiService, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/nifi", tags=["nifi"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except NiFiServiceError as e:
            raise HTTPException(e.status, str(e))
        except SpecError as e:
            raise HTTPException(e.status, str(e))

    @router.get("/deployments")
    async def list_(p=Depends(current_principal)):
        return {"deployments": await call(lambda: svc.list(p.urn)), "runtime": getattr(svc.runtime, "name", None),
                "versions": list(svc.versions())}

    @router.post("/deployments", status_code=201)
    async def create(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: svc.create(p.urn, body))

    @router.get("/deployments/{name}")
    async def get(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.get(p.urn, name))

    @router.patch("/deployments/{name}")
    async def patch(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: svc.update(p.urn, name, body))

    @router.delete("/deployments/{name}", status_code=204)
    async def delete_(name: str, p=Depends(current_principal)):
        await call(lambda: svc.delete(p.urn, name))
        return Response(status_code=204)

    @router.get("/deployments/{name}/status")
    async def status(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.status(p.urn, name))

    @router.get("/deployments/{name}/logs")
    async def logs(name: str, tail: int = Query(200), p=Depends(current_principal)):
        return await call(lambda: svc.logs(p.urn, name, tail))

    @router.get("/flow-templates/lakehouse")
    async def template(version: str = Query(""), p=Depends(current_principal)):
        return flowdef.lakehouse_template(version or supported_versions()[0])

    @router.get("/flows")
    async def flows(deployment: Optional[str] = Query(None), full: bool = Query(False), p=Depends(current_principal)):
        return {"flows": await call(lambda: svc.flows(p.urn, deployment, full))}

    @router.post("/flows", status_code=201)
    async def create_flow(body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: svc.create_flow(p.urn, body))

    @router.get("/flows/{name}")
    async def flow(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.flow(p.urn, name))

    @router.put("/flows/{name}")
    async def put_flow(name: str, body: dict = Body(...), p=Depends(current_principal)):
        return await call(lambda: svc.update_flow(p.urn, name, body))

    @router.delete("/flows/{name}", status_code=204)
    async def delete_flow(name: str, p=Depends(current_principal)):
        await call(lambda: svc.delete_flow(p.urn, name))
        return Response(status_code=204)

    @router.post("/flows/{name}/deploy")
    async def deploy(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.deploy(p.urn, name))

    @router.post("/flows/{name}/start")
    async def start(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.set_running(p.urn, name, True))

    @router.post("/flows/{name}/stop")
    async def stop(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.set_running(p.urn, name, False))

    @router.get("/flows/{name}/status")
    async def flow_status(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.flow_status(p.urn, name))

    @router.post("/flows/{name}/sync")
    async def sync(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.sync_now(p.urn, name))

    @router.api_route("/deployments/{name}/ui/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"])
    async def canvas(name: str, path: str, request: Request, p=Depends(current_principal)):
        """NiFi's canvas and API through Cairn: signed in as the person, decided per request, audited."""
        body = await request.body()
        cookie_auth = not request.headers.get("authorization")
        origin = request.headers.get("origin") or ""
        site = request.headers.get("sec-fetch-site") or ""
        same = site in ("same-origin", "none") or (bool(origin) and origin.split("://", 1)[-1] == request.headers.get(
            "host", ""))
        status_, headers, content = await call(lambda: svc.proxy(
            p.urn, name, request.method, path, request.url.query, dict(request.headers), body, cookie_auth, same))
        return Response(content=content, status_code=status_, headers=headers)

    return router
