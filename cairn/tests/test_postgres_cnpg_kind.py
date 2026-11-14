"""Managed Postgres (L140) on a real Kubernetes cluster (kind) through CloudNativePG (the pinned release): Cairn makes
an instance with a standby using only the chart's Role, backs it up to a Cairn Store running in the cluster, survives
the loss of its primary (the operator fails over and the service keeps answering), and restores it into a new
instance with the data and none of the logins.

Needs a kind cluster (CAIRN_KIND_KUBECONFIG; CAIRN_KIND_HOST_IP; the node named cairn-control-plane), helm, the
store image (CAIRN_PG_TEST_STORE_IMAGE, e.g. cairn/store:ci) loaded into the cluster, and internet access for the
operator's manifest and PostgreSQL image (CI job spark-kind)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import urllib.request

import pytest
import yaml
from fastapi.testclient import TestClient

KUBECONFIG = os.environ.get("CAIRN_KIND_KUBECONFIG", "")
HOST = os.environ.get("CAIRN_KIND_HOST_IP", "")
HELM = os.environ.get("CAIRN_HELM", "helm")
NODE = os.environ.get("CAIRN_KIND_NODE", "cairn-control-plane")
STORE_IMAGE = os.environ.get("CAIRN_PG_TEST_STORE_IMAGE", "")
pytestmark = pytest.mark.skipif(not (KUBECONFIG and HOST and STORE_IMAGE and shutil.which(HELM)),
                                reason="needs a kind cluster (CAIRN_KIND_KUBECONFIG, CAIRN_KIND_HOST_IP), helm and "
                                       "the store image (CAIRN_PG_TEST_STORE_IMAGE)")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANA = "urn:li:corpuser:ana"
NS = "cairn-postgres"
KEY = ("cairn-pg-kind", "pg-kind-secret-" + "k" * 24)


def kubectl(*args, check=True, stdin=None):
    r = subprocess.run(["kubectl", "--kubeconfig", KUBECONFIG, *args], capture_output=True, text=True, input=stdin)
    if check and r.returncode != 0:
        raise RuntimeError(f"kubectl {' '.join(args)}: {r.stderr}")
    return r.stdout.strip()


def wait_for(what, fn, timeout=600, every=5):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # noqa: BLE001 - not yet
            last = e
        time.sleep(every)
    raise AssertionError(f"{what}: not within {timeout} s ({last})")


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    from cairn.databases.cnpg import CNPG_MANIFEST

    tmp = tmp_path_factory.mktemp("cnpg")
    # The operator, at its pinned release.
    manifest = tmp / "cnpg.yaml"
    for attempt in range(6):
        try:
            manifest.write_bytes(urllib.request.urlopen(CNPG_MANIFEST, timeout=60).read())
            break
        except OSError:
            time.sleep(10 * (attempt + 1))
    kubectl("apply", "--server-side", "-f", str(manifest))
    kubectl("-n", "cnpg-system", "rollout", "status", "deployment/cnpg-controller-manager", "--timeout=300s")
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=true")
    # Earlier tests in the job delete "cairn" without waiting: nothing can be made in a namespace being deleted.
    end = time.time() + 300
    while kubectl("get", "namespace", "cairn", "-o", "jsonpath={.status.phase}", check=False) == "Terminating":
        assert time.time() < end, "namespace cairn is still being deleted"
        time.sleep(3)
    for ns in (NS, "cairn"):
        kubectl("create", "namespace", ns, check=False)
    kubectl("-n", "cairn", "create", "serviceaccount", "t-cairn", check=False)
    kubectl("-n", "cairn", "get", "serviceaccount", "t-cairn")          # it is there (made now, or before)
    out = subprocess.run([HELM, "template", "t", "helm/cairn", "--namespace", "cairn", "-s", "templates/pg-kubernetes.yaml",
                          "--set", "cairn.databases.instances.runtime=kubernetes",
                          "--set", f"cairn.databases.instances.kubernetes.namespace={NS}"],
                         cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    kubectl("apply", "-f", "-", stdin=out.stdout)
    # The store's keys, as files (never on a command line): the operator's copy in the namespace.
    (tmp / "access-key").write_text(KEY[0])
    (tmp / "secret-key").write_text(KEY[1])
    kubectl("-n", NS, "create", "secret", "generic", "cairn-pg-backups", f"--from-file=access-key={tmp / 'access-key'}",
            f"--from-file=secret-key={tmp / 'secret-key'}")
    # Cairn Store in the cluster: the instances back up to it at its Service; Cairn reaches it at a node port.
    kubectl("apply", "-f", "-", stdin=yaml.safe_dump_all([
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "pg-store", "namespace": NS},
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "pg-store"}}, "template": {
             "metadata": {"labels": {"app": "pg-store"}},
             "spec": {"containers": [{"name": "store", "image": STORE_IMAGE, "imagePullPolicy": "IfNotPresent",
                                      "ports": [{"containerPort": 8333}],
                                      "env": [{"name": "CAIRN_STORE_ACCESS_KEY", "valueFrom": {"secretKeyRef": {
                                          "name": "cairn-pg-backups", "key": "access-key"}}},
                                              {"name": "CAIRN_STORE_SECRET_KEY", "valueFrom": {"secretKeyRef": {
                                                  "name": "cairn-pg-backups", "key": "secret-key"}}},
                                              {"name": "CAIRN_STORE_FSYNC", "value": "false"},
                                              {"name": "CAIRN_STORE_META", "value": ""}],
                                      "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                      "volumes": [{"name": "data", "emptyDir": {}}]}}}},
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "pg-store", "namespace": NS},
         "spec": {"type": "NodePort", "selector": {"app": "pg-store"}, "ports": [{"port": 8333, "targetPort": 8333}]}}]))
    kubectl("-n", NS, "rollout", "status", "deployment/pg-store", "--timeout=180s")
    store_port = kubectl("-n", NS, "get", "service", "pg-store", "-o", "jsonpath={.spec.ports[0].nodePort}")
    token = tmp / "token"
    token.write_text(kubectl("-n", "cairn", "create", "token", "t-cairn", "--duration", "2h"))
    cfg = yaml.safe_load(open(KUBECONFIG))["clusters"][0]["cluster"]
    ca = tmp / "ca.crt"
    ca.write_bytes(base64.b64decode(cfg["certificate-authority-data"]))
    node_ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", NODE],
                             capture_output=True, text=True, check=True).stdout.strip()
    yield {"api": cfg["server"], "token": str(token), "ca": str(ca), "node": node_ip, "tmp": tmp,
           "store": f"http://{node_ip}:{store_port}"}
    kubectl("delete", "namespace", NS, "--ignore-not-found", "--wait=false")


def connect(creds, **kw):
    import psycopg

    return psycopg.connect(host=creds["host"], port=creds["port"], dbname=kw.get("dbname", creds["database"]),
                           user=creds["user"], password=creds["password"], sslmode="require", connect_timeout=5,
                           autocommit=True)


def test_an_instance_with_a_standby_fails_over_backs_up_and_restores(cluster, settings, graph, monkeypatch):
    from cairn.app import create_api_app
    from cairn.governance.abac import Policy, PolicyBundle
    from cairn.services import build_services

    tmp = cluster["tmp"]
    (tmp / "pg-key").write_text(base64.b64encode(os.urandom(32)).decode())
    env = {"CAIRN_PG_RUNTIME": "kubernetes", "CAIRN_PG_K8S_NAMESPACE": NS, "CAIRN_PG_K8S_API": cluster["api"],
           "CAIRN_PG_K8S_TOKEN_FILE": cluster["token"], "CAIRN_PG_K8S_CA_FILE": cluster["ca"],
           "CAIRN_PG_K8S_ACCESS": "node-port", "CAIRN_PG_K8S_NODE_ADDRESS": cluster["node"],
           "CAIRN_PG_K8S_BACKUP_SECRET": "cairn-pg-backups", "CAIRN_PG_INSTANCE_KEY_FILE": str(tmp / "pg-key"),
           "CAIRN_PG_BACKUP_STORE": json.dumps({"endpoint": cluster["store"], "bucket": "pg-backups",
                                                "endpoint_for_instances": f"http://pg-store.{NS}.svc:8333"}),
           "CAIRN_PG_BACKUP_ACCESS_KEY_FILE": str(tmp / "access-key"),
           "CAIRN_PG_BACKUP_SECRET_KEY_FILE": str(tmp / "secret-key"),
           "NO_PROXY": f"{cluster['node']},{os.environ.get('NO_PROXY', '')}"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    assert s.pg_instances is not None and s.pg_instances.runtime.name == "kubernetes"
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="ana", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ANA})]))
    ana = TestClient(create_api_app(s), headers={"X-Cairn-Actor": ANA})

    def ready(name):
        def check():
            st = ana.get(f"/v1/databases/instances/{name}/status").json()
            return st if st.get("state") == "ready" and st.get("health") in ("healthy", "degraded") else None
        try:
            return wait_for(f"{name} ready", check, timeout=900)
        except AssertionError:
            print(kubectl("-n", NS, "get", "clusters,pods,backups", "-o", "wide", check=False),
                  kubectl("-n", NS, "get", "events", "--sort-by=.lastTimestamp", check=False)[-4000:], sep="\n")
            raise

    r = ana.post("/v1/databases/instances", json={"name": "ha", "replicas": 1, "cpu": 0.5, "memory_mb": 512,
                                                  "storage_gb": 1, "pooler": {"instances": 1}})
    assert r.status_code == 201, r.text
    assert kubectl("-n", NS, "get", "cluster", "cairn-pg-ha", "-o", "jsonpath={.spec.instances}") == "2"
    assert kubectl("-n", NS, "get", "pooler", "cairn-pg-ha-pooler", "-o", "name")
    st = ready("ha")
    assert len(st["runtime_state"]["instances"]) == 2

    # Data through a login of Cairn's; a backup to the store in the cluster.
    r = ana.post("/v1/databases", json={"name": "app_db", "instance": "ha"})
    assert r.status_code == 201, r.text
    w = ana.post("/v1/databases/app_db/credentials", json={"access": "write", "ttl_minutes": 60}).json()
    with connect(w) as c:
        c.execute("CREATE TABLE t (id int PRIMARY KEY)")
        c.execute("INSERT INTO t SELECT generate_series(1, 100)")
    r = ana.post("/v1/databases/instances/ha/backups")
    assert r.status_code == 201 and r.json()["state"] == "completed", r.text
    assert s.pg_instances.store.list("ha/"), "nothing in the store"           # base backup and WAL (Barman Cloud)

    # The primary is lost: the operator promotes the standby, and the service keeps answering. "Ready" allows a
    # degraded instance, so first wait for a standby that is ready too (else there is nothing to promote).
    def runtime():
        return ana.get("/v1/databases/instances/ha/status").json().get("runtime_state") or {}

    def both_ready():
        rs = runtime()
        ok = rs.get("ready_instances") == 2 and len(rs.get("instances") or []) == 2 \
            and all(m["ready"] for m in rs["instances"])
        return rs if ok else None
    try:
        first = wait_for("a ready standby", both_ready, timeout=600)["primary"]
    except AssertionError:
        print(kubectl("-n", NS, "get", "clusters,pods", "-o", "wide", check=False))
        raise
    kubectl("-n", NS, "delete", "pod", first, "--wait=false")
    started = time.time()
    now = wait_for("a new primary", lambda: (lambda p: p if p and p != first else None)(runtime().get("primary")),
                   timeout=300)

    def writes():
        with connect(w) as c:
            c.execute("INSERT INTO t VALUES (1000 + (extract(epoch FROM now())::int % 100000)) ON CONFLICT DO NOTHING")
            return c.execute("SELECT count(*) FROM t WHERE id <= 100").fetchone()[0] == 100
    wait_for("writes after the primary was lost", writes, timeout=300, every=2)
    print(f"\nwrites again on {now} {time.time() - started:.0f} s after {first} was deleted")
    ready("ha")                                                    # the old primary comes back as a standby

    # Metrics for dashboards and the SRE agent, with the operator's exporter.
    m = ana.get("/v1/databases/instances/ha/metrics")
    assert m.status_code == 200, m.text
    assert any(d["database"] == "app_db" for d in m.json()["databases"])

    # Restored into a new instance: the data, the database renamed, no logins of the source.
    r = ana.post("/v1/databases/instances/ha/restore", json={"name": "ha-copy", "replicas": 0,
                                                             "databases": {"app_db": "app_copy"}})
    assert r.status_code == 201, r.text
    ready("ha-copy")
    rw = ana.post("/v1/databases/app_copy/credentials", json={"access": "read", "ttl_minutes": 30}).json()
    with connect(rw) as c:
        assert c.execute("SELECT count(*) FROM t WHERE id <= 100").fetchone()[0] == 100
    import psycopg

    with pytest.raises(psycopg.OperationalError):
        connect({**rw, "user": w["user"], "password": w["password"], "database": "app_copy"})

    # Cairn's account manages instances in their namespace only.
    from cairn.databases.cnpg import CnpgRuntime
    from cairn.databases.docker_runtime import RuntimeError_

    other = CnpgRuntime("default", api=cluster["api"], token_file=cluster["token"], ca_file=cluster["ca"])
    with pytest.raises(RuntimeError_, match="403"):
        other.state({"name": "ha", "spec": {}})

    for db in ("app_copy", "app_db"):
        assert ana.delete(f"/v1/databases/{db}").status_code == 204
    for name in ("ha-copy", "ha"):
        assert ana.delete(f"/v1/databases/instances/{name}").status_code == 204
    wait_for("clusters gone", lambda: kubectl("-n", NS, "get", "clusters", "-o", "name") == "", timeout=180)
