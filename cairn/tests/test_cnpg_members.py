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


def test_an_instance_runs_only_a_release_of_an_offered_image():
    """The image runs as the instance, next to the backup store's keys (Barman Cloud in the pod): a caller may move
    to another release of the image the operator offers for that major version, and nothing else."""
    import pytest

    from cairn.databases.docker_runtime import RuntimeError_

    rt = CnpgRuntime("ns", api="https://k8s.invalid")
    assert rt.image_for("16") == "ghcr.io/cloudnative-pg/postgresql:16.6"
    assert rt.image_for("16", "ghcr.io/cloudnative-pg/postgresql:16.8") == "ghcr.io/cloudnative-pg/postgresql:16.8"
    assert rt.image_for("17", "ghcr.io/cloudnative-pg/postgresql:17.4-bookworm").endswith(":17.4-bookworm")
    digest = "ghcr.io/cloudnative-pg/postgresql@sha256:" + "a" * 64
    assert rt.image_for("16", digest) == digest
    for bad in ("evil.example/postgresql:16.6", "ghcr.io/cloudnative-pg/postgresql-evil:16.6",
                "ghcr.io/cloudnative-pg/postgresql:17.2", "ghcr.io/cloudnative-pg/postgresql:160",
                "ghcr.io/cloudnative-pg/postgresql", "docker.io/library/busybox:16"):
        with pytest.raises(RuntimeError_) as e:
            rt.image_for("16", bad)
        assert e.value.status == 400, bad
    inst = {"name": "a", "spec": {"version": "16", "cpu": 1, "memory_mb": 512, "storage_gb": 1},
            "image": "evil.example/postgresql:16.6", "backup_prefix": "a/u"}
    with pytest.raises(RuntimeError_):
        rt.cluster(inst)
