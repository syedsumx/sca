"""Managed NiFi deployments on Kubernetes: a StatefulSet Cairn renders itself (there is no official Apache NiFi chart,
and no third-party operator is used), applied server-side through the Kubernetes API with the field manager ``cairn``
and the label ``cairn.io/nifi-deployment``, pruning what a new render no longer has (managed Airflow's API client,
:class:`cairn.airflow_deploy.k8s_runtime.KubernetesRuntime`).

Per deployment ``<name>``, in a namespace of its own (``CAIRN_NIFI_K8S_NAMESPACE_PREFIX`` + name, Pod Security
``restricted``):

* a ``StatefulSet`` (``nifi``, one pod) from the upstream image ``apache/nifi:<version>``, unmodified, as NiFi's user
  (uid 1000, non-root, no privilege escalation, every capability dropped, the runtime's seccomp profile); its ``conf``
  (the flow) and repositories on a ``PersistentVolumeClaim`` (``data``; an init container copies the image's ``conf``
  there the first time), its logs on an ``emptyDir``;
* a ``Secret`` (``cairn-nifi-files``) with what :mod:`cairn.nifi.keys` derives (the keystore and its password, the
  sensitive properties key, the single user's bcrypt hash, the start script), mounted read-only for NiFi's group:
  never environment variables;
* ``Services`` (``nifi`` and the headless ``nifi-hl``) and a ``NetworkPolicy`` that admits traffic to NiFi only from
  Cairn's namespace (``CAIRN_NIFI_K8S_CAIRN_NAMESPACE``): people reach NiFi only through Cairn's proxy.

The pod has no service account token. Cairn needs to manage namespaces and the kinds above in them (the chart's
``cairn.nifi.deployments`` ClusterRole).
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Any, Optional

from cairn.airflow_deploy.k8s_runtime import SA, KubernetesRuntime as _Kube, _b64
from cairn.databases.docker_runtime import RuntimeError_
from cairn.nifi.docker_runtime import DEFAULT_IMAGES, VOLUMES, env
from cairn.nifi.keys import CAIRN_DIR, NIFI_HOME, NIFI_UID

LABEL = "cairn.io/nifi-deployment"
PRUNED = ("ServiceAccount", "Service", "NetworkPolicy", "StatefulSet")      # the kinds a render holds, but its Secret


class KubernetesRuntime(_Kube):
    name = "kubernetes"
    label = LABEL

    def __init__(self, api: str = "", token_file: str = "", ca_file: str = "", namespace_prefix: str = "cairn-nifi-",
                 images: Optional[dict] = None, cairn_namespace: str = "cairn", storage_class: str = "",
                 start_timeout: float = 900):
        # Not managed Airflow's constructor (that one needs Airflow's chart): only its API client is shared.
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.api = (api or (f"https://{host}:{port}" if host else "")).rstrip("/")
        if not self.api:
            raise RuntimeError_("managed NiFi on Kubernetes: no API server (not in a cluster, and no "
                                "CAIRN_NIFI_K8S_API)", 503)
        self.token_file = token_file or f"{SA}/token"
        self.verify: Any = ca_file or (f"{SA}/ca.crt" if os.path.exists(f"{SA}/ca.crt") else True)
        self.prefix, self.cairn_namespace, self.storage_class = namespace_prefix, cairn_namespace, storage_class
        self.images = {str(k): v for k, v in (images or DEFAULT_IMAGES).items()}
        self.start_timeout = start_timeout

    @classmethod
    def from_env(cls) -> "KubernetesRuntime":
        import json

        e = os.environ.get
        images = e("CAIRN_NIFI_K8S_IMAGES", "")
        return cls(e("CAIRN_NIFI_K8S_API", ""), e("CAIRN_NIFI_K8S_TOKEN_FILE", ""), e("CAIRN_NIFI_K8S_CA_FILE", ""),
                   e("CAIRN_NIFI_K8S_NAMESPACE_PREFIX", "cairn-nifi-"), json.loads(images) if images else None,
                   e("CAIRN_NIFI_K8S_CAIRN_NAMESPACE", "cairn"), e("CAIRN_NIFI_K8S_STORAGE_CLASS", ""))

    def image_for(self, version: str) -> str:
        if str(version) not in self.images:
            raise RuntimeError_(f"NiFi {version} is not offered here (CAIRN_NIFI_K8S_IMAGES: "
                                f"{', '.join(sorted(self.images))})", 400)
        return self.images[str(version)]

    # ---- addresses -------------------------------------------------------------------------------------------------
    def api_url(self, dep: dict) -> str:
        return f"https://nifi.{self.namespace(dep['name'])}.svc:8443"

    def tls_hosts(self, dep: dict) -> list[str]:
        ns = self.namespace(dep["name"])
        return [f"nifi.{ns}.svc", f"nifi.{ns}.svc.cluster.local", f"nifi-0.nifi-hl.{ns}.svc",
                f"nifi-0.nifi-hl.{ns}.svc.cluster.local"]

    def proxy_hosts(self, dep: dict) -> list[str]:
        return [f"nifi.{self.namespace(dep['name'])}.svc:8443"]

    # ---- the render ------------------------------------------------------------------------------------------------
    def render(self, dep: dict, image: str) -> list[dict]:
        name, ns = dep["name"], self.namespace(dep["name"])
        labels = {self.label: name}
        res = dep["spec"]["resources"]
        files = {p.rsplit("/", 1)[-1]: c for p, (c, _mode) in dep["files"].items()}
        # What restarts the pod when it changes: the files, but not the certificates and stores, written afresh each
        # time (fresh signatures and salts) from keys that stay the same.
        digest = hashlib.sha256(b"".join(k.encode() + v for k, v in sorted(files.items())
                                         if not k.endswith((".p12", ".crt")))).hexdigest()[:16]
        mounts = [{"name": "data", "mountPath": f"{NIFI_HOME}/{v}", "subPath": v} for v in VOLUMES]
        mounts += [{"name": "cairn", "mountPath": CAIRN_DIR, "readOnly": True},
                   {"name": "logs", "mountPath": f"{NIFI_HOME}/logs"}]
        hardened = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}, "runAsNonRoot": True,
                    "runAsUser": NIFI_UID, "runAsGroup": NIFI_UID, "seccompProfile": {"type": "RuntimeDefault"}}
        container = {
            "name": "nifi", "image": image, "command": ["bash", f"{CAIRN_DIR}/start.sh"], "workingDir": NIFI_HOME,
            "env": [{"name": k, "value": v} for k, v in (x.split("=", 1) for x in env(dep, dep["proxy_hosts"],
                                                                                   dep["context_path"]))],
            "ports": [{"name": "https", "containerPort": 8443}],
            "resources": {"requests": {"cpu": str(res["cpu"]), "memory": f"{int(res['memory_mb'])}Mi"},
                          "limits": {"memory": f"{int(res['memory_mb'])}Mi"}},
            "readinessProbe": {"tcpSocket": {"port": 8443}, "periodSeconds": 10, "failureThreshold": 3},
            "livenessProbe": {"tcpSocket": {"port": 8443}, "initialDelaySeconds": 180, "periodSeconds": 20,
                              "failureThreshold": 6},
            "volumeMounts": mounts, "securityContext": hardened}
        init = {"name": "conf", "image": image, "command": ["bash", "-c",
                f"[ -f /data/conf/nifi.properties ] || {{ mkdir -p /data/conf && cp -a {NIFI_HOME}/conf/. /data/conf/; }};"
                f" for d in {' '.join(v for v in VOLUMES if v != 'conf')}; do mkdir -p /data/$d; done"],
                "volumeMounts": [{"name": "data", "mountPath": "/data"}], "securityContext": hardened,
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}}}
        claim: dict[str, Any] = {"metadata": {"name": "data", "labels": labels},
                                 "spec": {"accessModes": ["ReadWriteOnce"],
                                          "resources": {"requests": {"storage": f"{int(res['storage_gb'])}Gi"}}}}
        if self.storage_class:
            claim["spec"]["storageClassName"] = self.storage_class
        sts = {"apiVersion": "apps/v1", "kind": "StatefulSet",
               "metadata": {"name": "nifi", "namespace": ns, "labels": labels},
               "spec": {"replicas": 1, "serviceName": "nifi-hl", "podManagementPolicy": "OrderedReady",
                        "selector": {"matchLabels": {**labels, "app.kubernetes.io/name": "nifi"}},
                        "template": {"metadata": {"labels": {**labels, "app.kubernetes.io/name": "nifi"},
                                                  "annotations": {"cairn.io/files": digest}},
                                     "spec": {"serviceAccountName": "nifi", "automountServiceAccountToken": False,
                                              "securityContext": {"runAsNonRoot": True, "runAsUser": NIFI_UID,
                                                                  "runAsGroup": NIFI_UID, "fsGroup": NIFI_UID,
                                                                  "seccompProfile": {"type": "RuntimeDefault"}},
                                              "terminationGracePeriodSeconds": 60, "initContainers": [init],
                                              "containers": [container],
                                              "volumes": [{"name": "cairn", "secret": {"secretName": "cairn-nifi-files",
                                                                                       "defaultMode": 0o440}},
                                                          {"name": "logs", "emptyDir": {"sizeLimit": "2Gi"}}]}},
                        "volumeClaimTemplates": [claim]}}
        svc_spec = {"selector": {**labels, "app.kubernetes.io/name": "nifi"},
                    "ports": [{"name": "https", "port": 8443, "targetPort": 8443}]}
        return [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {
                **labels, "app.kubernetes.io/managed-by": "cairn", "pod-security.kubernetes.io/enforce": "restricted"}}},
            {"apiVersion": "v1", "kind": "ServiceAccount", "automountServiceAccountToken": False,
             "metadata": {"name": "nifi", "namespace": ns, "labels": labels}},
            {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
             "metadata": {"name": "cairn-nifi-files", "namespace": ns, "labels": labels},
             "data": {k: _b64(v) for k, v in sorted(files.items())}},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "nifi", "namespace": ns, "labels": labels},
             "spec": svc_spec},
            {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "nifi-hl", "namespace": ns, "labels": labels},
             "spec": {**svc_spec, "clusterIP": "None"}},
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
             "metadata": {"name": "cairn-nifi-ingress", "namespace": ns, "labels": labels},
             "spec": {"podSelector": {}, "policyTypes": ["Ingress"], "ingress": [{
                 "from": [{"namespaceSelector": {"matchLabels": {
                     "kubernetes.io/metadata.name": self.cairn_namespace}}}],
                 "ports": [{"protocol": "TCP", "port": 8443}]}]}},
            sts,
        ]

    def apply(self, dep: dict, image: str) -> list[dict]:
        objs = self.render(dep, image)
        for o in objs:
            self._apply(o)
        self._prune(dep, objs)
        return objs

    def _prune(self, dep: dict, objs: list[dict]) -> None:
        """What an earlier render had and this one does not, removed, among the kinds a render holds (what the
        chart's ClusterRole lets Cairn list). Never the StatefulSet's volume (its claim is made by Kubernetes from the
        template: it goes only with the namespace) nor a Secret."""
        from cairn.airflow_deploy.k8s_runtime import KINDS

        ns = self.namespace(dep["name"])
        keep = {(o["kind"], o["metadata"]["name"]) for o in objs}
        for _av, kind, base, plural in KINDS:
            if kind not in PRUNED:
                continue
            got = self._call("GET", f"{base}/namespaces/{ns}/{plural}", ok404=True,
                             params={"labelSelector": f"{self.label}={dep['name']}"}) or {}
            for item in got.get("items") or []:
                if (kind, item["metadata"]["name"]) not in keep:
                    self._call("DELETE", f"{base}/namespaces/{ns}/{plural}/{item['metadata']['name']}", ok404=True)

    # ---- lifecycle -------------------------------------------------------------------------------------------------
    def create(self, dep: dict, image: str) -> dict:
        self.apply(dep, image)
        return {"image": image}

    def recreate(self, dep: dict, image: str) -> dict:
        """Scaling and upgrades: the render applied again and the pod rolls; its volume stays. A StatefulSet's claim
        template cannot change, so it is kept as it was made, and a bigger ``storage_gb`` is asked of the volume
        itself (its storage class must allow expansion)."""
        ns = self.namespace(dep["name"])
        sts = self._call("GET", f"/apis/apps/v1/namespaces/{ns}/statefulsets/nifi", ok404=True)
        objs = self.render(dep, image)
        if sts:
            made = (sts.get("spec") or {}).get("volumeClaimTemplates") or []
            for o in objs:
                if o["kind"] == "StatefulSet" and made:
                    o["spec"]["volumeClaimTemplates"] = [
                        {"metadata": {"name": c["metadata"]["name"], "labels": (c["metadata"].get("labels") or {})},
                         "spec": {k: v for k, v in c["spec"].items() if k in ("accessModes", "resources",
                                                                            "storageClassName")}} for c in made]
            want = f"{int(dep['spec']['resources']['storage_gb'])}Gi"
            pvc = self._call("GET", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/data-nifi-0", ok404=True)
            have = (((pvc or {}).get("spec") or {}).get("resources") or {}).get("requests", {}).get("storage")
            if pvc and have != want:
                self._call("PATCH", f"/api/v1/namespaces/{ns}/persistentvolumeclaims/data-nifi-0",
                           {"spec": {"resources": {"requests": {"storage": want}}}},
                           ctype="application/merge-patch+json")
        for o in objs:
            self._apply(o)
        self._prune(dep, objs)
        return {"image": image}

    def _workloads(self, dep: dict) -> list[dict]:
        got = self._call("GET", f"/apis/apps/v1/namespaces/{self.namespace(dep['name'])}/statefulsets", ok404=True,
                         params={"labelSelector": f"{self.label}={dep['name']}"}) or {}
        return [{**item, "kind": "StatefulSet"} for item in got.get("items") or []]

    def state(self, dep: dict) -> dict:
        comps = []
        for w in self._workloads(dep):
            want = int((w.get("spec") or {}).get("replicas") or 0)
            ready = int((w.get("status") or {}).get("readyReplicas") or 0)
            comps.append({"component": w["metadata"]["name"], "kind": w["kind"], "replicas": want,
                          "ready_replicas": ready, "running": want > 0 and ready >= want})
        return {"runtime": "kubernetes", "namespace": self.namespace(dep["name"]), "components": comps,
                "running": any(c["running"] for c in comps), "ready": bool(comps) and all(c["running"] for c in comps)}

    def wait_ready(self, dep: dict, ready, timeout: Optional[float] = None) -> None:
        end = time.time() + (timeout or self.start_timeout)
        last: Any = None
        while time.time() < end:
            try:
                if self.state(dep)["ready"] and ready():
                    return
            except Exception as e:  # noqa: BLE001 - not up yet
                last = e
            time.sleep(5)
        raise RuntimeError_(f"{dep['name']} was not ready after {int(timeout or self.start_timeout)} s: {last}", 504)

    def logs(self, dep: dict, tail: int = 200) -> str:
        ns = self.namespace(dep["name"])
        got = self._call("GET", f"/api/v1/namespaces/{ns}/pods",
                         params={"labelSelector": f"{self.label}={dep['name']}"}) or {}
        out = []
        for pod in got.get("items") or []:
            text = self._call("GET", f"/api/v1/namespaces/{ns}/pods/{pod['metadata']['name']}/log",
                              params={"tailLines": str(max(1, min(int(tail), 5000))), "container": "nifi"},
                              raw=True, ok404=True)
            out.append(f"--- {pod['metadata']['name']}\n{text or ''}")
        return "\n".join(out) or "(no NiFi pod)"
