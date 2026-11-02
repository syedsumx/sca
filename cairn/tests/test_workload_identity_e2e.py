"""Workload identity against a real Kubernetes API server: service-account tokens the cluster issues
(TokenRequest, the API projected tokens come from) are exchanged at Cairn for a pipeline user's token.

Needs CAIRN_K8S_BIN_DIR with kube-apiserver, etcd and kubectl (CI downloads them)."""

import json
import os
import shutil
import subprocess
import tempfile
import time

import httpx
import pytest

from test_trino_e2e import cairn, port, wait  # noqa: F401  (fixtures)

BIN = os.environ.get("CAIRN_K8S_BIN_DIR", "")
pytestmark = pytest.mark.skipif(not (BIN and os.path.exists(os.path.join(BIN, "kube-apiserver"))),
                                reason="needs CAIRN_K8S_BIN_DIR with kube-apiserver, etcd and kubectl")
SA = "system:serviceaccount:pipelines:svc-dbt"


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def k8s():
    d = tempfile.mkdtemp()
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt", "-days", "1",
             "-subj", "/CN=test-ca", "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=d)
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "api.key", "-out", "api.csr", "-subj", "/CN=kube-apiserver", cwd=d)
    with open(f"{d}/ext.cnf", "w") as f:
        f.write("subjectAltName=IP:127.0.0.1,DNS:localhost\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                "extendedKeyUsage=serverAuth\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n")
    _openssl("x509", "-req", "-in", "api.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "api.crt",
             "-days", "1", "-extfile", "ext.cnf", cwd=d)
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "admin.key", "-out", "admin.csr",
             "-subj", "/O=system:masters/CN=admin", cwd=d)
    _openssl("x509", "-req", "-in", "admin.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial", "-out", "admin.crt",
             "-days", "1", cwd=d)
    _openssl("genrsa", "-out", "sa.key", "2048", cwd=d)
    _openssl("rsa", "-in", "sa.key", "-pubout", "-out", "sa.pub", cwd=d)
    ep, cp, ap = port(), port(), port()
    etcd = subprocess.Popen([f"{BIN}/etcd", "--data-dir", f"{d}/etcd", "--listen-client-urls", f"http://127.0.0.1:{ep}",
                             "--advertise-client-urls", f"http://127.0.0.1:{ep}", "--listen-peer-urls", f"http://127.0.0.1:{cp}"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    issuer = f"https://127.0.0.1:{ap}"
    api = subprocess.Popen([f"{BIN}/kube-apiserver", f"--etcd-servers=http://127.0.0.1:{ep}", f"--secure-port={ap}",
                            "--bind-address=127.0.0.1", f"--tls-cert-file={d}/api.crt", f"--tls-private-key-file={d}/api.key",
                            f"--client-ca-file={d}/ca.crt", f"--service-account-issuer={issuer}",
                            f"--service-account-key-file={d}/sa.pub", f"--service-account-signing-key-file={d}/sa.key",
                            "--authorization-mode=RBAC", "--service-cluster-ip-range=10.96.0.0/16", f"--cert-dir={d}/certs"],
                           stdout=open(f"{d}/api.log", "w"), stderr=subprocess.STDOUT)
    with open(f"{d}/kubeconfig", "w") as f:
        f.write(f"""apiVersion: v1
kind: Config
clusters: [{{name: t, cluster: {{server: "{issuer}", certificate-authority: "{d}/ca.crt"}}}}]
users: [{{name: admin, user: {{client-certificate: "{d}/admin.crt", client-key: "{d}/admin.key"}}}}]
contexts: [{{name: t, context: {{cluster: t, user: admin}}}}]
current-context: t
""")

    def kubectl(*args):
        r = subprocess.run([f"{BIN}/kubectl", "--kubeconfig", f"{d}/kubeconfig", *args], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()
    for _ in range(90):
        if subprocess.run([f"{BIN}/kubectl", "--kubeconfig", f"{d}/kubeconfig", "get", "--raw", "/readyz"],
                          capture_output=True).returncode == 0:
            break
        time.sleep(1)
    else:
        pytest.fail(open(f"{d}/api.log").read()[-3000:])
    for _ in range(30):  # RBAC's default roles are created shortly after start
        if subprocess.run([f"{BIN}/kubectl", "--kubeconfig", f"{d}/kubeconfig", "get", "clusterrole",
                           "system:service-account-issuer-discovery"], capture_output=True).returncode == 0:
            break
        time.sleep(1)
    # The cluster's signing keys are public (OIDC discovery), as on managed clusters.
    kubectl("create", "clusterrolebinding", "oidc-discovery", "--clusterrole=system:service-account-issuer-discovery",
            "--group=system:unauthenticated")
    kubectl("create", "namespace", "pipelines")
    kubectl("create", "namespace", "other")
    for ns, sa in (("pipelines", "svc-dbt"), ("pipelines", "svc-unbound"), ("other", "svc-dbt")):
        kubectl("-n", ns, "create", "serviceaccount", sa)
    os.environ["CAIRN_WORKLOAD_ISSUERS"] = json.dumps([{
        "name": "k8s", "issuer": issuer, "audience": "cairn", "jwks_url": f"{issuer}/openid/v1/jwks",
        "ca_file": f"{d}/ca.crt", "max_token_lifetime": 86400}])
    yield {"kubectl": kubectl, "issuer": issuer, "dir": d}
    os.environ.pop("CAIRN_WORKLOAD_ISSUERS", None)
    for p in (api, etcd):
        p.terminate()
        p.wait(20)
    shutil.rmtree(d, ignore_errors=True)


def exchange(url, subject):
    return httpx.post(f"{url}/v1/auth/workload-token", data={
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange", "subject_token": subject,
        "subject_token_type": "urn:ietf:params:oauth:token-type:jwt"}, timeout=20)


def test_a_service_account_signs_in_as_its_pipeline_user(k8s, cairn, tmp_path):  # noqa: F811
    url, s = cairn
    assert s.workload is not None
    admin = {"X-Cairn-Actor": "urn:li:corpuser:admin"}
    # Only administrators bind workloads to users.
    r = httpx.post(f"{url}/v1/workload-identity/bindings", json={"issuer": "k8s", "subject": SA, "user": "svc-dbt"},
                   headers={"X-Cairn-Actor": "urn:li:corpuser:ana"}, timeout=10)
    assert r.status_code == 403
    r = httpx.post(f"{url}/v1/workload-identity/bindings", headers=admin, timeout=10, json={
        "issuer": "k8s", "subject": SA, "user": "svc-dbt", "conditions": {"kubernetes.io.namespace": "pipelines"}})
    assert r.status_code == 201, r.text
    binding = r.json()["id"]
    assert [b["subject"] for b in httpx.get(f"{url}/v1/workload-identity/bindings", headers=admin, timeout=10).json()] == [SA]

    # A token the cluster issued for the service account, for audience cairn (what a projected volume holds).
    sa_token = k8s["kubectl"]("-n", "pipelines", "create", "token", "svc-dbt", "--audience", "cairn", "--duration", "10m")
    r = exchange(url, sa_token)
    assert r.status_code == 200, r.text
    cairn_token = r.json()["access_token"]
    assert r.json()["user"] == "svc-dbt" and r.json()["expires_in"] == 3600
    me = httpx.get(f"{url}/me", headers={"Authorization": f"Bearer {cairn_token}"}, timeout=10).json()
    assert me == {"urn": "urn:li:corpuser:svc-dbt", "via": "workload"}
    # It works where a personal access token does: a Trino token, a lakehouse session.
    for path in ("/v1/trino/token", "/v1/lakehouse/token"):
        r = httpx.post(url + path, headers={"Authorization": f"Bearer {cairn_token}"}, timeout=10)
        assert r.status_code == 200 and r.json()["user"] == "svc-dbt", (path, r.text)
    ev = [e for e in s.audit.entries("identity", limit=50) if e["action"] == "workload.token_issued"]
    assert ev and ev[0]["details"]["subject"] == SA and ev[0]["details"]["binding"] == binding

    # The clients exchange the file a projected volume holds (dbt runner; Spark).
    f = tmp_path / "token"
    f.write_text(sa_token)
    from cairn.pipelines.dbt_runner import exchange_workload_token
    assert exchange_workload_token(url, str(f), True).count(".") == 2
    import importlib.util
    spec = importlib.util.spec_from_file_location("cairn_spark", os.path.join(os.path.dirname(__file__), "..", "spark", "src",
                                                                              "cairn_spark", "__init__.py"))
    cairn_spark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cairn_spark)
    os.environ["CAIRN_WORKLOAD_TOKEN_FILE"] = str(f)
    try:
        assert cairn_spark._token(None, url).count(".") == 2
    finally:
        os.environ.pop("CAIRN_WORKLOAD_TOKEN_FILE")

    # Refused: the cluster's default audience, an unbound service account, the same name in another
    # namespace, a token living longer than allowed is fine here (24 h) but not one Cairn cannot verify.
    refused = {
        "default audience": k8s["kubectl"]("-n", "pipelines", "create", "token", "svc-dbt", "--duration", "10m"),
        "unbound": k8s["kubectl"]("-n", "pipelines", "create", "token", "svc-unbound", "--audience", "cairn"),
        "other namespace": k8s["kubectl"]("-n", "other", "create", "token", "svc-dbt", "--audience", "cairn"),
        "forged": sa_token[:-8] + "AAAAAAAA",
    }
    for why, t in refused.items():
        r = exchange(url, t)
        assert r.status_code in (400, 403) and r.json()["error"] in ("invalid_grant", "access_denied"), (why, r.text)
    # A long-lived token (48 h) is refused when the issuer allows at most a day.
    long = k8s["kubectl"]("-n", "pipelines", "create", "token", "svc-dbt", "--audience", "cairn", "--duration", "48h")
    r = exchange(url, long)
    assert r.status_code == 400 and "longer" in r.json()["error_description"], r.text
    # A suspended pipeline user, and an unbound workload, get nothing; the Cairn token already issued is
    # a short-lived one.
    status = s.workload.user_status
    s.workload.user_status = lambda urn: "SUSPENDED"
    try:
        assert exchange(url, sa_token).status_code == 403
    finally:
        s.workload.user_status = status
    assert httpx.delete(f"{url}/v1/workload-identity/bindings/{binding}", headers=admin, timeout=10).status_code == 204
    assert exchange(url, sa_token).status_code == 403
    actions = [e["action"] for e in s.audit.entries("identity", limit=50)]
    assert "workload.binding_created" in actions and "workload.binding_deleted" in actions
    # A Cairn workload token that is forged or not Cairn's is refused as a credential.
    r = httpx.get(f"{url}/me", headers={"Authorization": f"Bearer {cairn_token[:-6]}AAAAAA"}, timeout=10)
    assert r.status_code == 401
