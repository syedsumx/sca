"""Managed PostgreSQL instances on Kubernetes, through the CloudNativePG operator (pinned: ``CNPG_VERSION``).

The operator is installed once per cluster (``kubectl apply --server-side -f`` its release manifest; see the operator
manual's "Managed Postgres"). Cairn then applies, in one namespace (``CAIRN_PG_K8S_NAMESPACE``), server-side with the
field manager ``cairn``, per instance ``<name>``:

* a ``Cluster`` ``cairn-pg-<name>``: ``1 + replicas`` PostgreSQL pods (a primary and streaming standbys, spread over
  nodes; the operator fails over to a standby when the primary is lost), the major version's image, CPU, memory and
  storage, the allow-listed parameters (``pg_stat_statements`` on), and continuous WAL archiving and base backups to
  Cairn Store with Barman Cloud (``backup.barmanObjectStore``), the store's credentials read by the operator from a
  Secret the operator of Cairn makes (``CAIRN_PG_K8S_BACKUP_SECRET``: keys ``access-key`` and ``secret-key``);
* a ``Secret`` ``cairn-pg-<name>-superuser`` (the superuser's password, derived by Cairn);
* a ``Pooler`` ``cairn-pg-<name>-pooler`` (PgBouncer) when pooling is on;
* a ``ScheduledBackup`` per backup schedule, and a ``Backup`` per backup taken on demand;
* a ``Service`` of type NodePort for the primary when Cairn reaches instances from outside the cluster
  (``CAIRN_PG_K8S_ACCESS=node-port``).

A restore is a new ``Cluster`` bootstrapped with ``recovery`` from the source's object store (a backup, or the
archive up to a point in time). A switchover sets the cluster's target primary (as ``kubectl cnpg promote`` does).
Cairn signs in with its pod's service account (or ``CAIRN_PG_K8S_API``, ``CAIRN_PG_K8S_TOKEN_FILE``,
``CAIRN_PG_K8S_CA_FILE``); the chart's Role allows these kinds in that namespace only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional

from cairn.databases.docker_runtime import RuntimeError_

logger = logging.getLogger(__name__)
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
CNPG_VERSION = "1.27.0"
CNPG_MANIFEST = ("https://raw.githubusercontent.com/cloudnative-pg/cloudnative-pg/release-1.27/releases/"
                 f"cnpg-{CNPG_VERSION}.yaml")
DEFAULT_IMAGES = {"16": "ghcr.io/cloudnative-pg/postgresql:16.6", "17": "ghcr.io/cloudnative-pg/postgresql:17.2"}
GROUP = "/apis/postgresql.cnpg.io/v1"
LABEL = "cairn.io/pg-instance"


def cluster_name(instance: str) -> str:
    return f"cairn-pg-{instance}"


def quantity_cpu(cpu: float) -> str:
    return f"{int(round(float(cpu) * 1000))}m"


def every_hours_cron(hours: int, offset_minute: int = 0) -> str:
    """CloudNativePG schedules have six fields (seconds first)."""
    if hours >= 24 and hours % 24 == 0:
        days = hours // 24
        return f"0 {offset_minute} 0 */{days} * *" if days > 1 else f"0 {offset_minute} 0 * * *"
    return f"0 {offset_minute} */{hours} * * *"


class CnpgRuntime:
    name = "kubernetes"
    supports_replicas = True
    supports_pooler = True

    def __init__(self, namespace: str, api: str = "", token_file: str = "", ca_file: str = "",
                 images: Optional[dict] = None, store: Optional[dict] = None, backup_secret: str = "",
                 access: str = "cluster-ip", node_address: str = "", storage_class: str = ""):
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api = (api or (f"https://{host}:{port}" if host else "")).rstrip("/")
        if not self.api:
            raise RuntimeError_("managed Postgres on Kubernetes: no API server (not in a cluster, and no "
                                "CAIRN_PG_K8S_API)", 503)
        if access not in ("cluster-ip", "node-port"):
            raise RuntimeError_("CAIRN_PG_K8S_ACCESS is cluster-ip or node-port", 503)
        if access == "node-port" and not node_address:
            raise RuntimeError_("CAIRN_PG_K8S_ACCESS=node-port needs CAIRN_PG_K8S_NODE_ADDRESS", 503)
        if store and not backup_secret:
            raise RuntimeError_("backups on Kubernetes need the Secret with the store's keys "
                                "(CAIRN_PG_K8S_BACKUP_SECRET)", 503)
        self.ns, self.access, self.node_address = namespace, access, node_address
        self.token_file = token_file or f"{SA}/token"
        self.verify: Any = ca_file or (f"{SA}/ca.crt" if os.path.exists(f"{SA}/ca.crt") else True)
        self.images = {str(k): v for k, v in (images or DEFAULT_IMAGES).items()}
        self.store, self.backup_secret, self.storage_class = store, backup_secret, storage_class

    # ---- the API ---------------------------------------------------------------------------------------------
    def _call(self, method: str, path: str, body: Any = None, ok404: bool = False, ctype: str = "",
              params: Optional[dict] = None) -> Optional[dict]:
        import httpx

        try:
            token = open(self.token_file).read().strip()
        except OSError as e:
            raise RuntimeError_(f"kubernetes: the service account token: {e}", 503)
        headers = {"Authorization": f"Bearer {token}"}
        if ctype:
            headers["Content-Type"] = ctype
        content = None if body is None else (body if isinstance(body, (str, bytes)) else json.dumps(body))
        try:
            with httpx.Client(base_url=self.api, verify=self.verify, timeout=30) as c:
                r = c.request(method, path, content=content, headers=headers, params=params)
        except httpx.HTTPError as e:
            raise RuntimeError_(f"kubernetes: {method} {path}: {type(e).__name__}", 503) from e
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 300:
            raise RuntimeError_(f"kubernetes: {method} {path}: {r.status_code} {r.text[:300]}",
                                403 if r.status_code == 403 else 409 if r.status_code == 409 else 502)
        return r.json() if r.content else {}

    def _apply(self, path: str, obj: dict) -> None:
        self._call("PATCH", path, obj, ctype="application/apply-patch+yaml",
                   params={"fieldManager": "cairn", "force": "true"})

    def _p(self, kind: str, name: str = "") -> str:
        core = {"secrets": "/api/v1", "services": "/api/v1", "pods": "/api/v1"}
        base = core.get(kind, GROUP)
        return f"{base}/namespaces/{self.ns}/{kind}" + (f"/{name}" if name else "")

    def _meta(self, name: str, instance: str) -> dict:
        return {"name": name, "namespace": self.ns, "labels": {LABEL: instance, "app.kubernetes.io/managed-by": "cairn"}}

    # ---- manifests -------------------------------------------------------------------------------------------
    def image_for(self, version: str, image: str = "") -> str:
        if image:
            return image
        if str(version) not in self.images:
            raise RuntimeError_(f"PostgreSQL {version} is not offered here (CAIRN_PG_K8S_IMAGES: "
                                f"{', '.join(sorted(self.images))})", 400)
        return self.images[str(version)]

    def _object_store(self, prefix: str, server: str) -> dict:
        s = self.store or {}
        return {"destinationPath": f"s3://{s['bucket']}/{prefix.strip('/')}/",
                "endpointURL": s.get("endpoint_in_cluster") or s["endpoint"], "serverName": server,
                "s3Credentials": {"accessKeyId": {"name": self.backup_secret, "key": "access-key"},
                                  "secretAccessKey": {"name": self.backup_secret, "key": "secret-key"},
                                  "region": {"name": self.backup_secret, "key": "region"}
                                  if s.get("region_in_secret") else None},
                "wal": {"compression": "gzip", "maxParallel": 2}, "data": {"compression": "gzip", "jobs": 2}}

    def cluster(self, inst: dict, recovery: Optional[dict] = None) -> dict:
        """The CloudNativePG Cluster for an instance (``recovery``: {"source_prefix", "source_name", "target_time"
        | "backup_id"} to bootstrap from another instance's backups)."""
        spec, name = inst["spec"], cluster_name(inst["name"])
        params = {k: str(v) for k, v in (spec.get("parameters") or {}).items()}
        params.setdefault("pg_stat_statements.track", "top")     # the operator then preloads the extension
        mem = int(spec["memory_mb"])
        c: dict[str, Any] = {
            "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "metadata": self._meta(name, inst["name"]),
            "spec": {
                "instances": 1 + int(spec.get("replicas") or 0),
                "imageName": self.image_for(spec["version"], inst.get("image") or ""),
                "primaryUpdateStrategy": "unsupervised", "primaryUpdateMethod": "switchover",
                "enableSuperuserAccess": True, "superuserSecret": {"name": f"{name}-superuser"},
                "postgresql": {"parameters": params},
                "resources": {"requests": {"cpu": quantity_cpu(spec["cpu"]), "memory": f"{mem}Mi"},
                              "limits": {"memory": f"{mem}Mi"}},
                "storage": {"size": f"{int(spec['storage_gb'])}Gi"},
                "affinity": {"enablePodAntiAffinity": True, "topologyKey": "kubernetes.io/hostname",
                             "podAntiAffinityType": "preferred"},
                "monitoring": {"enablePodMonitor": False},
            }}
        if self.storage_class:
            c["spec"]["storage"]["storageClass"] = self.storage_class
        if self.store:
            keep = max([int(s.get("retention_days") or 7) for s in spec.get("backup_schedules") or []] or [7])
            store = self._object_store(inst["backup_prefix"], name)
            store["s3Credentials"] = {k: v for k, v in store["s3Credentials"].items() if v}
            c["spec"]["backup"] = {"retentionPolicy": f"{keep}d", "barmanObjectStore": store}
        if recovery:
            src = self._object_store(recovery["source_prefix"], cluster_name(recovery["source_name"]))
            src["s3Credentials"] = {k: v for k, v in src["s3Credentials"].items() if v}
            src.pop("wal", None)
            src.pop("data", None)
            boot: dict[str, Any] = {"source": "origin"}
            if recovery.get("target_time"):
                boot["recoveryTarget"] = {"targetTime": recovery["target_time"]}
            elif recovery.get("backup_id"):
                boot["recoveryTarget"] = {"backupID": recovery["backup_id"], "targetImmediate": True}
            c["spec"]["bootstrap"] = {"recovery": boot}
            c["spec"]["externalClusters"] = [{"name": "origin", "barmanObjectStore": src}]
        else:
            c["spec"]["bootstrap"] = {"initdb": {"dataChecksums": True}}
        return c

    def pooler(self, inst: dict) -> dict:
        p = inst["spec"].get("pooler") or {}
        name = cluster_name(inst["name"])
        return {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Pooler",
                "metadata": self._meta(f"{name}-pooler", inst["name"]),
                "spec": {"cluster": {"name": name}, "instances": int(p.get("instances") or 2), "type": "rw",
                         "pgbouncer": {"poolMode": p.get("mode") or "transaction",
                                       "parameters": {"max_client_conn": str(p.get("max_client_connections") or 1000),
                                                      "default_pool_size": str(p.get("pool_size") or 20)}}}}

    def scheduled_backup(self, inst: dict, sched: dict) -> dict:
        name = cluster_name(inst["name"])
        return {"apiVersion": "postgresql.cnpg.io/v1", "kind": "ScheduledBackup",
                "metadata": self._meta(f"{name}-{sched['name']}", inst["name"]),
                "spec": {"schedule": every_hours_cron(int(sched["every_hours"])), "backupOwnerReference": "self",
                         "cluster": {"name": name}, "method": "barmanObjectStore", "immediate": True}}

    # ---- an instance -----------------------------------------------------------------------------------------
    def apply(self, inst: dict, password: Optional[str] = None, recovery: Optional[dict] = None) -> None:
        """Applies the instance's objects (create and every change: resize, replicas, image, parameters, pooler,
        schedules); the operator rolls the pods."""
        name = cluster_name(inst["name"])
        if password is not None:
            self._apply(self._p("secrets", f"{name}-superuser"), {
                "apiVersion": "v1", "kind": "Secret", "metadata": self._meta(f"{name}-superuser", inst["name"]),
                "type": "kubernetes.io/basic-auth", "stringData": {"username": "postgres", "password": password}})
        cluster = self.cluster(inst, recovery)
        if recovery is None and self._call("GET", self._p("clusters", name), ok404=True) is not None:
            cluster["spec"].pop("bootstrap", None)        # how it began is fixed; a restore keeps its recovery
            cluster["spec"].pop("externalClusters", None)
        self._apply(self._p("clusters", name), cluster)
        if inst["spec"].get("pooler"):
            self._apply(self._p("poolers", f"{name}-pooler"), self.pooler(inst))
        else:
            self._call("DELETE", self._p("poolers", f"{name}-pooler"), ok404=True)
        wanted = {f"{name}-{s['name']}" for s in inst["spec"].get("backup_schedules") or []} if self.store else set()
        for s in (inst["spec"].get("backup_schedules") or []) if self.store else []:
            self._apply(self._p("scheduledbackups", f"{name}-{s['name']}"), self.scheduled_backup(inst, s))
        have = self._call("GET", self._p("scheduledbackups"), params={"labelSelector": f"{LABEL}={inst['name']}"}) or {}
        for item in have.get("items") or []:
            if item["metadata"]["name"] not in wanted:
                self._call("DELETE", self._p("scheduledbackups", item["metadata"]["name"]), ok404=True)
        if self.access == "node-port":
            self._apply(self._p("services", f"{name}-np"), {
                "apiVersion": "v1", "kind": "Service", "metadata": self._meta(f"{name}-np", inst["name"]),
                "spec": {"type": "NodePort", "ports": [{"name": "postgres", "port": 5432, "targetPort": 5432}],
                         "selector": {"cnpg.io/cluster": name, "cnpg.io/instanceRole": "primary"}}})

    def create(self, inst: dict, password: str) -> dict:
        self.apply(inst, password)
        return {"host": "", "port": 5432, "image": self.image_for(inst["spec"]["version"], inst.get("image") or "")}

    def restore_cluster(self, inst: dict, password: str, recovery: dict) -> dict:
        self.apply(inst, password, recovery)
        return {"host": "", "port": 5432, "image": self.image_for(inst["spec"]["version"], inst.get("image") or "")}

    def delete(self, inst: dict, purge: bool = True) -> None:
        name = cluster_name(inst["name"])
        sel = {"labelSelector": f"{LABEL}={inst['name']}"}
        for kind in ("scheduledbackups", "poolers"):
            for item in (self._call("GET", self._p(kind), params=sel) or {}).get("items") or []:
                self._call("DELETE", self._p(kind, item["metadata"]["name"]), ok404=True)
        self._call("DELETE", self._p("clusters", name), ok404=True)
        self._call("DELETE", self._p("services", f"{name}-np"), ok404=True)
        self._call("DELETE", self._p("secrets", f"{name}-superuser"), ok404=True)

    def state(self, inst: dict) -> dict:
        name = cluster_name(inst["name"])
        c = self._call("GET", self._p("clusters", name), ok404=True)
        if c is None:
            return {"runtime": "kubernetes", "running": False, "status": "missing", "instances": []}
        st = c.get("status") or {}
        pods = (self._call("GET", self._p("pods"), params={"labelSelector": f"cnpg.io/cluster={name}"}) or {}).get("items") or []
        members = []
        for p in pods:
            ready = all(cs.get("ready") for cs in (p.get("status") or {}).get("containerStatuses") or [])
            members.append({"name": p["metadata"]["name"], "role": "primary" if p["metadata"]["name"] == st.get("currentPrimary")
                            else "replica", "ready": ready, "node": (p.get("spec") or {}).get("nodeName"),
                            "ip": (p.get("status") or {}).get("podIP")})
        conds = {x.get("type"): x for x in st.get("conditions") or []}
        archiving = conds.get("ContinuousArchiving") or {}
        return {"runtime": "kubernetes", "status": st.get("phase") or "creating",
                "running": st.get("readyInstances", 0) >= 1 and bool(st.get("currentPrimary")),
                "ready_instances": st.get("readyInstances", 0), "wanted_instances": (c.get("spec") or {}).get("instances"),
                "primary": st.get("currentPrimary"), "target_primary": st.get("targetPrimary"),
                "first_recoverability_point": st.get("firstRecoverabilityPoint"),
                "last_successful_backup": st.get("lastSuccessfulBackup"),
                "continuous_archiving": {"ok": archiving.get("status") == "True", "reason": archiving.get("reason"),
                                         "message": archiving.get("message")} if archiving else None,
                "image": (c.get("spec") or {}).get("imageName"), "instances": members}

    def ready(self, inst: dict) -> bool:
        s = self.state(inst)
        return s["status"] == "Cluster in healthy state" and s["running"]

    def endpoint(self, inst: dict) -> tuple[str, int]:
        name = cluster_name(inst["name"])
        if self.access == "node-port":
            svc = self._call("GET", self._p("services", f"{name}-np")) or {}
            return self.node_address, int(svc["spec"]["ports"][0]["nodePort"])
        return f"{name}-rw.{self.ns}.svc", 5432

    def client_endpoint(self, inst: dict) -> tuple[str, int]:
        """Where applications connect: the pooler when there is one, else the primary's service."""
        name = cluster_name(inst["name"])
        if inst["spec"].get("pooler") and self.access == "cluster-ip":
            return f"{name}-pooler.{self.ns}.svc", 5432
        return self.endpoint(inst)

    def switchover(self, inst: dict, target: str = "") -> str:
        """Promotes a standby (the first ready one, or ``target``): what ``kubectl cnpg promote`` does."""
        name = cluster_name(inst["name"])
        st = self.state(inst)
        replicas = [m["name"] for m in st["instances"] if m["role"] == "replica" and m["ready"]]
        if target and target not in replicas:
            raise RuntimeError_(f"{target} is not a ready standby of {inst['name']}", 409)
        if not replicas:
            raise RuntimeError_("there is no ready standby to switch over to", 409)
        target = target or replicas[0]
        self._call("PATCH", self._p("clusters", name) + "/status",
                   {"status": {"targetPrimary": target, "phase": "Switchover in progress",
                               "phaseReason": f"Switching over to {target} (Cairn)"}},
                   ctype="application/merge-patch+json")
        return target

    def restart(self, inst: dict) -> None:
        """A rolling restart (the operator restarts standbys, then switches over)."""
        name = cluster_name(inst["name"])
        self._call("PATCH", self._p("clusters", name),
                   {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}},
                   ctype="application/merge-patch+json")

    # ---- backups ---------------------------------------------------------------------------------------------
    def backup(self, inst: dict, bid: str) -> dict:
        """A base backup on demand (a Backup object); answered once the operator reports it done or failed."""
        name = cluster_name(inst["name"])
        self._apply(self._p("backups", f"{name}-{bid}"), {
            "apiVersion": "postgresql.cnpg.io/v1", "kind": "Backup", "metadata": self._meta(f"{name}-{bid}", inst["name"]),
            "spec": {"cluster": {"name": name}, "method": "barmanObjectStore"}})
        end = time.time() + 3600
        while time.time() < end:
            b = self._call("GET", self._p("backups", f"{name}-{bid}")) or {}
            st = b.get("status") or {}
            if st.get("phase") == "completed":
                return {"id": bid, "backup_id": st.get("backupId"), "start_lsn": st.get("beginLSN"),
                        "end_lsn": st.get("endLSN"), "timeline": None, "bytes": None,
                        "started": st.get("startedAt"), "stopped": st.get("stoppedAt")}
            if st.get("phase") == "failed":
                raise RuntimeError_(f"backup failed: {st.get('error') or 'see the operator'}")
            time.sleep(3)
        raise RuntimeError_("the backup did not finish within an hour", 504)

    def backups(self, inst: dict) -> list[dict]:
        sel = {"labelSelector": f"cnpg.io/cluster={cluster_name(inst['name'])}"}
        out = []
        for b in (self._call("GET", self._p("backups"), params=sel) or {}).get("items") or []:
            st = b.get("status") or {}
            out.append({"name": b["metadata"]["name"], "phase": st.get("phase"), "backup_id": st.get("backupId"),
                        "started": st.get("startedAt"), "stopped": st.get("stoppedAt"), "error": st.get("error")})
        return out

    def exporter(self, inst: dict, wanted: tuple[str, ...]) -> dict:
        """The operator's exporter on each pod (port 9187), the series named in ``wanted`` (by prefix)."""
        import httpx

        out: dict[str, dict] = {}
        for m in self.state(inst)["instances"]:
            if not m.get("ip"):
                continue
            try:
                text = httpx.get(f"http://{m['ip']}:9187/metrics", timeout=5).text
            except httpx.HTTPError as e:
                out[m["name"]] = {"error": type(e).__name__}
                continue
            out[m["name"]] = parse_prometheus(text, wanted)
        return out


def parse_prometheus(text: str, wanted: tuple[str, ...]) -> dict:
    """``{series: value}`` for the samples whose metric name starts with one of ``wanted``."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)", line)
        if not m or not m.group(1).startswith(wanted):
            continue
        try:
            out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
        except ValueError:
            continue
    return out
