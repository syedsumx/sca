"""The objects Cairn applies for an agent app on Kubernetes (``cairn.ai.agent_apps_k8s``), checked without a cluster:
what the agent's pod may use, and that no credential is written into the Deployment. (The end-to-end behaviour on a
real cluster is ``tests/test_agent_apps_k8s.py``.)"""

from cairn.ai.agent_apps_k8s import KubeRuntime

PEERS = [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "api"}}}]


def runtime(**kw) -> KubeRuntime:
    return KubeRuntime("cairn-agents", "cairn/agent-runtime:test", "http://cairn-api.cairn.svc:9000",
                       api="https://k8s.invalid", ingress_from=PEERS, **kw)


def objects(rt: KubeRuntime) -> dict:
    return {o["kind"]: o for o in rt.manifests("triage", 3, b"bundle", "runtime-secret")}


def test_the_pods_scratch_space_is_bounded():
    """The pod's only writable place is /tmp, an emptyDir on the node's disk: without a size limit agent code fills
    the node's disk (and the other tenants' pods on it are evicted, not this one)."""
    spec = objects(runtime())["Deployment"]["spec"]["template"]["spec"]
    tmp = next(v for v in spec["volumes"] if v["name"] == "tmp")
    assert tmp["emptyDir"].get("sizeLimit") == "1Gi"
    assert objects(runtime(tmp_size="256Mi"))["Deployment"]["spec"]["template"]["spec"]["volumes"][1]["emptyDir"] == {
        "sizeLimit": "256Mi"}
