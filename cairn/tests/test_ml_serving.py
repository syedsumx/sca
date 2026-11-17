"""Managed model serving (L63), the parts that need no cluster: names that Kubernetes accepts, and scaling that
keeps going for the other deployments when one of them cannot be applied. The runtime is a recording stand-in;
the Kubernetes runtime's manifests are checked against Kubernetes' own name rules."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine

from cairn.common.db import metadata
from cairn.common.security import Principal
from cairn.ml.serving import KubeRuntime, ModelServing, ServingError, model_deployments, model_load

OWNER = Principal("urn:li:corpuser:ana", "token")
# Kubernetes: object names of Services are DNS-1035 labels; label values begin and end with an alphanumeric.
DNS1035 = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")
LABEL_VALUE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9_.]*)?[A-Za-z0-9])?$")


class Gateway:
    def may_manage(self, p, model):
        return True

    def may_use(self, p, model):
        return True

    def registry_get(self, p, model, version):
        return 200, {}, b"{}"


class Runtime:
    def __init__(self, fail: set[str] = frozenset()):
        self.fail, self.scaled = set(fail), {}

    def scale(self, d, n):
        if d["name"] in self.fail:
            raise ServingError("kubernetes: refused", 502)
        self.scaled[d["name"]] = n

    def running(self, d):
        return self.scaled.get(d["name"], 0)

    def delete(self, d):
        self.scaled.pop(d["name"], None)


@pytest.fixture
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path}/serving.db")
    metadata.create_all(e, tables=[model_deployments, model_load])
    return e


def test_names_kubernetes_refuses_are_refused_up_front(engine):
    ms = ModelServing(engine, Runtime(), Gateway())
    for bad in ("churn-", "a-", "-a", "Churn", "9a", "a" * 42):
        with pytest.raises(ServingError):
            ms.deploy(OWNER, {"name": bad, "model": "churn", "version": "1"})
    assert ms.list(OWNER) == []                                     # nothing recorded for a refused name
    kube = KubeRuntime("cairn-models", "http://mlflow:5000", "img", [{"podSelector": {}}], api="https://k8s")
    for good in ("a", "churn", "churn-v2", "a" * 41):
        d = ms.deploy(OWNER, {"name": good, "model": "churn", "version": "1"})
        for obj in kube.manifests({**d, "id": "x"}, 1):
            assert DNS1035.match(obj["metadata"]["name"]) and len(obj["metadata"]["name"]) <= 63
            assert all(LABEL_VALUE.match(v) and len(v) <= 63 for v in obj["metadata"]["labels"].values())


def test_one_deployment_that_cannot_be_applied_does_not_stop_scaling_the_others(engine):
    rt = Runtime()
    ms = ModelServing(engine, rt, Gateway())
    for n in ("alpha", "beta", "gamma"):
        ms.deploy(OWNER, {"name": n, "model": "churn", "version": "1", "min_replicas": 0, "max_replicas": 2})
    # Raise every deployment's floor behind the runtime's back, and make one of them fail to apply.
    with engine.begin() as conn:
        conn.execute(model_deployments.update().values(min_replicas=1))
    rt.fail = {"alpha"}
    out = ms.autoscale()
    assert rt.scaled["beta"] == 1 and rt.scaled["gamma"] == 1        # the others were scaled
    assert out["beta"] == 1 and out["gamma"] == 1 and rt.scaled["alpha"] == 0


def test_a_call_racing_a_delete_leaves_no_servers_behind(engine):
    """A call that finds the deployment at zero starts a replica. If the deployment is deleted meanwhile (here: while
    the call looks for its servers), the replica it started must not stay running with no deployment to scale it."""
    import asyncio

    class Racing(Runtime):
        def endpoints(self, d):
            if not getattr(self, "raced", False):
                self.raced = True
                ms.undeploy(OWNER, d["name"])                       # someone deletes it now
            return []

    rt = Racing()
    ms = ModelServing(engine, rt, Gateway())
    ms.cold_s = 0
    ms.deploy(OWNER, {"name": "churn", "model": "churn", "version": "1"})
    with pytest.raises(ServingError) as e:
        asyncio.run(ms.invoke(OWNER, "churn", b"{}", "application/json"))
    assert e.value.status == 404
    assert "churn" not in rt.scaled                                   # nothing left running
