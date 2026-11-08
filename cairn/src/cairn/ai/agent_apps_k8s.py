"""Agent apps on Kubernetes (``CAIRN_AGENT_APPS_RUNTIME=kubernetes``): each deployed agent app a Deployment of its own.

Cairn applies four objects per app in one namespace (``CAIRN_AGENT_APPS_K8S_NAMESPACE``), labelled
``cairn.io/agent-app: <name>`` (server-side apply, field manager ``cairn``):

* a **Secret** holding the project's bundle (the tar.gz ``cairn agents deploy`` sent) and the runtime secret Cairn
  sends with every request, so only Cairn is answered; neither ever appears in the Deployment;
* a **Deployment** of one pod running the agent runtime image (``CAIRN_AGENT_APPS_K8S_IMAGE``, built from
  ``docker/agent-runtime.Dockerfile``): it unpacks the bundle into ``/tmp`` and serves it (``cairn.agentkit.runtime
  --bundle``). The pod has no Kubernetes credentials, runs as a non-root user without privileges on a read-only root
  file system, and holds no key of its own: each run brings the asking person's short-lived token. A project needing
  a package the image lacks exits naming it, and the deploy is refused with that message. Redeploys replace the pod
  (``Recreate``): two versions never answer side by side;
* a **Service** for its port: ``ClusterIP`` when Cairn runs in the cluster; ``NodePort`` when it reaches the pods from
  outside (``CAIRN_AGENT_APPS_K8S_ACCESS=node-port``, at ``CAIRN_AGENT_APPS_K8S_NODE_ADDRESS``);
* a **NetworkPolicy**: only Cairn reaches the pods (``CAIRN_AGENT_APPS_K8S_INGRESS_FROM``, network policy peers as
  JSON; by default Cairn's API pods in ``CAIRN_AGENT_APPS_K8S_CAIRN_NAMESPACE``). Egress stays open: the agent calls
  Cairn back (``CAIRN_AGENT_APPS_CAIRN_URL``) for its models and data.

Any Cairn replica serves any app: the address and secret are read back from the cluster. Cairn signs in with its pod's
service account (or ``CAIRN_AGENT_APPS_K8S_API``, ``…_TOKEN_FILE``, ``…_CA_FILE``); the chart's Role lets it manage
these four kinds, and read pods, in that namespace only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from typing import Any, Optional

from cairn.ai.agent_apps import AgentAppError

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
LABEL = "cairn.io/agent-app"
VERSION = "cairn.io/agent-app-version"
PORT = 8080
# A Secret holds at most 1 MiB, and its data is base64: bundles above this go through the process runtime only.
MAX_BUNDLE = 700_000


def object_name(app: str) -> str:
    return f"cairn-agent-{app}"


class KubeRuntime:
    def __init__(self, namespace: str, image: str, cairn_url: str, api: str = "", token_file: str = "",
                 ca_file: str = "", access: str = "cluster-ip", node_address: str = "",
                 ingress_from: Optional[list] = None, resources: Optional[dict] = None, start_timeout: float = 180):
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api = (api or (f"https://{host}:{port}" if host else "")).rstrip("/")
        if not self.api:
            raise AgentAppError("agent apps on Kubernetes: no API server (not in a cluster, and no "
                                "CAIRN_AGENT_APPS_K8S_API)", 503)
        if not image:
            raise AgentAppError("agent apps on Kubernetes need the runtime image (CAIRN_AGENT_APPS_K8S_IMAGE)", 503)
        if access not in ("cluster-ip", "node-port"):
            raise AgentAppError("CAIRN_AGENT_APPS_K8S_ACCESS is cluster-ip or node-port", 503)
        if access == "node-port" and not node_address:
            raise AgentAppError("CAIRN_AGENT_APPS_K8S_ACCESS=node-port needs CAIRN_AGENT_APPS_K8S_NODE_ADDRESS", 503)
        if not ingress_from:
            raise AgentAppError("agent apps on Kubernetes need the peers that may reach them "
                                "(CAIRN_AGENT_APPS_K8S_INGRESS_FROM)", 503)
        self.namespace, self.image, self.cairn_url = namespace, image, cairn_url
        self.access, self.node_address, self.ingress_from = access, node_address, ingress_from
        self.token_file = token_file or f"{SA}/token"
        self.verify: Any = ca_file or (f"{SA}/ca.crt" if os.path.exists(f"{SA}/ca.crt") else True)
        self.resources = resources or {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}}
        self.start_timeout = start_timeout
        self.procs: dict = {}                         # the process runtime's; nothing runs in Cairn's process here
        self._cache: dict[str, tuple[int, str, str]] = {}  # name -> (version, url, secret)
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, cairn_url: str) -> "KubeRuntime":
        e = os.environ.get
        peers = e("CAIRN_AGENT_APPS_K8S_INGRESS_FROM", "")
        if peers:
            try:
                ingress = json.loads(peers)
            except ValueError:
                raise AgentAppError("CAIRN_AGENT_APPS_K8S_INGRESS_FROM: network policy peers as JSON", 503)
        else:
            cairn_ns = e("CAIRN_AGENT_APPS_K8S_CAIRN_NAMESPACE", "")
            if not cairn_ns:
                raise AgentAppError("set CAIRN_AGENT_APPS_K8S_INGRESS_FROM, or CAIRN_AGENT_APPS_K8S_CAIRN_NAMESPACE "
                                    "for Cairn's API pods", 503)
            ingress = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": cairn_ns}},
                        "podSelector": {"matchLabels": {"app.kubernetes.io/component": "api"}}}]
        res = e("CAIRN_AGENT_APPS_K8S_RESOURCES", "")
        return cls(e("CAIRN_AGENT_APPS_K8S_NAMESPACE", "cairn-agents"), e("CAIRN_AGENT_APPS_K8S_IMAGE", ""), cairn_url,
                   e("CAIRN_AGENT_APPS_K8S_API", ""), e("CAIRN_AGENT_APPS_K8S_TOKEN_FILE", ""),
                   e("CAIRN_AGENT_APPS_K8S_CA_FILE", ""), e("CAIRN_AGENT_APPS_K8S_ACCESS", "cluster-ip"),
                   e("CAIRN_AGENT_APPS_K8S_NODE_ADDRESS", ""), ingress, json.loads(res) if res else None,
                   float(e("CAIRN_AGENT_APPS_K8S_START_TIMEOUT", "180")))

    # ---- the API ------------------------------------------------------------------------------
    def _call(self, method: str, path: str, body: Any = None, ok404: bool = False, ctype: str = "") -> Optional[dict]:
        import httpx

        try:
            token = open(self.token_file).read().strip()
        except OSError as e:
            raise AgentAppError(f"kubernetes: the service account token: {e}", 503)
        headers = {"Authorization": f"Bearer {token}", **({"Content-Type": ctype} if ctype else {})}
        content = None if body is None else json.dumps(body)
        try:
            with httpx.Client(base_url=self.api, verify=self.verify, timeout=30) as c:
                r = c.request(method, path, content=content, headers=headers)
        except httpx.HTTPError as e:
            raise AgentAppError(f"kubernetes: {method} {path}: {type(e).__name__}: {e}", 502)
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 300:
            raise AgentAppError(f"kubernetes: {method} {path}: {r.status_code} {r.text[:300]}", 502)
        return r.json() if r.content else {}

    def _paths(self, app: str) -> dict[str, str]:
        n, ns = object_name(app), self.namespace
        return {"Secret": f"/api/v1/namespaces/{ns}/secrets/{n}",
                "Deployment": f"/apis/apps/v1/namespaces/{ns}/deployments/{n}",
                "Service": f"/api/v1/namespaces/{ns}/services/{n}",
                "NetworkPolicy": f"/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies/{n}"}

    def _apply(self, path: str, obj: dict) -> dict:
        return self._call("PATCH", f"{path}?fieldManager=cairn&force=true", obj, ctype="application/apply-patch+yaml")

    # ---- objects ------------------------------------------------------------------------------
    def manifests(self, name: str, version: int, bundle: bytes, secret: str) -> list[dict]:
        n, labels = object_name(name), {LABEL: name, "app.kubernetes.io/managed-by": "cairn"}
        meta = {"name": n, "namespace": self.namespace, "labels": labels}
        secure = {"runAsNonRoot": True, "allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]},
                  "readOnlyRootFilesystem": True, "seccompProfile": {"type": "RuntimeDefault"}}
        container = {
            "name": "agent", "image": self.image, "command": ["python", "-m", "cairn.agentkit.runtime"],
            "args": ["--bundle", "/etc/cairn-agent/bundle.tgz", "--dir", "/tmp/project", "--mode", "app",
                     "--host", "0.0.0.0", "--port", str(PORT), "--secret-file", "/etc/cairn-agent/secret"],
            "env": [{"name": "CAIRN_URL", "value": self.cairn_url}, {"name": "HOME", "value": "/tmp"},
                    {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
            "ports": [{"name": "http", "containerPort": PORT}],
            "readinessProbe": {"httpGet": {"path": "/healthz", "port": PORT}, "periodSeconds": 2,
                               "failureThreshold": 3},
            # A project that cannot start (a missing package) says why here, and Cairn reports it to the deployer.
            "terminationMessagePolicy": "FallbackToLogsOnError",
            "resources": self.resources, "securityContext": secure,
            "volumeMounts": [{"name": "project", "mountPath": "/etc/cairn-agent", "readOnly": True},
                             {"name": "tmp", "mountPath": "/tmp"}]}
        spec = {"automountServiceAccountToken": False, "enableServiceLinks": False,
                "securityContext": {"runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001},
                "containers": [container],
                "volumes": [{"name": "project", "secret": {"secretName": n, "defaultMode": 0o440}},
                            {"name": "tmp", "emptyDir": {}}]}
        stamp = {VERSION: str(version), "cairn.io/secret": hashlib.sha256(secret.encode()).hexdigest()[:16]}
        return [
            {"apiVersion": "v1", "kind": "Secret", "metadata": {**meta, "annotations": {VERSION: str(version)}},
             "type": "Opaque", "data": {"bundle.tgz": base64.b64encode(bundle).decode(),
                                        "secret": base64.b64encode(secret.encode()).decode()}},
            {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta,
             "spec": {"replicas": 1, "selector": {"matchLabels": {LABEL: name}}, "revisionHistoryLimit": 2,
                      "strategy": {"type": "Recreate"},
                      "template": {"metadata": {"labels": labels, "annotations": stamp}, "spec": spec}}},
            {"apiVersion": "v1", "kind": "Service", "metadata": meta,
             "spec": {"selector": {LABEL: name}, "ports": [{"name": "http", "port": PORT, "targetPort": PORT}],
                      # Node ports keep the caller's address (Local), so the network policy sees that it is Cairn.
                      **({"type": "NodePort", "externalTrafficPolicy": "Local"} if self.access == "node-port"
                         else {"type": "ClusterIP"})}},
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": meta,
             "spec": {"podSelector": {"matchLabels": {LABEL: name}}, "policyTypes": ["Ingress"],
                      "ingress": [{"from": self.ingress_from, "ports": [{"port": PORT, "protocol": "TCP"}]}]}},
        ]

    # ---- the runtime interface (as ProcessRuntime) ----------------------------------------------
    def start(self, name: str, version: int, bundle: bytes) -> None:
        if len(bundle) > MAX_BUNDLE:
            raise AgentAppError(f"on Kubernetes a bundle is at most {MAX_BUNDLE} bytes (this one is {len(bundle)}): "
                                "leave data files out of the project", 413)
        paths = self._paths(name)
        cur = self._call("GET", paths["Secret"], ok404=True)
        if cur and (cur["metadata"].get("annotations") or {}).get(VERSION) == str(version):
            secret = base64.b64decode(cur["data"]["secret"]).decode()      # the same version: keep its pod
        else:
            secret = secrets.token_urlsafe(32)
        with self._lock:
            self._cache.pop(name, None)
        for obj in self.manifests(name, version, bundle, secret):
            self._apply(paths[obj["kind"]], obj)
        try:
            self._wait(name, version)
        except AgentAppError:
            self.stop(name)                            # the app stays down until a deploy works
            raise

    def _ready(self, d: Optional[dict], version: int) -> bool:
        if not d:
            return False
        tpl = ((d.get("spec") or {}).get("template") or {}).get("metadata") or {}
        st = d.get("status") or {}
        return ((tpl.get("annotations") or {}).get(VERSION) == str(version)
                and int(st.get("observedGeneration") or 0) >= int(d["metadata"].get("generation") or 0)
                and int(st.get("readyReplicas") or 0) >= 1 and int(st.get("updatedReplicas") or 0) >= 1
                and int(st.get("replicas") or 0) == int(st.get("updatedReplicas") or 0))

    def _failure(self, name: str, version: int) -> Optional[AgentAppError]:
        """Why the app's pod of this version cannot start, if it has said so."""
        pods = self._call("GET", f"/api/v1/namespaces/{self.namespace}/pods?labelSelector={LABEL}%3D{name}") or {}
        for p in pods.get("items", []):
            if ((p["metadata"].get("annotations") or {}).get(VERSION)) != str(version):
                continue
            for cs in (p.get("status") or {}).get("containerStatuses") or []:
                term = (cs.get("state") or {}).get("terminated") or (cs.get("lastState") or {}).get("terminated")
                if term and int(term.get("exitCode") or 0) != 0:
                    msg = (term.get("message") or term.get("reason") or "").strip()[-1500:]
                    status = 409 if term.get("exitCode") == 3 else 502
                    return AgentAppError(msg if status == 409 else f"the agent did not start: {msg}", status)
                waiting = ((cs.get("state") or {}).get("waiting") or {})
                if waiting.get("reason") in ("ErrImagePull", "ImagePullBackOff", "InvalidImageName",
                                             "CreateContainerConfigError"):
                    return AgentAppError(f"the agent did not start: {waiting['reason']}: "
                                         f"{(waiting.get('message') or '')[:500]}", 502)
        return None

    def _pending(self, name: str, version: int) -> str:
        """What keeps the app's pod from being ready (unschedulable, a container waiting), for the deployer."""
        pods = self._call("GET", f"/api/v1/namespaces/{self.namespace}/pods?labelSelector={LABEL}%3D{name}") or {}
        for p in pods.get("items", []):
            if ((p["metadata"].get("annotations") or {}).get(VERSION)) != str(version):
                continue
            st = p.get("status") or {}
            for c in st.get("conditions") or []:
                if c.get("status") == "False" and c.get("message"):
                    return f"{c.get('type')}: {c['message'][:500]}"
            for cs in st.get("containerStatuses") or []:
                waiting = (cs.get("state") or {}).get("waiting") or {}
                if waiting.get("reason"):
                    return f"{waiting['reason']}: {(waiting.get('message') or '')[:500]}"
            return f"pod {st.get('phase', 'unknown')}"
        return "no pod was created"

    def _wait(self, name: str, version: int) -> None:
        path = self._paths(name)["Deployment"]
        end = time.monotonic() + self.start_timeout
        while True:
            if self._ready(self._call("GET", path, ok404=True), version):
                return
            failed = self._failure(name, version)
            if failed is not None:
                raise failed
            if time.monotonic() > end:
                why = self._pending(name, version)
                raise AgentAppError(f"the agent did not start within {int(self.start_timeout)} seconds"
                                    + (f": {why}" if why else ""), 504)
            time.sleep(1)

    def endpoint(self, name: str, version: int) -> Optional[tuple[str, str]]:
        paths = self._paths(name)
        if not self._ready(self._call("GET", paths["Deployment"], ok404=True), version):
            return None
        with self._lock:
            got = self._cache.get(name)
        if got and got[0] == version:
            return got[1], got[2]
        sec = self._call("GET", paths["Secret"], ok404=True)
        svc = self._call("GET", paths["Service"], ok404=True)
        if not sec or not svc or (sec["metadata"].get("annotations") or {}).get(VERSION) != str(version):
            return None
        port = svc["spec"]["ports"][0]
        url = (f"http://{self.node_address}:{port['nodePort']}" if self.access == "node-port"
               else f"http://{svc['spec']['clusterIP']}:{port['port']}")
        secret = base64.b64decode(sec["data"]["secret"]).decode()
        with self._lock:
            self._cache[name] = (version, url, secret)
        return url, secret

    def stop(self, name: str) -> None:
        with self._lock:
            self._cache.pop(name, None)
        for path in self._paths(name).values():
            self._call("DELETE", path, ok404=True)
