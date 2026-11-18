"""Code sandboxes (L116) without containers: the Kubernetes pod a run becomes (fenced by a NetworkPolicy before it
exists, no service account token, read-only root, limits), the egress proxy's signed credentials and policy, and
request checks. The containers themselves are tested in test_sandboxes_e2e.py."""

import base64
import socket
import threading

import pytest

from cairn.ai.sandboxes import (Policy, ProxyCredentials, KubeSandbox, SandboxError, Spec, handle_proxy_connection,
                                split_output)


def spec(**kw):
    return Spec("r" * 32, "python", "result = 1", {"data/a.csv": b"x,y\n"}, {"CAIRN_TOKEN": "cat_secret"}, 30, 256,
                0.5, {"cairn": True, "allow": []}, 1 << 20)


def test_the_pod_is_fenced_and_locked_down(tmp_path):
    tok = tmp_path / "token"
    tok.write_text("t")
    k = KubeSandbox("sbx", "img:1", api="https://k8s.invalid", token_file=str(tok), runtime_class="gvisor",
                    proxy_url="http://cairn-egress.cairn.svc:3129", proxy_peers=[{"podSelector": {}}],
                    creds=ProxyCredentials(b"k" * 32))
    netpol, program, env, pod = k.manifests(spec(), True, "CRED")
    assert netpol["kind"] == "NetworkPolicy" and netpol["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert netpol["spec"]["ingress"] == [] and netpol["spec"]["egress"][0]["ports"] == [{"port": 3129, "protocol": "TCP"}]
    assert k.manifests(spec(), False)[0]["spec"]["egress"] == []          # no network: nothing at all
    ps = pod["spec"]
    assert ps["automountServiceAccountToken"] is False and ps["runtimeClassName"] == "gvisor"
    assert ps["activeDeadlineSeconds"] == 60 and ps["securityContext"]["runAsNonRoot"]
    c = ps["containers"][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] and c["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert c["resources"]["limits"] == {"cpu": "0.5", "memory": "256Mi"}
    assert "cat_secret" not in str(c) and env["stringData"]["CAIRN_TOKEN"] == "cat_secret"
    assert env["stringData"]["HTTPS_PROXY"] == "http://run:CRED@cairn-egress.cairn.svc:3129"
    items = {i["path"] for i in ps["volumes"][0]["secret"]["items"]}
    assert {"runner.py", "wrap.py", "main.py", "inputs/data/a.csv"} <= items
    assert base64.b64decode(program["data"]["main.py"]).decode() == "result = 1"


def test_proxy_credentials_are_signed_and_expire():
    creds = ProxyCredentials(b"k" * 32)
    cred = creds.issue(["pypi.org"], True, 60)
    header = "Basic " + base64.b64encode(f"run:{cred}".encode()).decode()
    assert creds.check(header)["a"] == ["pypi.org"] and creds.check(header)["c"] is True
    # Change the signature's last hex digit (overwriting it with a fixed value left it unchanged one time in 16-256).
    forged = "Basic " + base64.b64encode(f"run:{cred[:-1]}{'0' if cred[-1] != '0' else '1'}".encode()).decode()
    assert creds.check(forged) is None and creds.check("Bearer x") is None
    assert ProxyCredentials(b"j" * 32).check(header) is None
    expired = creds.issue([], False, -5)
    assert creds.check("Basic " + base64.b64encode(f"run:{expired}".encode()).decode()) is None
    with pytest.raises(SandboxError):
        ProxyCredentials(b"short")


def test_a_runs_egress_cap_counts_all_its_connections_to_the_proxy():
    """On Kubernetes each connection to Cairn's egress proxy carries the run's credential: the run's traffic is
    counted across its connections, so many connections do not each get CAIRN_SANDBOX_MAX_EGRESS_MB."""
    from cairn.ai.sandboxes import TcpProxy

    creds = ProxyCredentials(b"k" * 32)
    proxy = TcpProxy("127.0.0.1:0", creds, "", False, 10)
    try:
        def header(cred):
            return {"proxy-authorization": "Basic " + base64.b64encode(f"run:{cred}".encode()).decode()}
        run, other = creds.issue(["pypi.org"], False, 60), creds.issue(["pypi.org"], False, 60)
        assert proxy._policy(header(run)).count(8)
        assert not proxy._policy(header(run)).count(8)          # a second connection of the same run: over the cap
        assert proxy._policy(header(other)).count(8)            # another run has its own
    finally:
        proxy.close()


def _ask(policy, request: bytes) -> bytes:
    a, b = socket.socketpair()
    t = threading.Thread(target=handle_proxy_connection, args=(b, lambda h: policy))
    t.start()
    a.sendall(request)
    out = b""
    while True:
        d = a.recv(65536)
        if not d:
            break
        out += d
    t.join(10)
    return out


def test_the_proxy_refuses_hosts_not_allowed_and_private_addresses():
    p = Policy(["localhost.example"], "")
    assert b"403" in _ask(p, b"CONNECT evil.example:443 HTTP/1.1\r\nHost: evil.example\r\n\r\n")
    assert b"403" in _ask(p, b"GET http://cairn/data HTTP/1.1\r\nHost: cairn\r\n\r\n")     # Cairn not asked for
    assert b"400" in _ask(p, b"GET /relative HTTP/1.1\r\n\r\n")
    # An allowed name that resolves to a private address is refused (no reaching the cluster through a name).
    p = Policy(["localhost"], "")
    p.allow = {"localhost"}
    assert b"private address" in _ask(p, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
    assert b"407" in _ask(None, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
    assert Policy(["pypi.org"]).allows("pypi.org", 443, 443) and not Policy(["pypi.org"]).allows("pypi.org", 22, 443)


def test_the_tcp_proxy_caps_a_runs_egress_across_its_connections():
    """On Kubernetes each connection to the egress proxy carries the run's credential: the cap is the run's, not one
    connection's, so opening more connections does not lift it."""
    import http.server

    from cairn.ai.sandboxes import TcpProxy

    body = b"x" * (600 << 10)

    class Big(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Big)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    # The address the server listens on: "localhost" can resolve to ::1 first on a runner, where nothing listens.
    target = f"127.0.0.1:{up.server_address[1]}"
    creds = ProxyCredentials(b"k" * 32)
    proxy = TcpProxy("127.0.0.1:0", creds, "", allow_private=True, max_bytes=1 << 20)
    auth = base64.b64encode(f"run:{creds.issue([target], False, 60)}".encode()).decode()
    try:
        got = []
        for _ in range(3):
            c = socket.create_connection(("127.0.0.1", proxy.port), 10)
            c.sendall(f"GET http://{target}/ HTTP/1.1\r\nHost: {target}\r\nProxy-Authorization: Basic {auth}\r\n\r\n"
                      .encode())
            n = 0
            while True:
                d = c.recv(65536)
                if not d:
                    break
                n += len(d)
            c.close()
            got.append(n)
        assert got[0] > len(body) and sum(got) < (1 << 20) + (64 << 10), got
    finally:
        proxy.close()
        up.shutdown()


def test_a_pods_log_is_read_up_to_the_output_cap(tmp_path):
    """The code can write to the container's output directly, so the pod's log is asked for with limitBytes."""
    from cairn.ai.sandboxes import output_cap

    tok = tmp_path / "token"
    tok.write_text("t")
    k = KubeSandbox("sbx", "img:1", api="https://k8s.invalid", token_file=str(tok), poll=0)
    calls = []

    def call(method, path, body=None, ok404=False, raw=False):
        calls.append((method, path))
        return {"status": {"phase": "Succeeded"}} if method == "GET" and not raw else ("" if raw else {})
    k._call = call
    out = k.run(spec(), None)
    [log] = [p for m, p in calls if p.endswith(f"limitBytes={output_cap(1 << 20)}")]
    assert "/log?" in log and out["status"] == "failed"


def test_the_kubernetes_sweep_deletes_what_old_runs_left(tmp_path):
    """Objects of runs older than any run can be (Cairn stopped during them) are deleted; newer ones are left."""
    tok = tmp_path / "token"
    tok.write_text("t")
    k = KubeSandbox("sbx", "img:1", api="https://k8s.invalid", token_file=str(tok))
    calls = []

    def call(method, path, body=None, ok404=False, raw=False):
        calls.append((method, path))
        if method == "GET":
            return {"items": [{"metadata": {"name": "cairn-sbx-old", "creationTimestamp": "2020-01-01T00:00:00Z"}},
                              {"metadata": {"name": "cairn-sbx-new", "creationTimestamp": "2999-01-01T00:00:00Z"}}]}
        return {}
    k._call = call
    assert k.sweep(1200) == 3
    deleted = [p for m, p in calls if m == "DELETE"]
    assert deleted == ["/api/v1/namespaces/sbx/pods/cairn-sbx-old?gracePeriodSeconds=0",
                       "/api/v1/namespaces/sbx/secrets/cairn-sbx-old",
                       "/apis/networking.k8s.io/v1/namespaces/sbx/networkpolicies/cairn-sbx-old"]
    assert all("labelSelector=cairn.io/sandbox-run" in p for m, p in calls if m == "GET")


def test_output_parsing():
    assert split_output("log\nMARK{\"status\": \"ok\"}\n", "MARK") == ("log\n", {"status": "ok"})
    assert split_output("no marker", "MARK") == ("no marker", None)
