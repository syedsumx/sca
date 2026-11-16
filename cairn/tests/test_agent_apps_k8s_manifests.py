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


def test_the_egress_proxy_credential_is_in_the_secret_not_the_deployment(tmp_path):
    """With egress enforced the pod reaches the internet through the egress proxy with a 30-day credential. It belongs
    in the app's Secret: a Deployment's spec is readable by whoever may read Deployments, and is kept in its
    ReplicaSets' history."""
    import base64
    import json

    from cairn.common.db import get_engine, init_db
    from cairn.network import proxy as P
    from cairn.network.egress import Egress

    engine = get_engine(f"sqlite:///{tmp_path}/e.db")
    init_db(engine)
    rt = runtime()
    rt.egress = Egress(engine, enforce=True, proxy="http://cairn-egress-proxy:3128", key=b"k" * 32)
    objs = objects(rt)
    deployment = json.dumps(objs["Deployment"])
    assert "egress:" not in deployment and "@cairn-egress-proxy" not in deployment
    container = objs["Deployment"]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in container["env"]}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        ref = env[name]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "cairn-agent-triage" and "value" not in env[name]
        url = base64.b64decode(objs["Secret"]["data"][ref["key"]]).decode()
        assert P.check(b"k" * 32, url.split("egress:", 1)[1].rsplit("@", 1)[0])["r"] == "agent-app-triage-v3"
    # The project's volume shows the bundle and the runtime secret only, not the proxy credential.
    volume = next(v for v in objs["Deployment"]["spec"]["template"]["spec"]["volumes"] if v["name"] == "project")
    assert sorted(i["key"] for i in volume["secret"]["items"]) == ["bundle.tgz", "secret"]
