"""Data apps on Kubernetes: each app a Deployment of its own, started on its first visit and stopped when idle.

Cairn applies four objects per app in one namespace (``CAIRN_APPS_K8S_NAMESPACE``), all labelled
``cairn.io/app: <name>`` and owned by Cairn (server-side apply, field manager ``cairn``):

* a **Secret** with the app's key (``CAIRN_APP_KEY``) and, for an app from a configured repository, the
  header git signs in with: it reaches git through the environment, never a command line;
* a **Deployment** at zero replicas: an init container (``CAIRN_APPS_K8S_GIT_IMAGE``) checks out the exact
  commit Cairn deployed into an empty volume, then the app's ``image`` runs its ``command`` there. The pod has
  no Kubernetes credentials, runs as a non-root user without privileges, and gets ``PORT``, ``CAIRN_APP_BASE``
  and ``CAIRN_APP_KEY`` as a local app does;
* a **Service** for its port: ``ClusterIP`` when Cairn runs in the cluster; ``NodePort`` when it reaches the
  apps from outside (``CAIRN_APPS_K8S_ACCESS=node-port``, at ``CAIRN_APPS_K8S_NODE_ADDRESS``);
* a **NetworkPolicy**: only Cairn may reach the app's pods (``CAIRN_APPS_K8S_INGRESS_FROM``, network policy
  peers as JSON; by default Cairn's API pods in ``CAIRN_APPS_K8S_CAIRN_NAMESPACE``).

The first visit scales the Deployment to one and waits until it is ready; every visit stamps the Deployment
(``cairn.io/last-request``, at most once a minute), and a Deployment unstamped for the app's ``idle_minutes``
is scaled back to zero, whichever Cairn replica served it. Cairn signs in with its pod's service account (or
``CAIRN_APPS_K8S_API``, ``CAIRN_APPS_K8S_TOKEN_FILE``, ``CAIRN_APPS_K8S_CA_FILE``); the chart's Role lets it
manage these four kinds in that namespace only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
LABEL = "cairn.io/app"
STAMP = "cairn.io/last-request"
GIT_IMAGE = "alpine/git:v2.49.1"


class KubeError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def object_name(app: str) -> str:
    return f"cairn-app-{app}"


class KubeApps:
    def __init__(self, namespace: str, api: str = "", token_file: str = "", ca_file: str = "", access: str = "cluster-ip",
                 node_address: str = "", ingress_from: Optional[list] = None, git_image: str = GIT_IMAGE,
                 resources: Optional[dict] = None, start_timeout: float = 180):
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api = (api or (f"https://{host}:{port}" if host else "")).rstrip("/")
        if not self.api:
            raise KubeError("data apps on Kubernetes: no API server (not in a cluster, and no CAIRN_APPS_K8S_API)", 503)
        if access not in ("cluster-ip", "node-port"):
            raise KubeError("CAIRN_APPS_K8S_ACCESS is cluster-ip or node-port", 503)
        if access == "node-port" and not node_address:
            raise KubeError("CAIRN_APPS_K8S_ACCESS=node-port needs CAIRN_APPS_K8S_NODE_ADDRESS", 503)
        if not ingress_from:
            raise KubeError("data apps on Kubernetes need the peers that may reach them (CAIRN_APPS_K8S_INGRESS_FROM)", 503)
        self.namespace, self.access, self.node_address = namespace, access, node_address
        self.token_file = token_file or f"{SA}/token"
        self.verify: Any = ca_file or (f"{SA}/ca.crt" if os.path.exists(f"{SA}/ca.crt") else True)
        self.ingress_from, self.git_image = ingress_from, git_image
        self.resources = resources or {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}}
        self.start_timeout = start_timeout
        self._stamped: dict[str, float] = {}
        self._waking: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_env(cls) -> Optional["KubeApps"]:
        ns = os.environ.get("CAIRN_APPS_K8S_NAMESPACE", "")
        if not ns:
            return None
        peers = os.environ.get("CAIRN_APPS_K8S_INGRESS_FROM", "")
        if peers:
            try:
                ingress = json.loads(peers)
            except ValueError:
                raise KubeError("CAIRN_APPS_K8S_INGRESS_FROM: network policy peers as JSON", 503)
        else:
            cairn_ns = os.environ.get("CAIRN_APPS_K8S_CAIRN_NAMESPACE", "")
            if not cairn_ns:
                raise KubeError("set CAIRN_APPS_K8S_INGRESS_FROM, or CAIRN_APPS_K8S_CAIRN_NAMESPACE for Cairn's API pods", 503)
            ingress = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": cairn_ns}},
                        "podSelector": {"matchLabels": {"app.kubernetes.io/component": "api"}}}]
        res = os.environ.get("CAIRN_APPS_K8S_RESOURCES", "")
        return cls(ns, os.environ.get("CAIRN_APPS_K8S_API", ""), os.environ.get("CAIRN_APPS_K8S_TOKEN_FILE", ""),
                   os.environ.get("CAIRN_APPS_K8S_CA_FILE", ""), os.environ.get("CAIRN_APPS_K8S_ACCESS", "cluster-ip"),
                   os.environ.get("CAIRN_APPS_K8S_NODE_ADDRESS", ""), ingress,
                   os.environ.get("CAIRN_APPS_K8S_GIT_IMAGE", GIT_IMAGE), json.loads(res) if res else None)

    # ---- the API ------------------------------------------------------------------------------
    def _client(self):
        import httpx

        try:
            token = open(self.token_file).read().strip()
        except OSError as e:
            raise KubeError(f"kubernetes: the service account token: {e}", 503)
        return httpx.AsyncClient(base_url=self.api, verify=self.verify, timeout=30, headers={"Authorization": f"Bearer {token}"})

    async def _call(self, method: str, path: str, body: Any = None, ok404: bool = False, ctype: str = "") -> Optional[dict]:
        async with self._client() as c:
            headers = {"Content-Type": ctype} if ctype else {}
            content = None if body is None else (body if isinstance(body, (str, bytes)) else json.dumps(body))
            r = await c.request(method, path, content=content, headers=headers)
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 300:
            raise KubeError(f"kubernetes: {method} {path}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content else {}

    def _paths(self, app: str) -> dict[str, str]:
        n, ns = object_name(app), self.namespace
        return {"Secret": f"/api/v1/namespaces/{ns}/secrets/{n}",
                "Deployment": f"/apis/apps/v1/namespaces/{ns}/deployments/{n}",
                "Service": f"/api/v1/namespaces/{ns}/services/{n}",
                "NetworkPolicy": f"/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies/{n}"}

    async def _apply(self, path: str, obj: dict) -> dict:
        return await self._call("PATCH", f"{path}?fieldManager=cairn&force=true", obj, ctype="application/apply-patch+yaml")

    # ---- objects ------------------------------------------------------------------------------
    def manifests(self, app: dict, key: str, git: Optional[dict], replicas: int) -> list[dict]:
        """The four objects for ``app``. ``git``: {url, branch, commit, path, header} or None."""
        name, port = app["name"], int(app["port"])
        n, labels = object_name(name), {LABEL: name, "app.kubernetes.io/managed-by": "cairn"}
        meta = {"name": n, "namespace": self.namespace, "labels": labels}
        secret = {"CAIRN_APP_KEY": key}
        if git and git.get("header"):
            secret.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": git["header"]})
        base = f"apps/{name}"
        command = [c.replace("{port}", str(port)).replace("{base}", base) for c in app["command"]]
        secure = {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                  "seccompProfile": {"type": "RuntimeDefault"}}
        workdir = "/srv/app" + (f"/{git['path']}" if git and git.get("path") else "")
        container = {"name": "app", "image": app["image"], "command": command, "workingDir": workdir if git else None,
                     "ports": [{"name": "http", "containerPort": port}],
                     "env": [{"name": "PORT", "value": str(port)}, {"name": "CAIRN_APP_BASE", "value": f"/{base}/"},
                             {"name": "HOME", "value": "/tmp"},
                             {"name": "CAIRN_APP_KEY", "valueFrom": {"secretKeyRef": {"name": n, "key": "CAIRN_APP_KEY"}}}],
                     "readinessProbe": {"tcpSocket": {"port": port}, "periodSeconds": 2, "failureThreshold": 3},
                     "resources": self.resources, "securityContext": secure,
                     "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}] + ([{"name": "src", "mountPath": "/srv/app"}] if git else [])}
        if not git:
            container.pop("workingDir")
        spec: dict[str, Any] = {"automountServiceAccountToken": False, "enableServiceLinks": False,
                                "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
                                "containers": [container],
                                "volumes": [{"name": "tmp", "emptyDir": {}}] + ([{"name": "src", "emptyDir": {}}] if git else [])}
        if git:
            env = [{"name": "CAIRN_GIT_URL", "value": git["url"]}, {"name": "CAIRN_GIT_BRANCH", "value": git["branch"]},
                   {"name": "CAIRN_GIT_COMMIT", "value": git["commit"]}, {"name": "HOME", "value": "/tmp"}]
            if git.get("header"):
                env += [{"name": k, "valueFrom": {"secretKeyRef": {"name": n, "key": k}}}
                        for k in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")]
            spec["initContainers"] = [{
                "name": "checkout", "image": self.git_image, "command": ["sh", "-ec",
                    'git clone --quiet --no-checkout --branch "$CAIRN_GIT_BRANCH" -- "$CAIRN_GIT_URL" /srv/app\n'
                    'git -C /srv/app checkout --quiet "$CAIRN_GIT_COMMIT"'],
                "env": env, "securityContext": secure, "resources": {"limits": {"memory": "256Mi"}},
                "volumeMounts": [{"name": "src", "mountPath": "/srv/app"}, {"name": "tmp", "mountPath": "/tmp"}]}]
        return [
            {"apiVersion": "v1", "kind": "Secret", "metadata": meta, "type": "Opaque", "stringData": secret},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta,
             "spec": {"replicas": replicas, "selector": {"matchLabels": {LABEL: name}}, "revisionHistoryLimit": 2,
                      "template": {"metadata": {"labels": labels, "annotations": {"cairn.io/commit": (git or {}).get("commit", "")}},
                                   "spec": spec}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": meta,
             "spec": {"selector": {LABEL: name}, "ports": [{"name": "http", "port": port, "targetPort": port}],
                      # Node ports keep the caller's address (Local), so the network policy sees that it is Cairn.
                      **({"type": "NodePort", "externalTrafficPolicy": "Local"} if self.access == "node-port"
                         else {"type": "ClusterIP"})}},
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta,
             # In: Cairn only. Out is left open: apps query Trino as their viewer, and git is fetched at start.
             "spec": {"podSelector": {"matchLabels": {LABEL: name}}, "policyTypes": ["Ingress"],
                      "ingress": [{"from": self.ingress_from, "ports": [{"port": port, "protocol": "TCP"}]}]}},
        ]

    async def deploy(self, app: dict, key: str, git: Optional[dict]) -> None:
        """Applies the app's objects. A running app keeps running, on the new commit."""
        paths = self._paths(app["name"])
        current = await self._call("GET", paths["Deployment"], ok404=True)
        replicas = int(((current or {}).get("spec") or {}).get("replicas") or 0)
        for obj in self.manifests(app, key, git, replicas):
            await self._apply(paths[obj["kind"]], obj)

    async def delete(self, app: str) -> None:
        for path in self._paths(app).values():
            await self._call("DELETE", path, ok404=True)
        self._stamped.pop(app, None)

    # ---- running ------------------------------------------------------------------------------
    async def _scale(self, app: str, replicas: int) -> None:
        await self._call("PATCH", self._paths(app)["Deployment"] + "/scale", {"spec": {"replicas": replicas}},
                         ctype="application/merge-patch+json")

    async def _stamp(self, app: str) -> None:
        now = time.time()
        if now - self._stamped.get(app, 0) < 60:
            return
        self._stamped[app] = now
        await self._call("PATCH", self._paths(app)["Deployment"],
                         {"metadata": {"annotations": {STAMP: str(int(now))}}}, ctype="application/merge-patch+json")

    async def upstream(self, app: dict) -> str:
        """The app's address, started first if it is stopped."""
        name = app["name"]
        paths = self._paths(name)
        async with self._waking.setdefault(name, asyncio.Lock()):
            d = await self._call("GET", paths["Deployment"], ok404=True)
            if d is None:
                raise KubeError(f"{name} is not deployed", 503)
            if int((d.get("spec") or {}).get("replicas") or 0) < 1:
                self._stamped.pop(name, None)
                await self._stamp(name)                  # before it starts: a sweep must not stop it again
                await self._scale(name, 1)
            end = time.monotonic() + self.start_timeout
            while int(((d or {}).get("status") or {}).get("readyReplicas") or 0) < 1:
                if time.monotonic() > end:
                    raise KubeError(f"{name} did not start in {int(self.start_timeout)} s", 504)
                await asyncio.sleep(1)
                d = await self._call("GET", paths["Deployment"])
        await self._stamp(name)
        svc = await self._call("GET", paths["Service"])
        port = svc["spec"]["ports"][0]
        if self.access == "node-port":
            return f"http://{self.node_address}:{port['nodePort']}"
        return f"http://{svc['spec']['clusterIP']}:{port['port']}"

    async def running(self, app: str) -> bool:
        d = await self._call("GET", self._paths(app)["Deployment"], ok404=True)
        return bool(d) and int((d.get("spec") or {}).get("replicas") or 0) > 0

    async def stop_idle(self, idle_minutes: dict[str, int], now: Optional[float] = None) -> list[str]:
        """Scales to zero the apps whose last request is older than their idle time."""
        now = now or time.time()
        out = await self._call("GET", f"/apis/apps/v1/namespaces/{self.namespace}/deployments?labelSelector={LABEL}")
        stopped = []
        for d in out.get("items", []):
            name = d["metadata"]["labels"].get(LABEL)
            if name not in idle_minutes or int((d.get("spec") or {}).get("replicas") or 0) < 1:
                continue
            stamp = int(((d["metadata"].get("annotations") or {}).get(STAMP)) or 0)
            if now - stamp > idle_minutes[name] * 60:
                await self._scale(name, 0)
                self._stamped.pop(name, None)
                stopped.append(name)
        return stopped
