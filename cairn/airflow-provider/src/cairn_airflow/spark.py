"""Spark applications on Kubernetes from Airflow, through the Spark Operator, with no secret anywhere.

The application is a ``SparkApplication`` (``sparkoperator.k8s.io/v1beta2``) built by
:func:`cairn_spark.kubernetes.spark_application`: a Python driver with a projected service-account
token, which signs in to Cairn as the user its service account is bound to (workload identity). The task
creates it, waits for the Spark Operator to finish it, and fails with the application's error if it
fails; killing the task deletes the application (and so its pods)::

    from cairn_airflow.spark import CairnSparkApplicationOperator

    CairnSparkApplicationOperator(
        task_id="orders", name="orders", namespace="pipelines", service_account="svc-spark",
        image="registry.example.com/cairn/spark:0.1.0", main="/jobs/orders.py",
        cairn_url="https://cairn.example.com/cairn/api", catalog_uri="https://catalog.example.com")

It talks to the cluster as the Airflow worker's own service account (in a pod), or with
``kube_config_file``. The worker needs create, get, delete and watch on ``sparkapplications`` in
``namespace``; nothing on secrets.
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Optional, Sequence

try:  # Airflow 3
    from airflow.sdk import BaseOperator
except ImportError:  # pragma: no cover
    from airflow.models import BaseOperator

try:
    from airflow.exceptions import AirflowException
except ImportError:  # pragma: no cover
    AirflowException = RuntimeError  # type: ignore[misc,assignment]

GROUP, VERSION, PLURAL = "sparkoperator.k8s.io", "v1beta2", "sparkapplications"
DONE, FAILED = {"COMPLETED"}, {"FAILED", "SUBMISSION_FAILED", "UNKNOWN", "INVALIDATING"}


def _api(kube_config_file: Optional[str]):
    from kubernetes import client, config

    if kube_config_file:
        config.load_kube_config(config_file=kube_config_file)
    else:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
    return client.CustomObjectsApi()


def run_name(name: str, run_id: str, try_number: int) -> str:
    """One application per task try: a DNS-1123 name under 63 characters."""
    base = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40] or "spark"
    return f"{base}-{hashlib.sha256(f'{run_id}|{try_number}'.encode()).hexdigest()[:10]}-{try_number}"


class CairnSparkApplicationOperator(BaseOperator):
    template_fields: Sequence[str] = ("name", "main", "arguments", "image", "namespace")

    def __init__(self, *, name: str, image: str, main: str, service_account: str, cairn_url: str,
                 catalog_uri: str, namespace: str = "default", arguments: Sequence[str] = (),
                 executors: int = 2, executor_cores: int = 1, executor_memory: str = "2g",
                 driver_cores: int = 1, driver_memory: str = "1g", spark_conf: Optional[dict] = None,
                 kube_config_file: Optional[str] = None, poll_interval: float = 10.0,
                 delete_on_success: bool = False, **kwargs: Any):
        super().__init__(**kwargs)
        self.name, self.image, self.main, self.service_account = name, image, main, service_account
        self.cairn_url, self.catalog_uri, self.namespace = cairn_url, catalog_uri, namespace
        self.arguments, self.spark_conf = list(arguments), dict(spark_conf or {})
        self.sizing = {"executors": executors, "executor_cores": executor_cores, "executor_memory": executor_memory,
                       "driver_cores": driver_cores, "driver_memory": driver_memory}
        self.kube_config_file, self.poll_interval = kube_config_file, poll_interval
        self.delete_on_success = delete_on_success
        self._app: Optional[str] = None

    def manifest(self, app: str, context: Any = None) -> dict:
        from cairn_spark.kubernetes import spark_application

        labels = {}
        if context:
            ti = context.get("ti")
            labels = {"airflow.apache.org/dag-id": re.sub(r"[^A-Za-z0-9._-]", "_", str(getattr(ti, "dag_id", "")))[:63],
                      "airflow.apache.org/task-id": re.sub(r"[^A-Za-z0-9._-]", "_", str(getattr(ti, "task_id", "")))[:63]}
        return spark_application(app, image=self.image, main=self.main, service_account=self.service_account,
                                 cairn_url=self.cairn_url, catalog_uri=self.catalog_uri, namespace=self.namespace,
                                 arguments=self.arguments, spark_conf=self.spark_conf, labels=labels, **self.sizing)

    def execute(self, context: Any) -> dict:
        api = _api(self.kube_config_file)
        ti = context.get("ti")
        app = run_name(self.name, str(context.get("run_id") or ""), int(getattr(ti, "try_number", 1) or 1))
        self._app = app
        api.create_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL, self.manifest(app, context))
        self.log.info("SparkApplication %s/%s created", self.namespace, app)
        state, first = "", True
        while True:
            obj = api.get_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL, app)
            st = (obj.get("status") or {}).get("applicationState") or {}
            now = st.get("state") or ""
            if now != state or first:
                state, first = now, False
                self.log.info("SparkApplication %s: %s", app, state or "pending")
            if state in DONE:
                if self.delete_on_success:
                    api.delete_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL, app)
                return {"application": app, "namespace": self.namespace, "state": state,
                        "spark_application_id": (obj.get("status") or {}).get("sparkApplicationId")}
            if state in FAILED:
                raise AirflowException(f"SparkApplication {app} {state}: {st.get('errorMessage') or 'no message'}")
            time.sleep(self.poll_interval)

    def on_kill(self) -> None:
        if self._app:
            try:
                _api(self.kube_config_file).delete_namespaced_custom_object(GROUP, VERSION, self.namespace, PLURAL,
                                                                            self._app)
            except Exception:  # noqa: BLE001 - already gone
                self.log.warning("could not delete SparkApplication %s", self._app)
