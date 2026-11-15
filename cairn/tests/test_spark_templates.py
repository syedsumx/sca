"""cairn-spark's driver pod template: a job's own template keeps what it has (volumes, sidecars,
tolerations) and gains the projected workload token; clashes are refused, not overwritten."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "spark", "src"))

from cairn_spark import cli  # noqa: E402
from cairn_spark.kubernetes import TOKEN_DIR, VOLUME, merge_driver_template  # noqa: E402


def test_a_jobs_template_gains_the_token():
    own = {"apiVersion": "v1", "kind": "Pod", "spec": {
        "tolerations": [{"key": "spot", "operator": "Exists"}],
        "volumes": [{"name": "job", "configMap": {"name": "job"}}],
        "containers": [{"name": "spark-kubernetes-driver", "volumeMounts": [{"name": "job", "mountPath": "/job"}]},
                       {"name": "sidecar", "image": "busybox"}]}}
    out = merge_driver_template(own, "cairn")
    assert [v["name"] for v in out["spec"]["volumes"]] == ["job", VOLUME]
    assert out["spec"]["volumes"][1]["projected"]["sources"][0]["serviceAccountToken"]["audience"] == "cairn"
    driver = out["spec"]["containers"][0]
    assert [m["mountPath"] for m in driver["volumeMounts"]] == ["/job", TOKEN_DIR]
    assert out["spec"]["tolerations"] and out["spec"]["containers"][1] == {"name": "sidecar", "image": "busybox"}
    assert own["spec"]["volumes"] == [{"name": "job", "configMap": {"name": "job"}}]       # the original is untouched
    # No driver container named yet: one is added; a JVM job's driver signs in first.
    bare = merge_driver_template({"spec": {"nodeSelector": {"pool": "spark"}}}, jvm=True)
    assert bare["kind"] == "Pod" and bare["spec"]["containers"][0]["command"] == ["cairn-spark-driver"]


def test_clashes_are_refused():
    with pytest.raises(ValueError, match="already has a volume"):
        merge_driver_template({"spec": {"volumes": [{"name": VOLUME, "emptyDir": {}}]}})
    with pytest.raises(ValueError, match="already mounts"):
        merge_driver_template({"spec": {"containers": [{"name": "spark-kubernetes-driver",
                                                        "volumeMounts": [{"name": "x", "mountPath": TOKEN_DIR}]}]}})
    with pytest.raises(ValueError, match="is a Pod"):
        merge_driver_template({"kind": "Deployment"})


def test_the_jobs_template_setting_is_replaced_by_the_merged_one():
    rest = ["--master", "k8s://x", "--conf", "spark.kubernetes.driver.podTemplateFile=/t.json",
            "--conf=spark.kubernetes.driver.podTemplateFile=/u.json", "--conf", "spark.executor.instances=1", "job.py"]
    assert cli._drop_conf(rest, "spark.kubernetes.driver.podTemplateFile") == [
        "--master", "k8s://x", "--conf", "spark.executor.instances=1", "job.py"]


def test_governed_read_names_an_ipv6_trino_with_brackets(monkeypatch):
    import cairn_spark

    monkeypatch.setattr(cairn_spark, "trino_token", lambda *a, **k: "t")
    seen = {}

    class Reader:
        def jdbc(self, url, table, properties):
            seen.update(url=url, table=table)
            return "df"

    spark = type("S", (), {"read": Reader()})()
    assert cairn_spark.governed_read(spark, "sales.orders", cairn_url="http://c", token="x",
                                     trino_url="https://[::1]:8443") == "df"
    assert seen["url"] == "jdbc:trino://[::1]:8443/lakehouse?SSL=true" and seen["table"] == '"sales"."orders"'
    with pytest.raises(cairn_spark.CairnSparkError, match="no host"):
        cairn_spark.governed_read(spark, "sales.orders", cairn_url="http://c", token="x", trino_url="https://:8443")
