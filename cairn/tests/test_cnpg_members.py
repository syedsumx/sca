"""Which pods of a CloudNativePG cluster are its instances: not the pooler's PgBouncer pods, and not the pods of
the operator's bootstrap and join jobs, which all carry the cluster's label too."""

from cairn.databases.cnpg import CnpgRuntime, cluster_name


def pod(name, ready=True, **labels):
    return {"metadata": {"name": name, "labels": {"cnpg.io/cluster": "x", **labels}},
            "status": {"containerStatuses": [{"ready": ready}]}, "spec": {"nodeName": "n1"}}


def test_only_postgres_instances_are_members(monkeypatch):
    rt = object.__new__(CnpgRuntime)
    rt.namespace = "ns"
    name = cluster_name("ha")
    pods = [pod(f"{name}-1", **{"cnpg.io/podRole": "instance"}),
            pod(f"{name}-2", **{"cnpg.io/podRole": "instance"}),
            pod(f"{name}-1-initdb-abcde", ready=False, **{"cnpg.io/jobRole": "initdb"}),
            pod(f"{name}-3-join-fghij", ready=False, **{"cnpg.io/jobRole": "join"}),
            pod(f"{name}-pooler-rw-1", **{"cnpg.io/podRole": "pooler", "cnpg.io/poolerName": f"{name}-pooler-rw"})]

    def call(method, path, params=None, ok404=False, **kw):
        if "/clusters/" in path:
            return {"status": {"currentPrimary": f"{name}-1", "readyInstances": 2, "instances": 2}}
        if path.endswith("/pods"):
            return {"items": pods}
        return {}
    monkeypatch.setattr(rt, "_call", call, raising=False)
    monkeypatch.setattr(rt, "_p", lambda kind, n="": f"/api/{kind}/{n}" if n else f"/api/{kind}", raising=False)
    state = rt.state({"name": "ha"})
    names = [m["name"] for m in state["instances"]]
    assert names == [f"{name}-1", f"{name}-2"], state
    assert [m["role"] for m in state["instances"]] == ["primary", "replica"]
