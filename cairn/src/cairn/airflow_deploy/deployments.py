"""Managed Airflow: deployments per team and environment, run by Cairn and governed by it.

A deployment (``POST /v1/airflow/deployments``, ``CREATE_AIRFLOW_DEPLOYMENT``) is an Airflow (API server, scheduler,
DAG processor, triggerer, its metadata database) on one runtime: Docker for development and single hosts
(``cairn.airflow_deploy.docker_runtime``) or Kubernetes with the official Apache Airflow chart
(``cairn.airflow_deploy.k8s_runtime``). Everything else on ``urn:li:airflowDeployment:<name>`` is authorised there:
``VIEW_AIRFLOW_DEPLOYMENT`` (status, deploys, logs), ``DEPLOY_AIRFLOW`` (deploys, rollbacks, promotions into it),
``MANAGE_AIRFLOW_DEPLOYMENT`` (settings, upgrades, hibernation, deletion) and ``APPROVE_AIRFLOW_PROMOTION``
(promotions that wait for approval).

Each deployment is wired to Cairn when it is made: people sign in through Cairn (an OAuth client of its own, groups
mapped to Airflow roles, no local admin), tasks are its pipeline user (``svc-af-<name>``) by workload identity, the
``cairn`` connection and the Cairn secrets backend, OpenLineage to Cairn, and Airflow's OpenTelemetry when
observability is on. Secrets are references to secret scopes, read as the pipeline user when the deployment starts:
one that cannot be read stops it from starting.

Deploys are recorded (who, when, what, the git commit): **DAG-only** deploys are versioned bundles in Cairn Store,
parsed first (a DAG that does not import is refused) and synced in without a restart; **image** deploys are a team
image built ``FROM cairn/airflow`` (its Airflow must be the deployment's). Any earlier deploy can be rolled back to,
and a deploy is promoted to the next environment (dev → stage → prod), waiting for an approver when the target says
so. An Airflow upgrade parses the deployment's DAGs on the new version first and is refused on an import error.
Non-prod deployments may hibernate outside their hours (everything stopped) and wake when asked.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import secrets as pysecrets
import threading
import time
from typing import Any, Callable, Optional

from sqlalchemy import BigInteger, Column, Integer, String, Table, Text, delete, insert, select, update
from starlette.requests import Request  # module level: FastAPI resolves the handlers' annotations here

from cairn.airflow_deploy import bundle as bundles
from cairn.airflow_deploy.identity import ISSUER_NAME, Keys, client_id, pipeline_user, subject
from cairn.airflow_deploy.otel import airflow_otel_env
from cairn.airflow_deploy.spec import SpecError, awake_now, check_name, check_spec, secret_target, supported_versions
from cairn.common.db import metadata
from cairn.databases.docker_runtime import RuntimeError_

logger = logging.getLogger(__name__)

deployments_table = Table(
    "cairn_airflow_deployments", metadata,
    Column("name", String(31), primary_key=True),
    Column("uid", String(16), nullable=False),
    Column("runtime", String(16), nullable=False),
    Column("spec", Text, nullable=False),
    Column("state", String(16), nullable=False),        # creating ready updating hibernating failed deleting
    Column("image", String(255), nullable=True),        # the image running
    Column("custom_image", String(255), nullable=True),  # a team image deployed (None: the version's own)
    Column("port", Integer, nullable=True),
    Column("url", String(512), nullable=True),
    Column("dags_release", String(40), nullable=True),  # the live DAG deploy
    Column("ops_user", Integer, nullable=True),          # Cairn's operations user in Airflow (status, task logs)
    Column("awake_until_ms", BigInteger, nullable=True),
    Column("token_ms", BigInteger, nullable=True),       # when the workload token was last renewed (Docker)
    Column("error", Text, nullable=True),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
    Column("updated_ms", BigInteger, nullable=False),
)
deploys_table = Table(
    "cairn_airflow_deploys", metadata,
    Column("id", String(24), primary_key=True),
    Column("deployment", String(31), nullable=False, index=True),
    Column("seq", Integer, nullable=False),
    Column("kind", String(8), nullable=False),           # dags | image
    Column("state", String(12), nullable=False),         # live superseded refused failed
    Column("image", String(255), nullable=True),
    Column("bundle_key", String(512), nullable=True),
    Column("sha256", String(64), nullable=True),
    Column("files", Integer, nullable=True),
    Column("bytes", BigInteger, nullable=True),
    Column("git_sha", String(64), nullable=True),
    Column("note", Text, nullable=True),
    Column("source", String(128), nullable=True),        # rollback:<id>, promoted:<deployment>/<id>, git:<folder>
    Column("checked", Text, nullable=True),              # the DAG check: {"dags": [...], "import_errors": {...}}
    Column("error", Text, nullable=True),
    Column("created_by", String(512), nullable=False),
    Column("created_ms", BigInteger, nullable=False),
)
promotions_table = Table(
    "cairn_airflow_promotions", metadata,
    Column("id", String(24), primary_key=True),
    Column("deploy", String(24), nullable=False),
    Column("source", String(31), nullable=False, index=True),
    Column("target", String(31), nullable=False, index=True),
    Column("state", String(12), nullable=False),         # pending applied rejected failed
    Column("requested_by", String(512), nullable=False),
    Column("requested_ms", BigInteger, nullable=False),
    Column("decided_by", String(512), nullable=True),
    Column("decided_ms", BigInteger, nullable=True),
    Column("comment", Text, nullable=True),
    Column("result_deploy", String(24), nullable=True),
    Column("error", Text, nullable=True),
)

FAB = "airflow.providers.fab.auth_manager.fab_auth_manager.FabAuthManager"
WEBSERVER_CONFIG = "from cairn_airflow.auth import configure\n\nconfigure(globals())\n"
TOKEN_PATH = "/opt/cairn/identity/token"
CA_PATH = "/opt/cairn/ca.crt"
TOKEN_RENEW_MS = 20 * 60 * 1000
IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9./_:@-]{0,254}$")


class DeploymentError(Exception):
    def __init__(self, message: str, status: int = 400, detail: Any = None):
        super().__init__(message)
        self.status, self.detail = status, detail


def urn(name: str) -> str:
    return f"urn:li:airflowDeployment:{name}"


class DeployStore:
    """Cairn Store, where DAG bundles are kept (``<bucket>/<deployment>/<uid>/dags/<deploy>.tar.gz``)."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "us-east-1"):
        self.endpoint, self.bucket, self.ak, self.sk, self.region = endpoint.rstrip("/"), bucket, access_key, secret_key, region

    def _req(self, method: str, key: str = "", body: bytes = b"", ok=(200, 204), query: Optional[dict] = None):
        import httpx
        from urllib.parse import quote, urlencode

        from cairn.storage.store_admin import sigv4_headers

        url = f"{self.endpoint}/{self.bucket}" + (f"/{quote(key, safe='/-_.~')}" if key else "")
        if query:
            url += "?" + urlencode(sorted(query.items()), quote_via=quote)
        try:
            r = httpx.request(method, url, content=body or None, timeout=120, trust_env=False,
                              headers=sigv4_headers(method, url, body, self.ak, self.sk, self.region))
        except httpx.HTTPError as e:
            raise DeploymentError(f"Cairn Store is not reachable: {type(e).__name__}", 502) from e
        if r.status_code not in ok:
            raise DeploymentError(f"Cairn Store: {method} {key}: {r.status_code}", 502)
        return r

    def ensure_bucket(self) -> None:
        if self._req("HEAD", ok=(200, 404)).status_code == 404:
            self._req("PUT", ok=(200, 409))

    def put(self, key: str, data: bytes) -> None:
        self._req("PUT", key, data, ok=(200, 201, 204))

    def get(self, key: str) -> bytes:
        r = self._req("GET", key, ok=(200, 404))
        if r.status_code == 404:
            raise DeploymentError(f"the bundle {key} is not in the store any more", 410)
        return r.content

    def delete_prefix(self, prefix: str) -> None:
        text = self._req("GET", query={"list-type": "2", "prefix": prefix, "max-keys": "1000"}).text
        for key in re.findall(r"<Key>([^<]+)</Key>", text):
            self._req("DELETE", key, ok=(200, 204, 404))


class AirflowDeployments:
    def __init__(self, engine, allowed: Callable[[str, str, Optional[str]], bool], audit=None,
                 keys: Optional[Keys] = None, runtime=None, store: Optional[DeployStore] = None,
                 cairn_url: str = "", public_url: str = "", trino: Optional[dict] = None, ca_pem: bytes = b"",
                 secret_scopes=None, oauth=None, workload=None, k8s_issuer: str = "", git_folders=None,
                 self_approve: bool = False, clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.engine, self.allowed, self.audit, self.keys, self.runtime = engine, allowed, audit, keys, runtime
        self.store, self.cairn_url, self.public_url = store, cairn_url.rstrip("/"), public_url.rstrip("/")
        self.trino = trino or {}
        self.ca_pem, self.secret_scopes, self.oauth, self.workload = ca_pem, secret_scopes, oauth, workload
        self.k8s_issuer, self.git_folders, self.self_approve, self.clock = k8s_issuer, git_folders, self_approve, clock
        self._locks: dict[str, threading.Lock] = {}
        if oauth is not None:
            oauth.client_source = self.oauth_client

    # ---- helpers ---------------------------------------------------------------------------------------------
    def _need(self, actor: str, privilege: str, target: Optional[str]) -> None:
        try:
            ok = self.allowed(actor, privilege, target)
        except Exception:  # noqa: BLE001 - fail closed
            logger.warning("deployment decision failed (%s %s %s)", actor, privilege, target, exc_info=True)
            ok = False
        if not ok:
            raise DeploymentError(f"{privilege} is needed" + (f" on {target}" if target else ""), 403)

    def _may(self, actor: str, privileges: tuple[str, ...], target: str) -> bool:
        for p in privileges:
            try:
                if self.allowed(actor, p, target):
                    return True
            except Exception:  # noqa: BLE001 - fail closed
                logger.warning("deployment decision failed (%s %s %s)", actor, p, target, exc_info=True)
        return False

    def _see(self, actor: str, name: str) -> None:
        if not self._may(actor, ("VIEW_AIRFLOW_DEPLOYMENT", "DEPLOY_AIRFLOW", "MANAGE_AIRFLOW_DEPLOYMENT"), urn(name)):
            raise DeploymentError("no such deployment", 404)

    def _audit(self, action: str, actor: str, name: str, details: dict) -> None:
        if self.audit is not None:
            self.audit.record("pipelines", action, actor, urn(name), details)

    def _runtime(self):
        if self.runtime is None:
            raise DeploymentError("managed Airflow is not enabled here (CAIRN_AIRFLOW_RUNTIME)", 503)
        return self.runtime

    def _call(self, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except RuntimeError_ as e:
            raise DeploymentError(str(e), e.status) from e
        except (SpecError, bundles.BundleError) as e:
            raise DeploymentError(str(e), e.status) from e

    def _row(self, name: str):
        with self.engine.connect() as c:
            row = c.execute(select(deployments_table).where(deployments_table.c.name == name)).first()
        if row is None:
            raise DeploymentError("no such deployment", 404)
        return row

    def _set(self, name: str, **values) -> None:
        values["updated_ms"] = self.clock()
        with self.engine.begin() as c:
            c.execute(update(deployments_table).where(deployments_table.c.name == name).values(**values))

    def _claim(self, name: str, to: str, frm: tuple[str, ...] = ("ready",)) -> str:
        """Moves the deployment to ``to`` if it is in one of ``frm`` (one change at a time, across replicas): the
        state it was in."""
        row = self._row(name)
        with self.engine.begin() as c:
            n = c.execute(update(deployments_table).where(deployments_table.c.name == name,
                                                          deployments_table.c.state == row.state,
                                                          deployments_table.c.state.in_(frm))
                          .values(state=to, updated_ms=self.clock())).rowcount
        if n != 1:
            raise DeploymentError(f"{name} is {self._row(name).state}: try again when it is ready", 409)
        return row.state

    def _new_id(self) -> str:
        return pysecrets.token_hex(8)

    # ---- what a deployment is made of ------------------------------------------------------------------------
    def oauth_client(self, cid: str):
        """The OAuth client of a deployment (``airflow-<name>``), for Cairn's authorization server."""
        from cairn.dataaccess.trino_oauth import Client

        if not cid.startswith("airflow-") or self.keys is None:
            return None
        name = cid[len("airflow-"):]
        try:
            row = self._row(name)
        except DeploymentError:
            return None
        if row.state in ("deleting", "failed") or not row.url:
            return None
        return Client(self.keys.secrets(name, row.uid)["oauth_secret"], (f"{row.url}/auth/oauth-authorized/cairn",), cid)

    def _secrets(self, name: str, spec: dict) -> dict[str, str]:
        """The secret references' values, read as the deployment's pipeline user. Any that cannot be read: refused
        (the deployment does not start)."""
        refs = spec.get("secrets") or []
        if not refs:
            return {}
        if self.secret_scopes is None or not getattr(self.secret_scopes, "enabled", False):
            raise DeploymentError("this deployment references secrets, and secret scopes are not configured here "
                                  "(CAIRN_SECRETS_KEY)", 503)
        out = {}
        who = pipeline_user(name)
        for ref in refs:
            try:
                out[secret_target(ref)] = self.secret_scopes.read(who, ref["scope"], ref["key"])
            except Exception as e:  # noqa: BLE001 - fail closed, whatever the reason
                why = str(e) or type(e).__name__
                raise DeploymentError(f"secret {ref['scope']}/{ref['key']} cannot be read by {who} ({why}): grant "
                                      f"it READ on the scope; the deployment does not start without it", 409)
        return out

    def _executor(self, spec: dict) -> str:
        rt = self._runtime()
        ex = spec.get("executor") or rt.default_executor
        if ex not in rt.executors:
            raise DeploymentError(f"{ex} does not run on the {rt.name} runtime ({', '.join(rt.executors)})", 400)
        return ex

    def _dep(self, row, spec: Optional[dict] = None, with_secrets: bool = True) -> dict:
        """What the runtime needs: names, settings (secrets included) and files."""
        rt = self._runtime()
        spec = spec or json.loads(row.spec)
        name = row.name
        s = self.keys.secrets(name, row.uid)
        dep: dict[str, Any] = {"name": name, "uid": row.uid, "spec": spec, "port": row.port,
                               "db_password": s["db_password"], "secrets": s}
        public = row.url or rt.public_url(dep)
        dep["url"] = public
        conn_extra = {"workload_token_file": TOKEN_PATH, "trino_host": self.trino.get("host", "cairn-trino"),
                      "trino_port": int(self.trino.get("port", 8443)),
                      "trino_catalog": self.trino.get("catalog", "lakehouse")}
        if self.ca_pem:
            conn_extra["ca_file"] = CA_PATH
        backend = {"cairn_url": self.cairn_url, **conn_extra}
        lineage = {"type": "http", "url": self.cairn_url, "endpoint": "openlineage/api/v1/lineage",
                   "auth": {"type": "cairn_airflow.lineage.CairnTokenProvider", "workload_token_file": TOKEN_PATH,
                            "cairn_url": self.cairn_url, **({"ca_file": CA_PATH} if self.ca_pem else {})}}
        env = {
            "AIRFLOW__CORE__EXECUTOR": self._executor(spec),
            "AIRFLOW__CORE__FERNET_KEY": s["fernet_key"],
            "AIRFLOW__API_AUTH__JWT_SECRET": s["jwt_secret"],
            "AIRFLOW__API_AUTH__JWT_AUDIENCE": f"cairn-af-{name}",
            "AIRFLOW__API__SECRET_KEY": s["api_secret"],
            "AIRFLOW__API__BASE_URL": public,
            "AIRFLOW__CORE__AUTH_MANAGER": FAB,
            "AIRFLOW__FAB__CONFIG_FILE": "/opt/cairn/webserver_config.py",
            "AIRFLOW__CORE__LOAD_EXAMPLES": "false",
            "AIRFLOW__CORE__DAGS_FOLDER": "/opt/airflow/dags/current",
            "AIRFLOW__DAG_PROCESSOR__REFRESH_INTERVAL": "10",
            "AIRFLOW__DAG_PROCESSOR__MIN_FILE_PROCESS_INTERVAL": "10",
            "AIRFLOW__SECRETS__BACKEND": "cairn_airflow.secrets.CairnSecretsBackend",
            "AIRFLOW__SECRETS__BACKEND_KWARGS": json.dumps(backend),
            "AIRFLOW_CONN_CAIRN_DEFAULT": json.dumps({"conn_type": "cairn", "host": self.cairn_url,
                                                      "extra": conn_extra}),
            "AIRFLOW__OPENLINEAGE__TRANSPORT": json.dumps(lineage),
            "CAIRN_URL": self.cairn_url,
            "CAIRN_PUBLIC_URL": self.public_url,
            "CAIRN_AIRFLOW_OAUTH_SECRET": s["oauth_secret"],
            "CAIRN_AIRFLOW_CLIENT_ID": client_id(name),
            "CAIRN_AIRFLOW_AUDIENCE": client_id(name),
            "CAIRN_AIRFLOW_ROLES": json.dumps({g: [r] for g, r in (spec.get("roles") or {}).items()}),
            "CAIRN_AIRFLOW_DEPLOYMENT": name,
            "CAIRN_AIRFLOW_ENVIRONMENT": spec["environment"],
            "CAIRN_WORKLOAD_TOKEN_FILE": TOKEN_PATH,
            "PYTHONWARNINGS": "ignore::DeprecationWarning",
        }
        if not self.public_url.startswith("https://"):
            env["CAIRN_AIRFLOW_ALLOW_HTTP"] = "true"
        if self.ca_pem:
            env["CAIRN_CA_FILE"] = CA_PATH
        try:
            env.update(airflow_otel_env(name, spec["environment"]))
        except ValueError as e:
            raise DeploymentError(str(e), 503)
        env.update(spec.get("env") or {})
        if hasattr(rt, "db_url"):
            env["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] = rt.db_url(dep)
        if hasattr(rt, "internal_api_url"):
            env["AIRFLOW__CORE__EXECUTION_API_SERVER_URL"] = rt.internal_api_url(dep) + "/execution/"
        if with_secrets:
            env.update(self._secrets(name, spec))
        dep["env"] = env
        files: dict[str, tuple[Any, int]] = {"opt/cairn/webserver_config.py": (WEBSERVER_CONFIG, 0o444)}
        if self.ca_pem:
            files["opt/cairn/ca.crt"] = (self.ca_pem, 0o444)
        if rt.name == "docker":
            files["opt/cairn/identity/token"] = (self.keys.workload_token(name), 0o400)
        dep["files"] = files
        return dep

    def _image(self, row, spec: dict) -> str:
        return row.custom_image or self._call(self._runtime().image_for, spec["airflow_version"])

    # ---- identity --------------------------------------------------------------------------------------------
    def _bind(self, actor: str, name: str) -> None:
        if self.workload is None:
            raise DeploymentError("workload identity is not available (Trino sign-in, CAIRN_TRINO_ACCESS_KEY)", 503)
        rt = self._runtime()
        if rt.name == "docker":
            issuer, subs = ISSUER_NAME, [subject(name)]
        else:
            if not self.k8s_issuer or self.k8s_issuer not in self.workload.issuers:
                raise DeploymentError("the cluster's workload issuer is not set (CAIRN_AIRFLOW_K8S_WORKLOAD_ISSUER, "
                                      "one of CAIRN_WORKLOAD_ISSUERS)", 503)
            issuer, subs = self.k8s_issuer, [rt.service_account_subject(name)]
        have = {(b["subject"]) for b in self.workload.bindings(issuer) if b["user"] == pipeline_user(name)}
        for sub in subs:
            if sub not in have:
                try:
                    self.workload.bind(issuer, sub, pipeline_user(name), actor)
                except ValueError as e:
                    if "already bound" not in str(e):
                        raise DeploymentError(str(e), 409)

    def _unbind(self, actor: str, name: str) -> None:
        if self.workload is None:
            return
        for b in self.workload.bindings():
            if b["user"] == pipeline_user(name):
                self.workload.unbind(b["id"], actor)

    # ---- the record ------------------------------------------------------------------------------------------
    def _view(self, row) -> dict:
        spec = json.loads(row.spec)
        return {"name": row.name, "urn": urn(row.name), "runtime": row.runtime, "state": row.state,
                "url": row.url, "image": row.image, "custom_image": row.custom_image,
                "dags_release": row.dags_release, "pipeline_user": pipeline_user(row.name),
                "oauth_client": client_id(row.name), "error": row.error, "created_by": row.created_by,
                "created_ms": row.created_ms, "updated_ms": row.updated_ms,
                "awake_until_ms": row.awake_until_ms, **{k: spec.get(k) for k in (
                    "workspace", "team", "environment", "airflow_version", "executor", "resources", "dag_source",
                    "env", "secrets", "hibernation", "roles", "promotes_to", "approval")}}

    def list(self, actor: str) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(select(deployments_table).order_by(deployments_table.c.name)).all()
        return [self._view(r) for r in rows
                if self._may(actor, ("VIEW_AIRFLOW_DEPLOYMENT", "DEPLOY_AIRFLOW", "MANAGE_AIRFLOW_DEPLOYMENT"), urn(r.name))]

    def get(self, actor: str, name: str) -> dict:
        row = self._row(name)
        self._see(actor, name)
        if row.state == "creating" and getattr(self.runtime, "name", "") == "kubernetes":
            self.reconcile(name)
            row = self._row(name)
        return self._view(row)

    # ---- create, change, delete ------------------------------------------------------------------------------
    def create(self, actor: str, body: dict) -> dict:
        rt = self._runtime()
        name = self._call(check_name, body.get("name"))
        self._need(actor, "CREATE_AIRFLOW_DEPLOYMENT", None)
        spec = self._call(check_spec, body, None, self.versions())
        self._executor(spec)
        image = self._call(rt.image_for, spec["airflow_version"])
        if self.oauth is None:
            raise DeploymentError("people sign in to managed Airflow through Cairn: turn on Cairn's sign-in for "
                                  "engines (CAIRN_TRINO_ACCESS_KEY)", 503)
        if self.keys is None:
            raise DeploymentError("managed Airflow needs CAIRN_AIRFLOW_DEPLOY_KEY", 503)
        self._check_promotes_to(name, spec)
        # Secrets first: a deployment whose secrets cannot be had is not made at all.
        self._secrets(name, spec)
        if self.store is not None:
            self.store.ensure_bucket()
        uid = pysecrets.token_hex(6)
        now = self.clock()
        port = self._call(rt.allocate_port) if hasattr(rt, "allocate_port") else None
        with self.engine.begin() as c:
            if c.execute(select(deployments_table.c.name).where(deployments_table.c.name == name)).first():
                raise DeploymentError(f"{name} exists", 409)
            c.execute(insert(deployments_table).values(
                name=name, uid=uid, runtime=rt.name, spec=json.dumps(spec), state="creating", image=None,
                custom_image=None, port=port, url=None, dags_release=None, ops_user=None, awake_until_ms=None,
                token_ms=now, error=None, created_by=actor, created_ms=now, updated_ms=now))
        try:
            self._set(name, url=rt.public_url({"name": name, "port": port, "spec": spec}))
            self._bind(actor, name)
            dep = self._dep(self._row(name), spec)
            where = self._call(rt.create, dep, image)
            self._set(name, image=where.get("image"), ops_user=where.get("ops_user"))
            if rt.name == "docker":
                self._call(rt.wait_ready, dep)
                self._set(name, state="ready", error=None)
        except DeploymentError as e:
            self._set(name, state="failed", error=str(e)[:2000])
            self._audit("airflow_deployment.failed", actor, name, {"error": str(e)[:300]})
            raise
        self._audit("airflow_deployment.created", actor, name, {
            "runtime": rt.name, "environment": spec["environment"], "airflow_version": spec["airflow_version"],
            "pipeline_user": pipeline_user(name), "secrets": [f"{r['scope']}/{r['key']}" for r in spec["secrets"]]})
        return self.get(actor, name)

    def versions(self) -> tuple[str, ...]:
        rt = self.runtime
        pinned = supported_versions()
        offered = getattr(rt, "images", None)
        return tuple(v for v in pinned if not offered or v in offered) or pinned

    def _check_promotes_to(self, name: str, spec: dict) -> None:
        to = spec.get("promotes_to")
        if to and to == name:
            raise DeploymentError("a deployment does not promote to itself")

    def update(self, actor: str, name: str, body: dict) -> dict:
        """Settings (env, secrets, roles, resources, executor, hibernation, promotion) and Airflow upgrades. What
        changes Airflow's settings restarts its components; an upgrade parses the DAGs on the new version first."""
        row = self._row(name)
        self._need(actor, "MANAGE_AIRFLOW_DEPLOYMENT", urn(name))
        old = json.loads(row.spec)
        if "environment" in body and body["environment"] != old["environment"]:
            raise DeploymentError("a deployment's environment is fixed: make another and promote to it", 400)
        spec = self._call(check_spec, {k: v for k, v in body.items() if k != "name"}, old, self.versions())
        self._executor(spec)
        self._check_promotes_to(name, spec)
        upgrade = spec["airflow_version"] != old["airflow_version"]
        restart = upgrade or any(spec.get(k) != old.get(k) for k in ("env", "secrets", "roles", "resources",
                                                                   "executor", "dag_source"))
        if not restart:
            self._set(name, spec=json.dumps(spec))
            self._audit("airflow_deployment.updated", actor, name, {k: body[k] for k in body if k != "name"})
            return self.get(actor, name)
        if upgrade and row.custom_image:
            raise DeploymentError(f"{name} runs the team image {row.custom_image}: deploy an image built FROM "
                                  f"cairn/airflow:{spec['airflow_version']} (cairn airflow deploy) to upgrade", 409)
        prior = self._claim(name, "updating", ("ready", "hibernating"))
        try:
            if prior == "hibernating":
                self._secrets(name, spec)                 # checked now, applied at wake
                self._set(name, spec=json.dumps(spec), state="hibernating")
                self._audit("airflow_deployment.updated", actor, name, {k: body[k] for k in body if k != "name"})
                return self.get(actor, name)
            rt = self._runtime()
            dep = self._dep(row, spec)
            image = self._call(rt.image_for, spec["airflow_version"]) if upgrade else self._image(row, spec)
            if upgrade:
                check = self._precheck(row, dep, image)
                if check["import_errors"]:
                    self._audit("airflow_deployment.upgrade_refused", actor, name, {
                        "to": spec["airflow_version"], "import_errors": sorted(check["import_errors"])})
                    raise DeploymentError(f"the DAGs of {name} do not parse on Airflow {spec['airflow_version']}: "
                                          f"{', '.join(sorted(check['import_errors']))}", 409, check)
            where = self._call(rt.recreate, dep, image, upgrade)
            if rt.name == "docker":                       # Kubernetes: the pods roll on their own
                self._call(rt.wait_ready, dep)
            self._set(name, spec=json.dumps(spec), image=where.get("image"), error=None,
                      **({"ops_user": where["ops_user"]} if where.get("ops_user") else {}))
            self._resync(name)
        finally:
            r = self._row(name)
            if r.state == "updating":
                self._set(name, state="ready")
        self._audit("airflow_deployment.upgraded" if upgrade else "airflow_deployment.updated", actor, name,
                    {**{k: body[k] for k in body if k != "name"}, **({"from": old["airflow_version"]} if upgrade else {})})
        return self.get(actor, name)

    def delete(self, actor: str, name: str) -> None:
        row = self._row(name)
        self._need(actor, "MANAGE_AIRFLOW_DEPLOYMENT", urn(name))
        self._claim(name, "deleting", ("ready", "failed", "creating", "updating", "hibernating"))
        try:
            self._call(self._runtime().delete, {"name": name, "uid": row.uid, "spec": json.loads(row.spec)})
        except DeploymentError:
            self._set(name, state="failed")
            raise
        self._unbind(actor, name)
        if self.store is not None:
            try:
                self.store.delete_prefix(f"{name}/{row.uid}/")
            except DeploymentError as e:
                logger.warning("deployment %s: bundles left in the store: %s", name, e)
        with self.engine.begin() as c:
            c.execute(delete(deploys_table).where(deploys_table.c.deployment == name))
            c.execute(delete(promotions_table).where((promotions_table.c.target == name) |
                                                     (promotions_table.c.source == name)))
            c.execute(delete(deployments_table).where(deployments_table.c.name == name))
        self._audit("airflow_deployment.deleted", actor, name, {})

    # ---- deploys ---------------------------------------------------------------------------------------------
    def _deploy_view(self, r) -> dict:
        return {"id": r.id, "deployment": r.deployment, "seq": r.seq, "kind": r.kind, "state": r.state,
                "image": r.image, "sha256": r.sha256, "files": r.files, "bytes": r.bytes, "git_sha": r.git_sha,
                "note": r.note, "source": r.source, "checked": json.loads(r.checked) if r.checked else None,
                "error": r.error, "created_by": r.created_by, "created_ms": r.created_ms}

    def deploys(self, actor: str, name: str) -> list[dict]:
        self._row(name)
        self._see(actor, name)
        with self.engine.connect() as c:
            rows = c.execute(select(deploys_table).where(deploys_table.c.deployment == name)
                             .order_by(deploys_table.c.seq.desc())).all()
        return [self._deploy_view(r) for r in rows]

    def _deploy_row(self, name: str, did: str):
        with self.engine.connect() as c:
            r = c.execute(select(deploys_table).where(deploys_table.c.id == did, deploys_table.c.deployment == name)).first()
        if r is None:
            raise DeploymentError("no such deploy of that deployment", 404)
        return r

    def _record(self, name: str, actor: str, **values) -> str:
        did = self._new_id()
        with self.engine.begin() as c:
            seq = (c.execute(select(deploys_table.c.seq).where(deploys_table.c.deployment == name)
                             .order_by(deploys_table.c.seq.desc())).scalar() or 0) + 1
            row = {"image": None, "bundle_key": None, "sha256": None, "files": None, "bytes": None, "git_sha": None,
                   "note": None, "source": None, "checked": None, "error": None, **values}
            c.execute(insert(deploys_table).values(id=did, deployment=name, seq=seq, created_by=actor,
                                                   created_ms=self.clock(), **row))
        return did

    def _supersede(self, name: str, kind: str, keep: str) -> None:
        with self.engine.begin() as c:
            c.execute(update(deploys_table).where(deploys_table.c.deployment == name, deploys_table.c.kind == kind,
                                                  deploys_table.c.state == "live", deploys_table.c.id != keep)
                      .values(state="superseded"))

    def _precheck(self, row, dep: dict, image: str, data: Optional[bytes] = None) -> dict:
        """The deployment's DAGs (``data``, or its live release) parsed on ``image``."""
        if data is None and row.dags_release:
            data = self._bundle(row.name, row.dags_release)
        return self._call(self._runtime().check_dags, dep, image, data)

    def _bundle(self, name: str, did: str) -> bytes:
        r = self._deploy_row(name, did)
        if not r.bundle_key or self.store is None:
            raise DeploymentError("that deploy has no DAG bundle", 409)
        return self.store.get(r.bundle_key)

    def deploy_dags(self, actor: str, name: str, data: bytes, note: str = "", git_sha: str = "",
                    force: bool = False, source: str = "", authorized: bool = False) -> dict:
        """A DAG-only deploy: the bundle checked (it must parse on the deployment's Airflow), kept in the store and
        synced into the running deployment without a restart (a hibernating one gets it when it wakes)."""
        row = self._row(name)
        if not authorized:
            self._need(actor, "DEPLOY_AIRFLOW", urn(name))
        if self.store is None:
            raise DeploymentError("DAG deploys keep their bundles in Cairn Store (CAIRN_AIRFLOW_BUNDLE_STORE)", 503)
        info = self._call(bundles.inspect, data)
        if git_sha and not re.match(r"^[0-9a-f]{7,64}$", git_sha):
            raise DeploymentError("git_sha is a commit id")
        lock = self._locks.setdefault(name, threading.Lock())
        with lock:
            prior = self._claim(name, "deploying", ("ready", "hibernating"))
            try:
                spec = json.loads(row.spec)
                dep = self._dep(row, spec, with_secrets=False)
                check = self._precheck(row, dep, self._image(row, spec), data)
                if check["import_errors"] and not force:
                    did = self._record(name, actor, kind="dags", state="refused", sha256=info["sha256"],
                                       files=len(info["files"]), bytes=info["bytes"], git_sha=git_sha or None,
                                       note=note or None, source=source or None, checked=json.dumps(check))
                    self._audit("airflow_deployment.deploy_refused", actor, name, {
                        "deploy": did, "import_errors": sorted(check["import_errors"])})
                    raise DeploymentError(f"{len(check['import_errors'])} DAG file(s) do not import: "
                                          f"{', '.join(sorted(check['import_errors']))}", 422, check)
                did = self._new_id()
                key = f"{name}/{row.uid}/dags/{did}.tar.gz"
                self.store.put(key, data)
                with self.engine.begin() as c:
                    seq = (c.execute(select(deploys_table.c.seq).where(deploys_table.c.deployment == name)
                                     .order_by(deploys_table.c.seq.desc())).scalar() or 0) + 1
                    c.execute(insert(deploys_table).values(
                        id=did, deployment=name, seq=seq, kind="dags", state="live", image=None, bundle_key=key,
                        sha256=info["sha256"], files=len(info["files"]), bytes=info["bytes"], git_sha=git_sha or None,
                        note=note or None, source=source or None, checked=json.dumps(check), error=None,
                        created_by=actor, created_ms=self.clock()))
                if prior != "hibernating":
                    try:
                        self._call(self._runtime().sync_dags, dep, did, data)
                    except DeploymentError as e:
                        with self.engine.begin() as c:
                            c.execute(update(deploys_table).where(deploys_table.c.id == did)
                                      .values(state="failed", error=str(e)[:2000]))
                        raise
                self._supersede(name, "dags", did)
                self._set(name, dags_release=did)
            finally:
                self._set(name, state=prior)
        self._audit("airflow_deployment.deployed", actor, name, {
            "deploy": did, "kind": "dags", "sha256": info["sha256"], "files": len(info["files"]),
            "git_sha": git_sha or None, "source": source or None, "dags": check.get("dags")})
        return self._deploy_view(self._deploy_row(name, did))

    def deploy_git(self, actor: str, name: str, note: str = "", force: bool = False) -> dict:
        """A DAG-only deploy from the deployment's git folder, at the tip of its branch (as the deployer, who must
        own the folder)."""
        import os

        row = self._row(name)
        self._need(actor, "DEPLOY_AIRFLOW", urn(name))
        src = json.loads(row.spec).get("dag_source") or {}
        if src.get("type") != "git":
            raise DeploymentError(f"{name}'s DAGs come from deploys, not a git folder", 409)
        if self.git_folders is None:
            raise DeploymentError("git folders are not configured here", 503)
        gf = self.git_folders
        try:
            f = gf.get(actor, src["folder"])
            with gf._locks.setdefault(f["id"], threading.Lock()):
                d, _, _ = gf._checkout(f)
                sha = gf._git(d, "rev-parse", "HEAD").strip()
                base = os.path.join(d, f["path"] or "", src.get("path") or "")
                data = bundles.pack(base)
        except Exception as e:  # noqa: BLE001 - the folder's error, as the folder says it
            if isinstance(e, DeploymentError):
                raise
            raise DeploymentError(str(e), getattr(e, "status", 400))
        return self.deploy_dags(actor, name, data, note, sha, force, source=f"git:{src['folder']}", authorized=True)

    def deploy_image(self, actor: str, name: str, image: str, note: str = "", git_sha: str = "",
                     source: str = "", authorized: bool = False) -> dict:
        """An image deploy: a team image built FROM cairn/airflow with its requirements. Its Airflow must be the
        deployment's, and the live DAGs must parse on it; then the components are made again from it."""
        row = self._row(name)
        if not authorized:
            self._need(actor, "DEPLOY_AIRFLOW", urn(name))
        if not isinstance(image, str) or not IMAGE_REF.match(image):
            raise DeploymentError("image is an image reference (registry/name:tag or @sha256:...)")
        if git_sha and not re.match(r"^[0-9a-f]{7,64}$", git_sha):
            raise DeploymentError("git_sha is a commit id")
        rt = self._runtime()
        lock = self._locks.setdefault(name, threading.Lock())
        with lock:
            prior = self._claim(name, "deploying", ("ready",))
            try:
                spec = json.loads(row.spec)
                dep = self._dep(row, spec)
                version = self._call(rt.image_version, dep, image)
                if version != spec["airflow_version"]:
                    did = self._record(name, actor, kind="image", state="refused", image=image, git_sha=git_sha or None,
                                       note=note or None, source=source or None,
                                       error=f"the image runs Airflow {version}, the deployment {spec['airflow_version']}")
                    raise DeploymentError(f"{image} runs Airflow {version}; {name} runs {spec['airflow_version']} "
                                          f"(build FROM cairn/airflow:{spec['airflow_version']}, or upgrade first)", 409)
                check = self._precheck(row, dep, image)
                if check["import_errors"]:
                    did = self._record(name, actor, kind="image", state="refused", image=image, git_sha=git_sha or None,
                                       note=note or None, source=source or None, checked=json.dumps(check))
                    raise DeploymentError(f"the live DAGs do not import on {image}: "
                                          f"{', '.join(sorted(check['import_errors']))}", 422, check)
                where = self._call(rt.recreate, dep, image, True)
                if rt.name == "docker":                       # Kubernetes: the pods roll on their own
                    self._call(rt.wait_ready, dep)
                did = self._record(name, actor, kind="image", state="live", image=image, git_sha=git_sha or None,
                                   note=note or None, source=source or None, checked=json.dumps(check))
                self._supersede(name, "image", did)
                own = self._call(rt.image_for, spec["airflow_version"])
                self._set(name, image=where.get("image"), custom_image=None if image == own else image, error=None,
                          **({"ops_user": where["ops_user"]} if where.get("ops_user") else {}))
                self._resync(name)
            finally:
                self._set(name, state=prior)
        self._audit("airflow_deployment.deployed", actor, name, {"deploy": did, "kind": "image", "image": image,
                                                                 "git_sha": git_sha or None, "source": source or None})
        return self._deploy_view(self._deploy_row(name, did))

    def _resync(self, name: str) -> None:
        """After the components were made again (Kubernetes: their sidecars fetch it themselves): the live DAGs."""
        row = self._row(name)
        rt = self._runtime()
        if rt.name != "docker" or not row.dags_release:
            return
        dep = {"name": name, "uid": row.uid, "spec": json.loads(row.spec), "port": row.port}
        if self._call(rt.current_release, dep) != row.dags_release:
            self._call(rt.sync_dags, dep, row.dags_release, self._bundle(name, row.dags_release))

    def rollback(self, actor: str, name: str, did: str, force: bool = False) -> dict:
        """Back to an earlier deploy: deployed again (a new deploy, its source the old one), checked like any."""
        self._row(name)
        self._need(actor, "DEPLOY_AIRFLOW", urn(name))
        r = self._deploy_row(name, did)
        if r.state in ("refused", "failed"):
            raise DeploymentError("that deploy never went live: roll back to one that did", 409)
        if r.kind == "dags":
            out = self.deploy_dags(actor, name, self._bundle(name, did), f"rollback to #{r.seq}", r.git_sha or "",
                                   force, source=f"rollback:{did}", authorized=True)
        else:
            out = self.deploy_image(actor, name, r.image, f"rollback to #{r.seq}", r.git_sha or "",
                                    source=f"rollback:{did}", authorized=True)
        self._audit("airflow_deployment.rolled_back", actor, name, {"to": did, "deploy": out["id"]})
        return out

    # ---- promotion -------------------------------------------------------------------------------------------
    def _promotion_view(self, r) -> dict:
        return {"id": r.id, "deploy": r.deploy, "source": r.source, "target": r.target, "state": r.state,
                "requested_by": r.requested_by, "requested_ms": r.requested_ms, "decided_by": r.decided_by,
                "decided_ms": r.decided_ms, "comment": r.comment, "result_deploy": r.result_deploy, "error": r.error}

    def promotions(self, actor: str, name: str) -> list[dict]:
        self._row(name)
        self._see(actor, name)
        with self.engine.connect() as c:
            rows = c.execute(select(promotions_table).where((promotions_table.c.target == name) |
                                                            (promotions_table.c.source == name))
                             .order_by(promotions_table.c.requested_ms.desc())).all()
        return [self._promotion_view(r) for r in rows]

    def promote(self, actor: str, name: str, body: dict) -> dict:
        """A deploy of ``name`` (``deploy``; default its live DAGs) to the next environment (``to``; default its
        ``promotes_to``). Waits for an approver when the target requires approval."""
        src = self._row(name)
        extra = set(body) - {"deploy", "to", "comment"}
        if extra:
            raise DeploymentError(f"unknown fields: {', '.join(sorted(extra))}")
        self._see(actor, name)
        sspec = json.loads(src.spec)
        target = body.get("to") or sspec.get("promotes_to")
        if not target:
            raise DeploymentError(f"{name} names no next environment: give to, or set promotes_to", 400)
        trow = self._row(target)
        self._need(actor, "DEPLOY_AIRFLOW", urn(target))
        tspec = json.loads(trow.spec)
        if tspec["environment"] == sspec["environment"]:
            raise DeploymentError(f"{target} is the same environment ({tspec['environment']}) as {name}", 400)
        did = body.get("deploy") or src.dags_release
        if not did:
            raise DeploymentError(f"{name} has no live DAG deploy to promote: give deploy", 409)
        d = self._deploy_row(name, did)
        if d.state not in ("live", "superseded"):
            raise DeploymentError("only a deploy that went live is promoted", 409)
        if d.kind == "image" and tspec["airflow_version"] != sspec["airflow_version"]:
            raise DeploymentError(f"{target} runs Airflow {tspec['airflow_version']}, {name} "
                                  f"{sspec['airflow_version']}: upgrade it first", 409)
        pid = self._new_id()
        pending = bool((tspec.get("approval") or {}).get("required"))
        with self.engine.begin() as c:
            c.execute(insert(promotions_table).values(
                id=pid, deploy=did, source=name, target=target, state="pending", requested_by=actor,
                requested_ms=self.clock(), decided_by=None, decided_ms=None, comment=body.get("comment"),
                result_deploy=None, error=None))
        self._audit("airflow_deployment.promotion_requested", actor, target, {
            "promotion": pid, "from": name, "deploy": did, "approval": pending})
        if pending:
            return self._promotion(pid)
        return self._apply_promotion(actor, pid)

    def _promotion(self, pid: str) -> dict:
        with self.engine.connect() as c:
            r = c.execute(select(promotions_table).where(promotions_table.c.id == pid)).first()
        if r is None:
            raise DeploymentError("no such promotion", 404)
        return self._promotion_view(r)

    def _apply_promotion(self, actor: str, pid: str) -> dict:
        p = self._promotion(pid)
        d = self._deploy_row(p["source"], p["deploy"])
        try:
            if d.kind == "dags":
                out = self.deploy_dags(actor, p["target"], self._bundle(p["source"], d.id),
                                       f"promoted from {p['source']} #{d.seq}", d.git_sha or "",
                                       source=f"promoted:{p['source']}/{d.id}", authorized=True)
            else:
                out = self.deploy_image(actor, p["target"], d.image, f"promoted from {p['source']} #{d.seq}",
                                        d.git_sha or "", source=f"promoted:{p['source']}/{d.id}", authorized=True)
        except DeploymentError as e:
            with self.engine.begin() as c:
                c.execute(update(promotions_table).where(promotions_table.c.id == pid).values(
                    state="failed", error=str(e)[:2000], decided_by=p["decided_by"] or actor,
                    decided_ms=p["decided_ms"] or self.clock()))
            raise
        with self.engine.begin() as c:
            c.execute(update(promotions_table).where(promotions_table.c.id == pid).values(
                state="applied", result_deploy=out["id"], decided_by=p["decided_by"] or actor,
                decided_ms=p["decided_ms"] or self.clock()))
        self._audit("airflow_deployment.promoted", actor, p["target"], {"promotion": pid, "from": p["source"],
                                                                        "deploy": out["id"]})
        return self._promotion(pid)

    def decide(self, actor: str, pid: str, approve: bool, comment: str = "") -> dict:
        p = self._promotion(pid)
        if p["state"] != "pending":
            raise DeploymentError(f"that promotion is {p['state']}", 409)
        self._need(actor, "APPROVE_AIRFLOW_PROMOTION", urn(p["target"]))
        if actor == p["requested_by"] and not self.self_approve:
            raise DeploymentError("a promotion is approved by someone other than who asked for it", 403)
        with self.engine.begin() as c:
            n = c.execute(update(promotions_table).where(promotions_table.c.id == pid,
                                                         promotions_table.c.state == "pending")
                          .values(state="approved" if approve else "rejected", decided_by=actor,
                                  decided_ms=self.clock(), comment=comment or p["comment"])).rowcount
        if n != 1:
            raise DeploymentError("that promotion was decided meanwhile", 409)
        self._audit("airflow_deployment.promotion_" + ("approved" if approve else "rejected"), actor, p["target"],
                    {"promotion": pid, "comment": comment})
        if not approve:
            return self._promotion(pid)
        return self._apply_promotion(actor, pid)

    # ---- hibernation -----------------------------------------------------------------------------------------
    def hibernate(self, actor: str, name: str) -> dict:
        row = self._row(name)
        if actor:
            self._need(actor, "MANAGE_AIRFLOW_DEPLOYMENT", urn(name))
        if json.loads(row.spec)["environment"] == "prod":
            raise DeploymentError("prod deployments do not hibernate", 409)
        self._claim(name, "updating", ("ready",))
        try:
            self._call(self._runtime().stop, {"name": name, "uid": row.uid, "spec": json.loads(row.spec),
                                              "port": row.port})
            self._set(name, state="hibernating", awake_until_ms=None)
        except DeploymentError:
            self._set(name, state="ready")
            raise
        self._audit("airflow_deployment.hibernated", actor or "cairn", name, {})
        return self.get(actor, name) if actor else self._view(self._row(name))

    def wake(self, actor: str, name: str, minutes: Optional[int] = None) -> dict:
        """Starts a hibernating deployment (its secrets and identity read again: either missing, it stays down) and
        keeps it up for its ``wake_minutes`` (or ``minutes``) even outside its hours."""
        row = self._row(name)
        if actor:
            self._need(actor, "MANAGE_AIRFLOW_DEPLOYMENT", urn(name))
        spec = json.loads(row.spec)
        h = spec.get("hibernation") or {}
        mins = minutes or h.get("wake_minutes") or 60
        if not isinstance(mins, int) or not 1 <= mins <= 24 * 60:
            raise DeploymentError("minutes is 1 to 1440")
        until = self.clock() + mins * 60_000
        if row.state == "ready":
            self._set(name, awake_until_ms=until)
            return self.get(actor, name) if actor else self._view(self._row(name))
        self._claim(name, "updating", ("hibernating",))
        rt = self._runtime()
        try:
            dep = self._dep(row, spec)                   # secrets as of now: refused, it stays asleep
            dep["image"] = self._image(row, spec)        # Kubernetes renders again: the image it runs
            if rt.name == "docker":
                self._call(rt.put_files, dep)
                self._call(rt.start, dep)
                self._call(rt.wait_ready, dep)
            else:
                self._call(rt.start, dep)
            self._set(name, state="ready", awake_until_ms=until, error=None, token_ms=self.clock())
            self._resync(name)
        except DeploymentError as e:
            self._set(name, state="hibernating", error=str(e)[:2000])
            self._audit("airflow_deployment.wake_refused", actor or "cairn", name, {"error": str(e)[:300]})
            raise
        self._audit("airflow_deployment.woken", actor or "cairn", name, {"until_ms": until})
        return self.get(actor, name) if actor else self._view(self._row(name))

    def observability_sources(self) -> list[tuple[str, dict]]:
        """Each running deployment's Airflow API, as pipeline observability reads Airflow (shaped like
        ``cairn.flows.steps.airflow_from_env()``), with a short-lived token of Cairn's operations user."""
        out = []
        if self.runtime is None or self.keys is None:
            return out
        with self.engine.connect() as c:
            rows = c.execute(select(deployments_table).where(deployments_table.c.state == "ready")).all()
        for row in rows:
            if not row.ops_user:
                continue
            dep = {"name": row.name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec), "url": row.url}
            cfg = {"url": self.runtime.api_url(dep), "api": "v2",
                   "token": self.keys.api_token(row.name, row.uid, row.ops_user, f"cairn-af-{row.name}", ttl=900)}
            if isinstance(getattr(self.runtime, "verify", None), str):
                cfg["ca_file"] = self.runtime.verify
            out.append((row.name, cfg))
        return out

    def flow_airflow(self, actor: str, name: str) -> dict:
        """What a flow's ``airflow`` step calls a managed deployment with (it needs ``DEPLOY_AIRFLOW`` there): the
        deployment woken if it hibernates, and a short-lived token of Cairn's operations user in it."""
        row = self._row(name)
        self._need(actor, "DEPLOY_AIRFLOW", urn(name))
        if row.state == "hibernating":
            self.wake("", name)
            self._audit("airflow_deployment.woken_for_flow", actor, name, {})
            row = self._row(name)
        if row.state != "ready" or not row.ops_user:
            raise DeploymentError(f"{name} is {row.state}", 409)
        rt = self._runtime()
        dep = {"name": name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec), "url": row.url}
        out = {"url": rt.api_url(dep), "api": "v2",
               "token": self.keys.api_token(name, row.uid, row.ops_user, f"cairn-af-{name}", ttl=900)}
        if isinstance(getattr(rt, "verify", None), str):
            out["ca_file"] = rt.verify
        return out

    # ---- health, logs ----------------------------------------------------------------------------------------
    def _api(self, row, method: str, path: str, **kw):
        import httpx

        if not row.ops_user:
            raise DeploymentError("Cairn has no operations user in this deployment yet", 409)
        rt = self._runtime()
        dep = {"name": row.name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec), "url": row.url}
        token = self.keys.api_token(row.name, row.uid, row.ops_user, f"cairn-af-{row.name}")
        headers = {**kw.pop("headers", {}), "Authorization": f"Bearer {token}"}
        try:
            r = httpx.request(method, rt.api_url(dep) + path, headers=headers, timeout=30, trust_env=False,
                              verify=getattr(rt, "verify", True), **kw)
        except httpx.HTTPError as e:
            raise DeploymentError(f"Airflow does not answer: {type(e).__name__}", 503)
        if r.status_code >= 300:
            raise DeploymentError(f"Airflow: {method} {path}: {r.status_code} {r.text[:300]}",
                                  404 if r.status_code == 404 else 502)
        return r.json() if r.content else {}

    def status(self, actor: str, name: str) -> dict:
        """Health: the components, the scheduler's heartbeat, the DAG processor's import errors, queued and running
        tasks."""
        import httpx

        row = self._row(name)
        self._see(actor, name)
        if row.state == "creating" and getattr(self.runtime, "name", "") == "kubernetes":
            self.reconcile(name)
            row = self._row(name)
        out: dict[str, Any] = {"name": name, "state": row.state, "url": row.url, "image": row.image,
                               "dags_release": row.dags_release, "error": row.error}
        problems: list[str] = []
        rt = self._runtime()
        dep = {"name": name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec), "url": row.url}
        try:
            out["runtime_state"] = self._call(rt.state, dep)
        except DeploymentError as e:
            out["runtime_state"] = {"error": str(e)}
            problems.append("the runtime does not answer")
        if row.state in ("creating", "failed", "deleting", "hibernating"):
            out["health"] = row.state
            out["problems"] = problems
            return out
        try:
            h = httpx.get(rt.api_url(dep) + "/api/v2/monitor/health", timeout=10, trust_env=False,
                          verify=getattr(rt, "verify", True)).json()
            out["components"] = h
            for k in ("metadatabase", "scheduler", "dag_processor", "triggerer"):
                if (h.get(k) or {}).get("status") != "healthy":
                    problems.append(f"{k.replace('_', ' ')} is not healthy")
            beat = (h.get("scheduler") or {}).get("latest_scheduler_heartbeat")
            if beat:
                age = self.clock() / 1000 - dt.datetime.fromisoformat(beat.replace("Z", "+00:00")).timestamp()
                out["scheduler_heartbeat_age_s"] = round(age, 1)
        except (httpx.HTTPError, ValueError) as e:
            out["health"] = "down"
            out["problems"] = problems + [f"Airflow does not answer: {type(e).__name__}"]
            return out
        try:
            errs = self._api(row, "GET", "/api/v2/importErrors", params={"limit": 100})
            out["import_errors"] = [{"file": e.get("filename"), "error": (e.get("stack_trace") or "")[-1500:]}
                                    for e in errs.get("import_errors") or []]
            if out["import_errors"]:
                problems.append(f"{len(out['import_errors'])} DAG file(s) do not import")
            tasks = {}
            for state in ("queued", "running"):
                got = self._api(row, "GET", "/api/v2/dags/~/dagRuns/~/taskInstances",
                                params={"state": state, "limit": 1})
                tasks[state] = got.get("total_entries", 0)
            out["tasks"] = tasks
        except DeploymentError as e:
            problems.append(str(e)[:300])
        out["problems"] = problems
        out["health"] = "degraded" if problems else "healthy"
        return out

    def logs(self, actor: str, name: str, component: str = "scheduler", tail: int = 200) -> dict:
        row = self._row(name)
        self._see(actor, name)
        if not 1 <= int(tail) <= 5000:
            raise DeploymentError("tail is 1 to 5000")
        dep = {"name": name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec)}
        text = self._call(self._runtime().logs, dep, component, int(tail))
        self._audit("airflow_deployment.logs_read", actor, name, {"component": component})
        return {"name": name, "component": component, "log": text}

    def task_log(self, actor: str, name: str, dag_id: str, run_id: str, task_id: str, try_number: int = 1) -> dict:
        """A task's log, through Airflow's API (as Cairn's operations user in the deployment)."""
        from urllib.parse import quote

        row = self._row(name)
        if not self._may(actor, ("VIEW_AIRFLOW_DEPLOYMENT", "DEPLOY_AIRFLOW", "MANAGE_AIRFLOW_DEPLOYMENT"), urn(name)):
            raise DeploymentError("no such deployment", 404)
        if not 1 <= int(try_number) <= 100:
            raise DeploymentError("try_number is 1 to 100")
        path = (f"/api/v2/dags/{quote(dag_id, safe='')}/dagRuns/{quote(run_id, safe='')}/taskInstances/"
                f"{quote(task_id, safe='')}/logs/{int(try_number)}")
        got = self._api(row, "GET", path, params={"full_content": "true"}, headers={"Accept": "application/json"})
        content = got.get("content")
        if isinstance(content, list):
            lines = []
            for ev in content:
                if isinstance(ev, dict):
                    lines.append(" ".join(str(x) for x in (ev.get("timestamp") or "", ev.get("level") or "",
                                                           ev.get("event") or "") if x).strip())
                else:
                    lines.append(str(ev))
            text = "\n".join(lines)
        else:
            text = str(content or "")
        self._audit("airflow_deployment.task_log_read", actor, name, {"dag": dag_id, "run": run_id, "task": task_id})
        return {"name": name, "dag_id": dag_id, "run_id": run_id, "task_id": task_id, "try_number": try_number,
                "log": text}

    def bundle_for(self, actor: str, name: str, release: str = "current") -> tuple[str, bytes]:
        """The live DAG bundle, for the deployment's own sync sidecars (its pipeline user) or a deployer."""
        row = self._row(name)
        if actor != pipeline_user(name):
            self._need(actor, "DEPLOY_AIRFLOW", urn(name))
        did = row.dags_release if release == "current" else release
        if not did:
            raise DeploymentError("no DAGs deployed yet", 404)
        return did, self._bundle(name, did)

    # ---- the scheduler ---------------------------------------------------------------------------------------
    def reconcile(self, name: str) -> None:
        """Kubernetes: a deployment being made becomes ready once its pods are."""
        row = self._row(name)
        if row.state != "creating":
            return
        dep = {"name": name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec), "url": row.url}
        try:
            st = self.runtime.state(dep)
        except RuntimeError_ as e:
            logger.warning("deployment %s: %s", name, e)
            return
        if st.get("ready"):
            self._set(name, state="ready", error=None)

    def run_due(self) -> int:
        """Hibernation on its schedule, workload tokens renewed (Docker), deployments the cluster is still making."""
        if self.runtime is None:
            return 0
        now = self.clock()
        did = 0
        with self.engine.connect() as c:
            rows = c.execute(select(deployments_table)).all()
        when = dt.datetime.fromtimestamp(now / 1000, dt.timezone.utc)
        for row in rows:
            try:
                if row.state == "creating" and self.runtime.name == "kubernetes":
                    self.reconcile(row.name)
                    continue
                spec = json.loads(row.spec)
                h = spec.get("hibernation")
                awake = (row.awake_until_ms or 0) > now
                if h and row.state == "ready" and not awake and not awake_now(h, when):
                    self.hibernate("", row.name)
                    did += 1
                    continue
                if h and row.state == "hibernating" and awake_now(h, when) and not row.error:
                    self.wake("", row.name, minutes=1)
                    did += 1
                    continue
                if row.state == "ready" and self.runtime.name == "docker" and now - (row.token_ms or 0) >= TOKEN_RENEW_MS:
                    self.renew_identity(row.name)
                    did += 1
            except DeploymentError as e:
                logger.warning("deployment %s: %s", row.name, e)
        return did

    def renew_identity(self, name: str) -> None:
        row = self._row(name)
        rt = self._runtime()
        if rt.name != "docker":
            return
        dep = {"name": name, "uid": row.uid, "port": row.port, "spec": json.loads(row.spec)}
        self._call(rt.refresh_files, dep, {"opt/cairn/identity/token": (self.keys.workload_token(name), 0o400)})
        self._set(name, token_ms=self.clock())


def build_airflow_router(svc: AirflowDeployments, current_principal) -> Any:
    from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response
    from fastapi.responses import JSONResponse
    from starlette.concurrency import run_in_threadpool

    router = APIRouter(prefix="/v1/airflow", tags=["airflow"])

    async def call(fn):
        try:
            return await run_in_threadpool(fn)
        except DeploymentError as e:
            if e.detail is not None:
                return JSONResponse({"detail": str(e), "check": e.detail}, status_code=e.status)
            raise HTTPException(e.status, str(e))
        except (SpecError, bundles.BundleError) as e:
            raise HTTPException(e.status, str(e))

    @router.get("/deployments")
    async def list_(p=Depends(current_principal)):
        return {"deployments": await call(lambda: svc.list(p.urn)), "runtime": getattr(svc.runtime, "name", None),
                "versions": list(svc.versions()), "executors": list(getattr(svc.runtime, "executors", ()))}

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
        out = await call(lambda: svc.delete(p.urn, name))
        return out if isinstance(out, JSONResponse) else Response(status_code=204)

    @router.get("/deployments/{name}/status")
    async def status(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.status(p.urn, name))

    @router.get("/deployments/{name}/logs")
    async def logs(name: str, component: str = Query("scheduler"), tail: int = Query(200),
                   p=Depends(current_principal)):
        return await call(lambda: svc.logs(p.urn, name, component, tail))

    @router.get("/deployments/{name}/task-logs")
    async def task_logs(name: str, dag_id: str = Query(...), run_id: str = Query(...), task_id: str = Query(...),
                        try_number: int = Query(1), p=Depends(current_principal)):
        return await call(lambda: svc.task_log(p.urn, name, dag_id, run_id, task_id, try_number))

    @router.get("/deployments/{name}/deploys")
    async def deploys(name: str, p=Depends(current_principal)):
        return {"deploys": await call(lambda: svc.deploys(p.urn, name))}

    @router.post("/deployments/{name}/deploys", status_code=201)
    async def deploy(name: str, request: Request, note: str = Query(""), git_sha: str = Query(""),
                     force: bool = Query(False), p=Depends(current_principal)):
        """A DAG-only deploy (the body is the bundle, application/gzip), an image deploy ({"image": ...}) or a deploy
        from the deployment's git folder ({"from": "git"})."""
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/gzip", "application/x-gzip", "application/x-tar", "application/octet-stream")):
            data = await request.body()
            return await call(lambda: svc.deploy_dags(p.urn, name, data, note, git_sha, force))
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "send a DAG bundle (application/gzip) or {\"image\": ...} or {\"from\": \"git\"}")
        if not isinstance(body, dict):
            raise HTTPException(400, "a JSON object")
        if body.get("from") == "git":
            return await call(lambda: svc.deploy_git(p.urn, name, str(body.get("note") or note), bool(body.get("force"))))
        if body.get("image"):
            return await call(lambda: svc.deploy_image(p.urn, name, body["image"], str(body.get("note") or note),
                                                       str(body.get("git_sha") or git_sha)))
        raise HTTPException(400, "send a DAG bundle (application/gzip) or {\"image\": ...} or {\"from\": \"git\"}")

    @router.post("/deployments/{name}/deploys/{did}/rollback", status_code=201)
    async def rollback(name: str, did: str, force: bool = Query(False), p=Depends(current_principal)):
        return await call(lambda: svc.rollback(p.urn, name, did, force))

    @router.get("/deployments/{name}/dags/{release}")
    async def bundle(name: str, release: str, p=Depends(current_principal)):
        out = await call(lambda: svc.bundle_for(p.urn, name, release))
        if isinstance(out, JSONResponse):
            return out
        did, data = out
        return Response(data, media_type="application/gzip", headers={"X-Cairn-Release": did,
                                                                      "Cache-Control": "no-store"})

    @router.get("/deployments/{name}/promotions")
    async def promotions(name: str, p=Depends(current_principal)):
        return {"promotions": await call(lambda: svc.promotions(p.urn, name))}

    @router.post("/deployments/{name}/promote", status_code=201)
    async def promote(name: str, body: dict = Body(default={}), p=Depends(current_principal)):
        return await call(lambda: svc.promote(p.urn, name, body or {}))

    @router.post("/promotions/{pid}/approve")
    async def approve(pid: str, comment: str = Body("", embed=True), p=Depends(current_principal)):
        return await call(lambda: svc.decide(p.urn, pid, True, comment))

    @router.post("/promotions/{pid}/reject")
    async def reject(pid: str, comment: str = Body("", embed=True), p=Depends(current_principal)):
        return await call(lambda: svc.decide(p.urn, pid, False, comment))

    @router.post("/deployments/{name}/hibernate")
    async def hibernate(name: str, p=Depends(current_principal)):
        return await call(lambda: svc.hibernate(p.urn, name))

    @router.post("/deployments/{name}/wake")
    async def wake(name: str, minutes: Optional[int] = Body(None, embed=True), p=Depends(current_principal)):
        return await call(lambda: svc.wake(p.urn, name, minutes))

    return router
