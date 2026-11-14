"""Managed Airflow deployments on Kubernetes, with the official Apache Airflow chart (pinned: ``CHART_VERSION``).

Per deployment ``<name>``, in a namespace of its own (``CAIRN_AIRFLOW_K8S_NAMESPACE_PREFIX`` + name), Cairn renders
the chart (``helm template``, the chart from ``CAIRN_AIRFLOW_K8S_CHART``) with Cairn's values (``values-cairn.yaml``
is the base: no local admin, sign-in through Cairn, external metadata database, secrets from Secrets) and applies
what it renders through the Kubernetes API, server-side, with the field manager ``cairn`` and the label
``cairn.io/airflow-deployment``, pruning what a new render no longer has. Helm renders only: there is no release
for helm to keep, Cairn is the one owner of the objects, and a render is reproducible from the deployment's spec.

Cairn adds:

* a ``ServiceAccount`` (``airflow-cairn``) every component runs as; its projected token (audience ``cairn``) is the
  deployment's workload identity, bound by Cairn to the pipeline user (``CAIRN_AIRFLOW_K8S_WORKLOAD_ISSUER``);
* the Secrets of its settings (``cairn-af-env``: every setting, secret references resolved, read by every Airflow
  container through ``envFrom``), metadata connection, Fernet, API and JWT keys, and the CA;
* its metadata database: a CloudNativePG ``Cluster`` (``cairn-af-db``, the operator of Managed Postgres);
* a DAG sync sidecar beside the scheduler, DAG processor and triggerer, and before each task pod
  (``cairn.airflow_deploy.dag_sync`` from a ConfigMap): it fetches the live DAG release from Cairn as the pipeline
  user, so a DAG-only deploy restarts nothing;
* ``NetworkPolicies``: the chart's between components, and one that admits traffic into the namespace only from
  itself and the ingress controller's namespace (``CAIRN_AIRFLOW_K8S_INGRESS_NAMESPACE``) and Cairn's
  (``CAIRN_AIRFLOW_K8S_CAIRN_NAMESPACE``).

Hibernation scales every Deployment and StatefulSet to zero (the database too); waking applies the render again.
Cairn signs in with its pod's service account (or ``CAIRN_AIRFLOW_K8S_API``, ``_TOKEN_FILE``, ``_CA_FILE``); it
needs to manage namespaces and the kinds above in them (the chart's ``cairn.airflow.deployments`` ClusterRole).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from importlib import resources
from typing import Any, Optional

from cairn.databases.docker_runtime import RuntimeError_

logger = logging.getLogger(__name__)
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
LABEL = "cairn.io/airflow-deployment"
CHART_VERSION = "1.20.0"
RELEASE = "airflow"
ACCOUNT = "airflow-cairn"
DEFAULT_IMAGES = {"3.3.2": "cairn/airflow:0.1.0"}
FAB_CONFIG = "/opt/airflow/webserver_config.py"
# kind -> (API path prefix, plural): what a render may hold, in the order it is applied.
KINDS: list[tuple[str, str, str, str]] = [
    ("v1", "ServiceAccount", "/api/v1", "serviceaccounts"),
    ("v1", "Secret", "/api/v1", "secrets"),
    ("v1", "ConfigMap", "/api/v1", "configmaps"),
    ("v1", "PersistentVolumeClaim", "/api/v1", "persistentvolumeclaims"),
    ("rbac.authorization.k8s.io/v1", "Role", "/apis/rbac.authorization.k8s.io/v1", "roles"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding", "/apis/rbac.authorization.k8s.io/v1", "rolebindings"),
    ("postgresql.cnpg.io/v1", "Cluster", "/apis/postgresql.cnpg.io/v1", "clusters"),
    ("v1", "Service", "/api/v1", "services"),
    ("networking.k8s.io/v1", "NetworkPolicy", "/apis/networking.k8s.io/v1", "networkpolicies"),
    ("networking.k8s.io/v1", "Ingress", "/apis/networking.k8s.io/v1", "ingresses"),
    ("policy/v1", "PodDisruptionBudget", "/apis/policy/v1", "poddisruptionbudgets"),
    ("apps/v1", "StatefulSet", "/apis/apps/v1", "statefulsets"),
    ("apps/v1", "Deployment", "/apis/apps/v1", "deployments"),
    ("batch/v1", "Job", "/apis/batch/v1", "jobs"),
    ("batch/v1", "CronJob", "/apis/batch/v1", "cronjobs"),
    ("autoscaling/v2", "HorizontalPodAutoscaler", "/apis/autoscaling/v2", "horizontalpodautoscalers"),
]
_BY_KIND = {(a, k): (base, plural) for a, k, base, plural in KINDS}


def _b64(v: Any) -> str:
    return base64.b64encode(v if isinstance(v, bytes) else str(v).encode()).decode()


def sync_source() -> str:
    return resources.files("cairn.airflow_deploy").joinpath("dag_sync.py").read_text()


def base_values() -> dict:
    """``deploy/airflow/values-cairn.yaml``, the base Cairn's values start from (the chart's own defaults if the file
    is not shipped)."""
    import yaml

    here = os.path.dirname(os.path.abspath(__file__))
    for path in (os.environ.get("CAIRN_AIRFLOW_K8S_BASE_VALUES", ""),
                 os.path.join(here, "values-cairn.yaml"),
                 os.path.join(here, "..", "..", "..", "deploy", "airflow", "values-cairn.yaml")):
        if path and os.path.exists(path):
            with open(path) as fh:
                return yaml.safe_load(fh) or {}
    return {}


class KubernetesRuntime:
    name = "kubernetes"
    executors = ("KubernetesExecutor", "CeleryExecutor", "LocalExecutor")
    default_executor = "KubernetesExecutor"
    fab_config = FAB_CONFIG
    label = LABEL          # what marks the objects Cairn owns (pruning, workloads): managed NiFi has its own

    def __init__(self, api: str = "", token_file: str = "", ca_file: str = "", namespace_prefix: str = "cairn-af-",
                 images: Optional[dict] = None, helm: str = "helm", chart: str = "",
                 url_template: str = "https://{name}.airflow.example.com", ingress_class: str = "",
                 ingress_namespace: str = "ingress-nginx", cairn_namespace: str = "cairn", storage_class: str = "",
                 db_image: str = "", api_scheme: str = "http", start_timeout: float = 900):
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api = (api or (f"https://{host}:{port}" if host else "")).rstrip("/")
        if not self.api:
            raise RuntimeError_("managed Airflow on Kubernetes: no API server (not in a cluster, and no "
                                "CAIRN_AIRFLOW_K8S_API)", 503)
        if not chart:
            raise RuntimeError_(f"CAIRN_AIRFLOW_K8S_CHART: the official Apache Airflow chart {CHART_VERSION} "
                                "(a .tgz or its folder)", 503)
        if "{name}" not in url_template:
            raise RuntimeError_("CAIRN_AIRFLOW_K8S_URL_TEMPLATE names each deployment's address with {name}", 503)
        self.token_file = token_file or f"{SA}/token"
        self.verify: Any = ca_file or (f"{SA}/ca.crt" if os.path.exists(f"{SA}/ca.crt") else True)
        self.prefix, self.helm, self.chart = namespace_prefix, helm, chart
        self.images = {str(k): v for k, v in (images or DEFAULT_IMAGES).items()}
        self.url_template, self.ingress_class = url_template, ingress_class
        self.ingress_namespace, self.cairn_namespace = ingress_namespace, cairn_namespace
        self.storage_class, self.db_image, self.api_scheme = storage_class, db_image, api_scheme
        self.start_timeout = start_timeout

    @classmethod
    def from_env(cls) -> "KubernetesRuntime":
        images = os.environ.get("CAIRN_AIRFLOW_K8S_IMAGES", "")
        e = os.environ.get
        return cls(e("CAIRN_AIRFLOW_K8S_API", ""), e("CAIRN_AIRFLOW_K8S_TOKEN_FILE", ""), e("CAIRN_AIRFLOW_K8S_CA_FILE", ""),
                   e("CAIRN_AIRFLOW_K8S_NAMESPACE_PREFIX", "cairn-af-"), json.loads(images) if images else None,
                   e("CAIRN_AIRFLOW_K8S_HELM", "helm"), e("CAIRN_AIRFLOW_K8S_CHART", ""),
                   e("CAIRN_AIRFLOW_K8S_URL_TEMPLATE", "https://{name}.airflow.example.com"),
                   e("CAIRN_AIRFLOW_K8S_INGRESS_CLASS", ""), e("CAIRN_AIRFLOW_K8S_INGRESS_NAMESPACE", "ingress-nginx"),
                   e("CAIRN_AIRFLOW_K8S_CAIRN_NAMESPACE", "cairn"), e("CAIRN_AIRFLOW_K8S_STORAGE_CLASS", ""),
                   e("CAIRN_AIRFLOW_K8S_DB_IMAGE", ""))

    # ---- the API ---------------------------------------------------------------------------------------------
    def _call(self, method: str, path: str, body: Any = None, ok404: bool = False, ctype: str = "",
              params: Optional[dict] = None, raw: bool = False) -> Any:
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
            with httpx.Client(base_url=self.api, verify=self.verify, timeout=60, trust_env=False) as c:
                r = c.request(method, path, content=content, headers=headers, params=params)
        except httpx.HTTPError as e:
            raise RuntimeError_(f"kubernetes: {method} {path}: {type(e).__name__}", 503) from e
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 300:
            raise RuntimeError_(f"kubernetes: {method} {path}: {r.status_code} {r.text[:300]}",
                                403 if r.status_code == 403 else 409 if r.status_code == 409 else 502)
        if raw:
            return r.text
        return r.json() if r.content else {}

    def _apply(self, obj: dict) -> None:
        av, kind = obj["apiVersion"], obj["kind"]
        if kind == "Namespace":
            path = f"/api/v1/namespaces/{obj['metadata']['name']}"
        else:
            if (av, kind) not in _BY_KIND:
                raise RuntimeError_(f"the render holds a {kind} ({av}), which Cairn does not apply", 502)
            base, plural = _BY_KIND[(av, kind)]
            path = f"{base}/namespaces/{obj['metadata']['namespace']}/{plural}/{obj['metadata']['name']}"
        self._call("PATCH", path, obj, ctype="application/apply-patch+yaml",
                   params={"fieldManager": "cairn", "force": "true"})

    def namespace(self, name: str) -> str:
        return f"{self.prefix}{name}"

    # ---- addresses -------------------------------------------------------------------------------------------
    def public_url(self, dep: dict) -> str:
        return self.url_template.format(name=dep["name"]).rstrip("/")

    def internal_api_url(self, dep: dict) -> str:
        return f"{self.api_scheme}://{RELEASE}-api-server.{self.namespace(dep['name'])}.svc:8080"

    def api_url(self, dep: dict) -> str:
        return self.internal_api_url(dep)

    def service_account_subject(self, name: str) -> str:
        return f"system:serviceaccount:{self.namespace(name)}:{ACCOUNT}"

    def image_for(self, version: str) -> str:
        if str(version) not in self.images:
            raise RuntimeError_(f"Airflow {version} is not offered here (CAIRN_AIRFLOW_K8S_IMAGES: "
                                f"{', '.join(sorted(self.images))})", 400)
        return self.images[str(version)]

    # ---- the render ------------------------------------------------------------------------------------------
    def values(self, dep: dict, image: str) -> dict:
        """Cairn's values for the chart: values-cairn.yaml, then the deployment's own."""
        spec, name = dep["spec"], dep["name"]
        repo, _, tag = image.rpartition(":") if ":" in image.rsplit("/", 1)[-1] else (image, "", "latest")
        res = spec["resources"]

        def resources_(c: str) -> dict:
            r = res[c]
            q = {"cpu": f"{int(float(r['cpu']) * 1000)}m", "memory": f"{int(r['memory_mb'])}Mi"}
            return {"requests": dict(q), "limits": {"memory": q["memory"]}}
        sync = {"name": "cairn-dag-sync", "image": image, "command": ["python", "-I", "/opt/cairn-sync/dag_sync.py"],
                "envFrom": [{"secretRef": {"name": "cairn-af-env"}}],
                "env": [{"name": "CAIRN_DAGS_DIR", "value": "/opt/airflow/dags"}],
                "volumeMounts": [{"name": "cairn-dags", "mountPath": "/opt/airflow/dags"},
                                 {"name": "cairn-sync", "mountPath": "/opt/cairn-sync", "readOnly": True},
                                 {"name": "cairn-identity", "mountPath": "/opt/cairn/identity", "readOnly": True},
                                 {"name": "cairn-ca", "mountPath": "/opt/cairn/ca", "readOnly": True}],
                "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}}}
        dags_vol = [{"name": "cairn-dags", "emptyDir": {}},
                    {"name": "cairn-sync", "configMap": {"name": "cairn-af-dag-sync"}}]
        dags_mount = [{"name": "cairn-dags", "mountPath": "/opt/airflow/dags"}]
        account = {"create": False, "name": ACCOUNT}
        base = base_values()
        for k in ("env", "secret", "dags", "volumes", "volumeMounts"):
            base.pop(k, None)
        v: dict[str, Any] = {
            **base,
            "defaultAirflowRepository": repo, "defaultAirflowTag": tag, "airflowVersion": spec["airflow_version"],
            "executor": dep["env"]["AIRFLOW__CORE__EXECUTOR"],
            "labels": {LABEL: name},
            "postgresql": {"enabled": False}, "redis": {"enabled": dep["env"]["AIRFLOW__CORE__EXECUTOR"] == "CeleryExecutor"},
            "statsd": {"enabled": False}, "pgbouncer": {"enabled": False},
            "data": {"metadataSecretName": "cairn-af-metadata"},
            "fernetKeySecretName": "cairn-af-fernet", "apiSecretKeySecretName": "cairn-af-api-secret",
            "jwtSecretName": "cairn-af-jwt",
            "createUserJob": {"enabled": False},
            "migrateDatabaseJob": {"enabled": True, "useHelmHooks": False, "applyCustomEnv": True,
                                   "ttlSecondsAfterFinished": 600},
            "extraEnvFrom": "- secretRef:\n    name: cairn-af-env\n",
            "volumes": [{"name": "cairn-identity", "projected": {"sources": [{"serviceAccountToken": {
                "audience": "cairn", "expirationSeconds": 3600, "path": "token"}}]}},
                {"name": "cairn-ca", "secret": {"secretName": "cairn-af-ca", "optional": True}}],
            "volumeMounts": [{"name": "cairn-identity", "mountPath": "/opt/cairn/identity", "readOnly": True},
                             {"name": "cairn-ca", "mountPath": "/opt/cairn/ca", "readOnly": True}],
            "apiServer": {**(base.get("apiServer") or {}), "replicas": res["api_server"]["replicas"],
                          "resources": resources_("api_server"), "serviceAccount": account,
                          "apiServerConfig": "from cairn_airflow.auth import configure\n\nconfigure(globals())\n"},
            "scheduler": {"replicas": res["scheduler"]["replicas"], "resources": resources_("scheduler"),
                          "serviceAccount": account, "extraContainers": [sync], "extraVolumes": dags_vol,
                          "extraVolumeMounts": dags_mount},
            "dagProcessor": {"enabled": True, "replicas": res["dag_processor"]["replicas"],
                             "resources": resources_("dag_processor"), "serviceAccount": account,
                             "extraContainers": [sync], "extraVolumes": dags_vol, "extraVolumeMounts": dags_mount},
            "triggerer": {"replicas": res["triggerer"]["replicas"], "resources": resources_("triggerer"),
                          "serviceAccount": account, "extraContainers": [sync], "extraVolumes": dags_vol,
                          "extraVolumeMounts": dags_mount, "persistence": {"enabled": False}},
            "workers": {"replicas": res["worker"]["replicas"], "resources": resources_("worker"),
                        "serviceAccount": account, "persistence": {"enabled": False},
                        "extraInitContainers": [{**sync, "name": "cairn-dag-sync-once",
                                                 "command": [*sync["command"], "--once"]}],
                        "extraVolumes": dags_vol, "extraVolumeMounts": dags_mount},
            "migrateDatabaseJobServiceAccount": account,
            "networkPolicies": {"enabled": True},
            "ingress": {"apiServer": {"enabled": True, "hosts": [{"name": self.public_url(dep).split("://", 1)[1]
                                                                  .split("/", 1)[0]}],
                                      **({"ingressClassName": self.ingress_class} if self.ingress_class else {})}},
            "logs": {"persistence": {"enabled": False}},
        }
        v["migrateDatabaseJob"]["serviceAccount"] = account
        v["createUserJob"]["serviceAccount"] = account
        v.pop("migrateDatabaseJobServiceAccount")
        return v

    def _helm(self, dep: dict, image: str) -> list[dict]:
        import yaml

        ns = self.namespace(dep["name"])
        with tempfile.TemporaryDirectory(prefix="cairn-af-") as d:
            path = os.path.join(d, "values.yaml")
            with open(path, "w") as fh:
                yaml.safe_dump(self.values(dep, image), fh)
            try:
                out = subprocess.run([self.helm, "template", RELEASE, self.chart, "--namespace", ns, "-f", path,
                                      "--kube-version", "1.33.0"], capture_output=True, text=True, timeout=300)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise RuntimeError_(f"helm (CAIRN_AIRFLOW_K8S_HELM): {e}", 503)
        if out.returncode != 0:
            raise RuntimeError_(f"the Airflow chart did not render: {out.stderr.strip()[-800:]}", 502)
        docs = [x for x in yaml.safe_load_all(out.stdout) if isinstance(x, dict) and x.get("kind")]
        for x in docs:
            x.setdefault("metadata", {}).setdefault("namespace", ns)
            x["metadata"].setdefault("labels", {})[LABEL] = dep["name"]
        return docs

    def _secret(self, ns: str, name: str, dep: str, data: dict) -> dict:
        return {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": name, "namespace": ns, "labels": {LABEL: dep}},
                "data": {k: _b64(v) for k, v in data.items()}}

    def render(self, dep: dict, image: str) -> list[dict]:
        """Every object of the deployment, in the order it is applied."""
        name, ns, s = dep["name"], self.namespace(dep["name"]), dep["secrets"]
        db_url = (f"postgresql://airflow:{dep['db_password']}@cairn-af-db-rw.{ns}.svc:5432/airflow?sslmode=require")
        env = {**dep["env"], "AIRFLOW__FAB__CONFIG_FILE": FAB_CONFIG}
        if "CAIRN_CA_FILE" in env:
            env["CAIRN_CA_FILE"] = "/opt/cairn/ca/ca.crt"
            for k in ("AIRFLOW__SECRETS__BACKEND_KWARGS", "AIRFLOW_CONN_CAIRN_DEFAULT", "AIRFLOW__OPENLINEAGE__TRANSPORT"):
                if k in env:
                    env[k] = env[k].replace("/opt/cairn/ca.crt", "/opt/cairn/ca/ca.crt")
        objs: list[dict] = [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {
                LABEL: name, "app.kubernetes.io/managed-by": "cairn",
                "pod-security.kubernetes.io/enforce": "baseline"}}},
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": ACCOUNT, "namespace": ns,
                                                                        "labels": {LABEL: name}}},
            self._secret(ns, "cairn-af-env", name, env),
            self._secret(ns, "cairn-af-metadata", name, {"connection": db_url}),
            self._secret(ns, "cairn-af-fernet", name, {"fernet-key": s["fernet_key"]}),
            self._secret(ns, "cairn-af-api-secret", name, {"api-secret-key": s["api_secret"]}),
            self._secret(ns, "cairn-af-jwt", name, {"jwt-secret": s["jwt_secret"]}),
            {**self._secret(ns, "cairn-af-db-app", name, {"username": "airflow", "password": dep["db_password"]}),
             "type": "kubernetes.io/basic-auth"},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cairn-af-dag-sync", "namespace": ns,
                                                                   "labels": {LABEL: name}},
             "data": {"dag_sync.py": sync_source()}},
        ]
        ca = (dep.get("files") or {}).get("opt/cairn/ca.crt")
        objs.append(self._secret(ns, "cairn-af-ca", name, {"ca.crt": ca[0] if ca else b""}))
        db_res = dep["spec"]["resources"]["scheduler"]
        cluster: dict[str, Any] = {
            "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
            "metadata": {"name": "cairn-af-db", "namespace": ns, "labels": {LABEL: name}},
            "spec": {"instances": 1, "storage": {"size": "5Gi", **({"storageClass": self.storage_class}
                                                                    if self.storage_class else {})},
                     "bootstrap": {"initdb": {"database": "airflow", "owner": "airflow",
                                              "secret": {"name": "cairn-af-db-app"}}},
                     "resources": {"requests": {"cpu": "250m", "memory": "512Mi"},
                                   "limits": {"memory": f"{max(512, int(db_res['memory_mb']) // 2)}Mi"}}}}
        if self.db_image:
            cluster["spec"]["imageName"] = self.db_image
        objs.append(cluster)
        objs.append({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                     "metadata": {"name": "cairn-af-ingress", "namespace": ns, "labels": {LABEL: name}},
                     "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{"from": [
                         {"podSelector": {}},
                         {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": self.ingress_namespace}}},
                         {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": self.cairn_namespace}}}]}]}})
        rendered = self._helm(dep, image)
        order = {(a, k): i for i, (a, k, _, _) in enumerate(KINDS)}
        rendered.sort(key=lambda x: order.get((x["apiVersion"], x["kind"]), 99))
        return objs + rendered

    def apply(self, dep: dict, image: str) -> list[dict]:
        objs = self.render(dep, image)
        ns = self.namespace(dep["name"])
        # Jobs are immutable: the migration job of an earlier render goes before the new one is applied.
        jobs = self._call("GET", f"/apis/batch/v1/namespaces/{ns}/jobs", ok404=True,
                          params={"labelSelector": f"{LABEL}={dep['name']}"}) or {}
        for j in jobs.get("items") or []:
            self._call("DELETE", f"/apis/batch/v1/namespaces/{ns}/jobs/{j['metadata']['name']}",
                       {"propagationPolicy": "Background"}, ok404=True)
        for o in objs:
            self._apply(o)
        self._prune(dep, objs)
        return objs

    def _prune(self, dep: dict, objs: list[dict]) -> None:
        ns = self.namespace(dep["name"])
        keep = {(o["kind"], o["metadata"]["name"]) for o in objs}
        for _av, kind, base, plural in KINDS:
            if kind in ("Cluster", "Secret"):
                # The database is never pruned (only deleted with the namespace); Secrets have fixed names and
                # Cairn may write them, never list them.
                continue
            got = self._call("GET", f"{base}/namespaces/{ns}/{plural}", ok404=True,
                             params={"labelSelector": f"{self.label}={dep['name']}"}) or {}
            for item in got.get("items") or []:
                if (kind, item["metadata"]["name"]) not in keep:
                    self._call("DELETE", f"{base}/namespaces/{ns}/{plural}/{item['metadata']['name']}", ok404=True)

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def create(self, dep: dict, image: str) -> dict:
        self.apply(dep, image)
        return {"image": image, "ops_user": None}

    def recreate(self, dep: dict, image: str, migrate: bool = False) -> dict:
        self.apply(dep, image)
        return {"image": image, "ops_user": None}

    def _workloads(self, dep: dict) -> list[dict]:
        ns = self.namespace(dep["name"])
        out = []
        for kind, plural in (("Deployment", "deployments"), ("StatefulSet", "statefulsets")):
            got = self._call("GET", f"/apis/apps/v1/namespaces/{ns}/{plural}", ok404=True,
                             params={"labelSelector": f"{self.label}={dep['name']}"}) or {}
            for item in got.get("items") or []:
                item["kind"] = kind
                out.append(item)
        return out

    def stop(self, dep: dict) -> None:
        ns = self.namespace(dep["name"])
        for w in self._workloads(dep):
            plural = "deployments" if w["kind"] == "Deployment" else "statefulsets"
            self._call("PATCH", f"/apis/apps/v1/namespaces/{ns}/{plural}/{w['metadata']['name']}/scale",
                       {"spec": {"replicas": 0}}, ctype="application/merge-patch+json")
        self._call("PATCH", f"/apis/postgresql.cnpg.io/v1/namespaces/{ns}/clusters/cairn-af-db",
                   {"metadata": {"annotations": {"cnpg.io/hibernation": "on"}}},
                   ctype="application/merge-patch+json", ok404=True)

    def start(self, dep: dict) -> None:
        ns = self.namespace(dep["name"])
        self._call("PATCH", f"/apis/postgresql.cnpg.io/v1/namespaces/{ns}/clusters/cairn-af-db",
                   {"metadata": {"annotations": {"cnpg.io/hibernation": "off"}}},
                   ctype="application/merge-patch+json", ok404=True)
        image = dep.get("image") or self.image_for(dep["spec"]["airflow_version"])
        self.apply(dep, image)

    def state(self, dep: dict) -> dict:
        comps = []
        for w in self._workloads(dep):
            labels = (w.get("metadata") or {}).get("labels") or {}
            want = int((w.get("spec") or {}).get("replicas") or 0)
            ready = int((w.get("status") or {}).get("readyReplicas") or 0)
            comps.append({"component": labels.get("component") or w["metadata"]["name"], "kind": w["kind"],
                          "replicas": want, "ready_replicas": ready, "running": want > 0 and ready >= want})
        core = [c for c in comps if c["component"] in ("scheduler", "api-server", "dag-processor", "triggerer")]
        return {"runtime": "kubernetes", "namespace": self.namespace(dep["name"]), "components": comps,
                "running": sum(1 for c in comps if c["running"]),
                "ready": bool(core) and all(c["running"] for c in core)}

    def wait_ready(self, dep: dict, timeout: Optional[float] = None) -> dict:
        end = time.time() + (timeout or self.start_timeout)
        st: dict = {}
        while time.time() < end:
            st = self.state(dep)
            if st["ready"]:
                return st
            time.sleep(5)
        raise RuntimeError_(f"{dep['name']} was not ready after {int(timeout or self.start_timeout)} s: {st}", 504)

    def delete(self, dep: dict) -> None:
        self._call("DELETE", f"/api/v1/namespaces/{self.namespace(dep['name'])}", ok404=True)

    def logs(self, dep: dict, component: str, tail: int = 200) -> str:
        ns = self.namespace(dep["name"])
        got = self._call("GET", f"/api/v1/namespaces/{ns}/pods", params={
            "labelSelector": f"{LABEL}={dep['name']},component={component}"}) or {}
        out = []
        for pod in got.get("items") or []:
            text = self._call("GET", f"/api/v1/namespaces/{ns}/pods/{pod['metadata']['name']}/log",
                              params={"tailLines": str(tail), "container": component}, raw=True, ok404=True)
            out.append(f"--- {pod['metadata']['name']}\n{text or ''}")
        return "\n".join(out) or f"(no {component} pods)"

    # ---- DAGs and images: one-shot pods ------------------------------------------------------------------------
    def sync_dags(self, dep: dict, release: str, data: bytes) -> None:
        """The DAG sync sidecars fetch the live release from Cairn themselves (within their period)."""

    def current_release(self, dep: dict) -> Optional[str]:
        return None

    def _run_pod(self, dep: dict, image: str, script: str, files: dict[str, bytes], timeout: float = 600) -> str:
        """A one-shot pod (its files from a ConfigMap): its log; RuntimeError_ if it failed."""
        import secrets as pysecrets

        ns = self.namespace(dep["name"])
        pod = f"cairn-af-check-{pysecrets.token_hex(4)}"
        if sum(len(v) for v in files.values()) > 900_000:
            raise RuntimeError_("the DAG bundle is too large to check on Kubernetes (900 kB)", 413)
        cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": pod, "namespace": ns, "labels": {LABEL: dep["name"]}},
              "binaryData": {k: _b64(v) for k, v in files.items()}}
        self._call("POST", f"/api/v1/namespaces/{ns}/configmaps", cm)
        try:
            self._call("POST", f"/api/v1/namespaces/{ns}/pods", {
                "apiVersion": "v1", "kind": "Pod", "metadata": {"name": pod, "labels": {LABEL: dep["name"],
                                                                                       "component": "check"}},
                "spec": {"restartPolicy": "Never", "serviceAccountName": ACCOUNT, "automountServiceAccountToken": False,
                         "containers": [{"name": "check", "image": image, "command": ["bash", "-c", script],
                                         "envFrom": [{"secretRef": {"name": "cairn-af-env"}}],
                                         # The image's user cannot write under /opt/cairn: the unpacked DAGs
                                         # and the parse's SQLite file live in the pod's own scratch space.
                                         "volumeMounts": [{"name": "in", "mountPath": "/opt/cairn/in"},
                                                          {"name": "work", "mountPath": "/opt/cairn/check"},
                                                          {"name": "tmp", "mountPath": "/tmp"}],
                                         "securityContext": {"allowPrivilegeEscalation": False,
                                                             "capabilities": {"drop": ["ALL"]}},
                                         # DAG files run while they are parsed: bounded in memory, CPU and
                                         # the container's writable layer as well as in time.
                                         "resources": {"requests": {"cpu": "250m", "memory": "256Mi"},
                                                       "limits": {"memory": "1Gi", "cpu": "1",
                                                                  "ephemeral-storage": "1Gi"}}}],
                         "securityContext": {"runAsNonRoot": True, "runAsUser": 50000, "runAsGroup": 0,   # the official image's user
                                      "seccompProfile": {"type": "RuntimeDefault"}},
                         "volumes": [{"name": "in", "configMap": {"name": pod}},
                                     {"name": "work", "emptyDir": {"sizeLimit": "512Mi"}},
                                     {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}]}})
            end = time.time() + timeout
            while time.time() < end:
                p = self._call("GET", f"/api/v1/namespaces/{ns}/pods/{pod}") or {}
                phase = (p.get("status") or {}).get("phase")
                if phase in ("Succeeded", "Failed"):
                    log = self._call("GET", f"/api/v1/namespaces/{ns}/pods/{pod}/log", raw=True) or ""
                    if phase == "Failed":
                        raise RuntimeError_(f"{pod} failed: {log[-1500:]}")
                    return log
                time.sleep(3)
            raise RuntimeError_(f"{pod} did not finish in {int(timeout)} s", 504)
        finally:
            self._call("DELETE", f"/api/v1/namespaces/{ns}/pods/{pod}", ok404=True)
            self._call("DELETE", f"/api/v1/namespaces/{ns}/configmaps/{pod}", ok404=True)

    def check_dags(self, dep: dict, image: str, data: Optional[bytes]) -> dict:

        from cairn.airflow_deploy.docker_runtime import CHECK, dagbag_result

        script = ("set -e; mkdir -p /opt/cairn/check; "
                  "if [ -f /opt/cairn/in/bundle.tgz ]; then tar xzf /opt/cairn/in/bundle.tgz -C /opt/cairn/check; fi; "
                  + CHECK.replace("set -a; . /opt/cairn/airflow.env; set +a\n", ""))
        out = self._run_pod(dep, image, script, {"bundle.tgz": data} if data else {})
        result = dagbag_result(out)
        if result is None:
            raise RuntimeError_(f"the DAG check gave no result: {out[-800:]}")
        return result

    def image_version(self, dep: dict, image: str) -> str:
        import re

        out = self._run_pod(dep, image, "python -c 'import airflow; print(\"CAIRN_AIRFLOW_VERSION=\" + airflow.__version__)'",
                            {})
        m = re.search(r"CAIRN_AIRFLOW_VERSION=([0-9][0-9a-z.+-]*)", out)
        if not m:
            raise RuntimeError_(f"{image}: not an Airflow image ({out[-300:]})", 400)
        return m.group(1)
