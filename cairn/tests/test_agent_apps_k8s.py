"""Agent apps on a real Kubernetes cluster: Cairn deploys each app as its own pod with the chart's own Role, the pod
unpacks the project from a Secret and calls Cairn back for its model as the person asking, only Cairn reaches it, a
redeploy replaces it, a project needing a package the image lacks is refused naming it, any Cairn replica serves it,
and deleting removes everything.

Needs a cluster (CAIRN_KIND_KUBECONFIG; CAIRN_KIND_HOST_IP, where pods reach this host; its node's container named
CAIRN_KIND_NODE, cairn-control-plane), helm, the agent runtime image (CAIRN_AGENT_RUNTIME_TEST_IMAGE, built from
docker/agent-runtime.Dockerfile) and CAIRN_APPS_TEST_IMAGE (a python image) loaded into it."""

import base64
import json
import os
import shutil
import subprocess
import threading

import httpx
import pytest
import yaml

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HOST = os.environ.get("CAIRN_KIND_HOST_IP", "")
HELM = os.environ.get("CAIRN_HELM", "helm")
NODE = os.environ.get("CAIRN_KIND_NODE", "cairn-control-plane")
RUNTIME_IMAGE = os.environ.get("CAIRN_AGENT_RUNTIME_TEST_IMAGE", "")
PROBE_IMAGE = os.environ.get("CAIRN_APPS_TEST_IMAGE", "python:3.12-slim")
pytestmark = pytest.mark.skipif(not (KUBECONFIG and HOST and RUNTIME_IMAGE and shutil.which(HELM)),
                                reason="needs a cluster (CAIRN_KIND_KUBECONFIG, CAIRN_KIND_HOST_IP), helm and the "
                                       "agent runtime image (CAIRN_AGENT_RUNTIME_TEST_IMAGE)")
pytest.importorskip("anthropic")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NS = "cairn-agents"
ANA, BOB, CAL, ADMIN = ("urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:cal", "urn:li:corpuser:admin")


def kubectl(*args, check=True, stdin=None):
    r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, input=stdin)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def sub(who):
    return {"attr": "subject.urn", "op": "eq", "value": who}


@pytest.fixture
def cluster(tmp_path):
    """The agents' namespace with the chart's Role bound to Cairn's account; a token and CA for that account."""
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true")
    for ns in (NS, "cairn"):
        kubectl("create", "namespace", ns, check=False)
    kubectl("-n", "cairn", "create", "serviceaccount", "t-cairn", check=False)
    out = subprocess.run([HELM, "template", "t", "helm/cairn", "--namespace", "cairn", "-s",
                          "templates/agent-apps-kubernetes.yaml", "--set", "cairn.agentApps.runtime=kubernetes",
                          "--set", f"cairn.agentApps.kubernetes.namespace={NS}"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    kubectl("apply", "-f", "-", stdin=out.stdout)
    token = tmp_path / "token"
    token.write_text(kubectl("-n", "cairn", "create", "token", "t-cairn", "--duration", "1h"))
    cfg = yaml.safe_load(open(KUBECONFIG))["clusters"][0]["cluster"]
    ca = tmp_path / "ca.crt"
    ca.write_bytes(base64.b64decode(cfg["certificate-authority-data"]))
    node_ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                              NODE], capture_output=True, text=True, check=True).stdout.strip()
    yield {"api": cfg["server"], "token": str(token), "ca": str(ca), "node": node_ip}
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=false")


@pytest.fixture
def stack(cluster, settings, graph, monkeypatch, tmp_path):
    import http.server

    import uvicorn

    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle, Target
    from cairn.services import build_services
    from test_ai_gateway import SONNET, Provider
    from test_trino_e2e import port, wait

    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    p = port()
    env = {"CAIRN_AI_GATEWAY_ANTHROPIC_URL": f"http://127.0.0.1:{up.server_address[1]}",
           "CAIRN_AI_GATEWAY_ANTHROPIC_KEY": "sk-ant-upstream", "CAIRN_AI_GATEWAY_MODELS": SONNET,
           "CAIRN_AGENT_APPS_RUNTIME": "kubernetes", "CAIRN_AGENT_APPS_CAIRN_URL": f"http://{HOST}:{p}",
           "CAIRN_AGENT_APPS_K8S_NAMESPACE": NS, "CAIRN_AGENT_APPS_K8S_IMAGE": RUNTIME_IMAGE,
           "CAIRN_AGENT_APPS_K8S_API": cluster["api"], "CAIRN_AGENT_APPS_K8S_TOKEN_FILE": cluster["token"],
           "CAIRN_AGENT_APPS_K8S_CA_FILE": cluster["ca"], "CAIRN_AGENT_APPS_K8S_ACCESS": "node-port",
           "CAIRN_AGENT_APPS_K8S_NODE_ADDRESS": cluster["node"],
           # Cairn reaches the node from this host, at the cluster network's gateway.
           "CAIRN_AGENT_APPS_K8S_INGRESS_FROM": json.dumps([{"ipBlock": {"cidr": f"{HOST}/32"}}])}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition=sub(ADMIN)),
        Policy(id="builders", effect="allow", actions=["BUILD_AGENTS"], condition=sub(ANA)),
        Policy(id="models", effect="allow", actions=["USE_AI_GATEWAY"],
               target=Target(resource_urns=[f"urn:li:aiModel:{SONNET}"]),
               condition={"attr": "subject.urn", "op": "in", "value": [ANA, BOB]}),
        Policy(id="bob-uses-triage", effect="allow", actions=["USE_AGENT"],
               target=Target(resource_urns=["urn:li:cairnAgent:triage"]), condition=sub(BOB))]))
    Provider.seen.clear()
    # On all addresses: the agents' pods call Cairn back at HOST.
    server = uvicorn.Server(uvicorn.Config(create_api_app(s), host="0.0.0.0", port=p, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    wait(p)
    yield s, f"http://127.0.0.1:{p}", tmp_path
    server.should_exit = True
    up.shutdown()


def usage(s, actor):
    from sqlalchemy import select

    from cairn.ai.usage import usage_table

    with s.engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(usage_table).where(usage_table.c.actor == actor))]


def explain():
    return "\n".join([kubectl("-n", NS, "get", "pods", "-o", "wide", check=False),
                      kubectl("-n", NS, "get", "events", "--sort-by=.lastTimestamp", check=False),
                      kubectl("-n", NS, "logs", "-l", "cairn.io/agent-app", "--tail=50", check=False)])


def test_agent_apps_on_kubernetes(stack, cluster):
    from cairn.agentkit.project import ProjectError, deploy, init
    from cairn.ai.agent_apps import AgentAppError
    from cairn.ai.agent_apps_k8s import KubeRuntime

    s, url, tmp = stack
    proj = init(str(tmp / "triage"), template="basic")

    # Deploy: one pod of the agent runtime image, holding no secret in its spec, unprivileged, no Kubernetes token.
    try:
        app = deploy(str(proj), url, {"X-Cairn-Actor": ANA})
    except ProjectError:
        print(explain())
        raise
    assert app["status"] == "running" and app["version"] == 1
    dep = json.loads(kubectl("-n", NS, "get", "deployment", "cairn-agent-triage", "-o", "json"))
    spec = dep["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False and spec["securityContext"]["runAsUser"] == 10001
    assert spec["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    assert spec["containers"][0]["image"] == RUNTIME_IMAGE
    secret = s.agent_apps.runtime.endpoint("triage", 1)[1]
    assert secret not in json.dumps(dep)
    assert kubectl("-n", NS, "get", "networkpolicy", "cairn-agent-triage", "-o", "name")

    # Invoke as Bob: the pod calls Cairn's gateway back with Bob's run token; his usage, his session.
    def ask(who, text, session="s1"):
        return httpx.post(f"{url}/v1/agent-apps/triage/invocations", json={"input": text, "session_id": session},
                          headers={"X-Cairn-Actor": who}, timeout=180)
    first = ask(BOB, "Which tickets are open?")
    assert first.status_code == 200 and first.json()["output"] == "Hello from the model", (first.text, explain())
    assert ask(BOB, "And the oldest?").status_code == 200
    from test_ai_gateway import Provider

    assert [m["role"] for m in Provider.seen[-1]["body"]["messages"]] == ["user", "assistant", "user"]
    assert {r["task"] for r in usage(s, BOB)} == {f"gateway:{app['agent_id']}"} and len(usage(s, BOB)) == 2
    assert ask(CAL, "Hi").status_code == 404

    # Only Cairn reaches the pod, and only with the runtime secret.
    ep, _ = s.agent_apps.runtime.endpoint("triage", 1)
    assert httpx.post(f"{ep}/invocations", json={"messages": [{"role": "user", "content": "x"}]},
                      headers={"X-Cairn-Run-Token": "cat_forged"}).status_code == 404
    pod_ip = kubectl("-n", NS, "get", "pods", "-l", "cairn.io/agent-app=triage", "-o",
                     "jsonpath={.items[0].status.podIP}")
    probe = kubectl("run", "intruder", "--rm", "-i", "--restart=Never", f"--image={PROBE_IMAGE}",
                    "--image-pull-policy=IfNotPresent", "--command", "--", "python", "-c",
                    f"import urllib.request\ntry:\n urllib.request.urlopen('http://{pod_ip}:8080/healthz', timeout=5)\n"
                    " print('REACHED')\nexcept Exception as e:\n print('REFUSED', type(e).__name__)", check=False)
    assert "REFUSED" in probe and "REACHED" not in probe, probe

    # Another Cairn replica (its own runtime object) finds the app's address and secret in the cluster.
    other = KubeRuntime.from_env(f"http://{HOST}:0")
    got = other.endpoint("triage", 1)
    assert got is not None and got[1] == secret

    # Redeploy: a new version replaces the pod (never two versions answering).
    (proj / "agent.py").write_text((proj / "agent.py").read_text().replace("Answer briefly", "Answer in one line"))
    assert deploy(str(proj), url, {"X-Cairn-Actor": ANA})["version"] == 2
    assert ask(BOB, "Again?").status_code == 200
    assert other.endpoint("triage", 1) is None and other.endpoint("triage", 2) is not None
    pods = kubectl("-n", NS, "get", "pods", "-l", "cairn.io/agent-app=triage", "-o",
                   "jsonpath={.items[*].metadata.annotations.cairn\\.io/agent-app-version}")
    assert pods.split() == ["2"], pods

    # A project needing a package the image lacks is refused, naming it, and leaves nothing behind.
    bad = init(str(tmp / "needy"), template="basic")
    (bad / "requirements.txt").write_text("anthropic\nno-such-package-cairn-test\n")
    with pytest.raises(ProjectError, match="no-such-package-cairn-test"):
        deploy(str(bad), url, {"X-Cairn-Actor": ANA})
    assert kubectl("-n", NS, "get", "deployment,service,secret,networkpolicy", "-l", "cairn.io/agent-app=needy",
                   "-o", "name") == ""

    # Cairn's account manages agent apps in their namespace only.
    elsewhere = KubeRuntime("default", RUNTIME_IMAGE, url, cluster["api"], cluster["token"], cluster["ca"],
                            ingress_from=[{"ipBlock": {"cidr": f"{HOST}/32"}}])
    with pytest.raises(AgentAppError, match="403"):
        elsewhere.start("x", 1, b"")

    # Deleting the app removes its objects.
    assert httpx.delete(f"{url}/v1/agent-apps/triage", headers={"X-Cairn-Actor": ANA}).status_code == 204
    assert kubectl("-n", NS, "get", "deployment,service,secret,networkpolicy", "-l", "cairn.io/agent-app=triage",
                   "-o", "name") == ""
    assert ask(BOB, "Hi").status_code == 404
