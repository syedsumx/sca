"""Spark on Kubernetes without a secret in any Kubernetes object.

In cluster mode spark-submit writes the application's settings into a ConfigMap the driver pod mounts,
so a lakehouse credential (or a Cairn token, for lineage) given at submit time would be stored in the
cluster's API, readable by anyone who may read ConfigMaps in the namespace. So a cluster-mode
application is submitted with no credential at all: its driver pod gets a projected service-account
token for Cairn (audience ``cairn``, rotated by the kubelet), and the driver signs in itself, when the
program calls :func:`cairn_spark.lakehouse_session` (workload identity: the token is exchanged at Cairn
for the bound pipeline user's token). Executors need no identity: they get their settings from the
driver over Spark's RPC, and the catalog's storage credentials come with the tables.

A JVM program's catalog settings must be there before it starts, so its driver container runs
``cairn-spark-driver`` first: it signs in the same way, inside the pod, and starts Spark's driver with
the session's settings in a private file (cairn_spark.driver).

* :func:`driver_pod_template` and :func:`cluster_conf`: what ``cairn-spark submit --deploy-mode
  cluster`` adds.
* :func:`spark_application`: the same application as a Spark Operator ``SparkApplication``
  (``sparkoperator.k8s.io/v1beta2``), for ``kubectl apply`` or
  :class:`cairn_airflow.spark.CairnSparkApplicationOperator`.
"""

from __future__ import annotations

import json
import os
from typing import Optional, Sequence

TOKEN_DIR = "/var/run/secrets/cairn"
TOKEN_FILE = f"{TOKEN_DIR}/token"
VOLUME = "cairn-identity"


def identity_volume(audience: str = "cairn", expiration_seconds: int = 3600) -> dict:
    return {"name": VOLUME, "projected": {"sources": [{"serviceAccountToken": {
        "audience": audience, "expirationSeconds": int(expiration_seconds), "path": "token"}}]}}


def identity_mount() -> dict:
    return {"name": VOLUME, "mountPath": TOKEN_DIR, "readOnly": True}


def driver_env(cairn_url: str, catalog_uri: str, extra: Optional[dict] = None) -> dict:
    """What the driver needs to sign in by itself. Nothing secret."""
    env = {"CAIRN_URL": cairn_url.rstrip("/"), "CAIRN_CATALOG_URI": catalog_uri.rstrip("/"),
           "CAIRN_WORKLOAD_TOKEN_FILE": TOKEN_FILE}
    for k in ("CAIRN_SPARK_NAMESPACE", "CAIRN_SPARK_TOKEN_TTL", "CAIRN_SPARK_EVENT_LOG_DIR"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return {**env, **(extra or {})}


def driver_pod_template(audience: str = "cairn", expiration_seconds: int = 3600, jvm: bool = False) -> dict:
    """A driver pod template: the projected token, mounted read-only where the driver looks for it.
    For a JVM program (``jvm``), the driver's command is ``cairn-spark-driver``, which signs in and
    gives Spark the session's settings inside the pod (see cairn_spark.driver)."""
    container = {"name": "spark-kubernetes-driver", "volumeMounts": [identity_mount()]}
    if jvm:
        container["command"] = ["cairn-spark-driver"]
    return {"apiVersion": "v1", "kind": "Pod", "spec": {
        "volumes": [identity_volume(audience, expiration_seconds)], "containers": [container]}}


def merge_driver_template(own: dict, audience: str = "cairn", expiration_seconds: int = 3600, jvm: bool = False,
                          container_name: str = "spark-kubernetes-driver") -> dict:
    """A pod template of the job's own (its volumes, sidecars, tolerations...) with the projected token
    added to it, so the driver still signs in by itself. A template that already uses the token's volume
    name or mount path is refused rather than overwritten."""
    if not isinstance(own, dict) or own.get("kind", "Pod") != "Pod":
        raise ValueError("the driver pod template is a Pod")
    out = json.loads(json.dumps(own))
    spec = out.setdefault("spec", {})
    vols = spec.setdefault("volumes", [])
    if any(v.get("name") == VOLUME for v in vols):
        raise ValueError(f"the driver pod template already has a volume named {VOLUME}")
    vols.append(identity_volume(audience, expiration_seconds))
    containers = spec.setdefault("containers", [])
    c = next((x for x in containers if x.get("name") == container_name), None)
    if c is None:
        c = {"name": container_name}
        containers.insert(0, c)
    mounts = c.setdefault("volumeMounts", [])
    if any(m.get("mountPath") == TOKEN_DIR for m in mounts):
        raise ValueError(f"the driver pod template already mounts something at {TOKEN_DIR}")
    mounts.append(identity_mount())
    if jvm:
        c["command"] = ["cairn-spark-driver"]
    out.setdefault("apiVersion", "v1")
    out["kind"] = "Pod"
    return out


def cluster_conf(cairn_url: str, catalog_uri: str, service_account: str, template_path: str) -> dict:
    conf = {"spark.kubernetes.driver.podTemplateFile": template_path,
            "spark.kubernetes.driver.podTemplateContainerName": "spark-kubernetes-driver",
            "spark.kubernetes.authenticate.driver.serviceAccountName": service_account,
            # The driver's settings are stored in the cluster: keep anything secret-looking out of logs too.
            "spark.redaction.regex": r"(?i)secret|password|token|access[.]key|credential|apikey|api[.]key"}
    for k, v in driver_env(cairn_url, catalog_uri).items():
        conf[f"spark.kubernetes.driverEnv.{k}"] = v
    if os.environ.get("CAIRN_SPARK_EVENT_LOG_DIR"):
        conf["spark.eventLog.enabled"] = "true"
        conf["spark.eventLog.dir"] = os.environ["CAIRN_SPARK_EVENT_LOG_DIR"]
    return conf


def spark_application(name: str, *, image: str, main: str, service_account: str, cairn_url: str, catalog_uri: str,
                      namespace: str = "default", arguments: Sequence[str] = (), executors: int = 2,
                      executor_cores: int = 1, executor_memory: str = "2g", driver_cores: int = 1,
                      driver_memory: str = "1g", spark_version: str = "4.0.1", audience: str = "cairn",
                      spark_conf: Optional[dict] = None, labels: Optional[dict] = None,
                      main_class: Optional[str] = None) -> dict:
    """A Spark Operator SparkApplication running ``main`` (a path in the image, or a URL) as the Cairn
    user ``service_account`` is bound to: a Python program that calls lakehouse_session(), or with
    ``main_class`` a JVM program, whose driver pod signs in first (cairn-spark-driver)."""
    if not main_class and not main.endswith(".py"):
        raise ValueError("a JVM program needs its main_class; a Python program is a .py file")
    for k in (spark_conf or {}):
        if any(w in k.lower() for w in ("credential", "token", "secret", "password", "apikey")):
            raise ValueError(f"{k}: no secrets in a SparkApplication (they are stored in the cluster)")
    env = [{"name": k, "value": v} for k, v in driver_env(cairn_url, catalog_uri).items()]
    conf = {"spark.redaction.regex": r"(?i)secret|password|token|access[.]key|credential|apikey|api[.]key",
            **(spark_conf or {})}
    if os.environ.get("CAIRN_SPARK_EVENT_LOG_DIR"):
        conf.setdefault("spark.eventLog.enabled", "true")
        conf.setdefault("spark.eventLog.dir", os.environ["CAIRN_SPARK_EVENT_LOG_DIR"])
    return {
        "apiVersion": "sparkoperator.k8s.io/v1beta2", "kind": "SparkApplication",
        "metadata": {"name": name, "namespace": namespace, "labels": {"app.kubernetes.io/managed-by": "cairn",
                                                                     **(labels or {})}},
        "spec": {
            **({"type": "Scala", "mainClass": main_class} if main_class else {"type": "Python", "pythonVersion": "3"}),
            "mode": "cluster", "image": image, "imagePullPolicy": "IfNotPresent",
            "mainApplicationFile": main if "://" in main else f"local://{main}", "arguments": list(arguments),
            "sparkVersion": spark_version, "sparkConf": conf,
            "restartPolicy": {"type": "Never"},
            "volumes": [identity_volume(audience)],
            "driver": {"cores": driver_cores, "memory": driver_memory, "serviceAccount": service_account,
                       "env": env, "volumeMounts": [identity_mount()],
                       "labels": {"cairn.io/role": "driver"},
                       # A JVM driver signs in before Spark starts, inside its pod.
                       **({"template": {"spec": {"containers": [{"name": "spark-kubernetes-driver",
                                                                 "command": ["cairn-spark-driver"]}]}}}
                          if main_class else {})},
            # Executors get no token: their settings come from the driver, their storage keys with the tables.
            "executor": {"instances": executors, "cores": executor_cores, "memory": executor_memory,
                         "labels": {"cairn.io/role": "executor"}},
        },
    }
