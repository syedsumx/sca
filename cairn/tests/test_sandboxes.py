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
    forged = "Basic " + base64.b64encode(f"run:{cred[:-2]}00".encode()).decode()
    assert creds.check(forged) is None and creds.check("Bearer x") is None
    assert ProxyCredentials(b"j" * 32).check(header) is None
    expired = creds.issue([], False, -5)
    assert creds.check("Basic " + base64.b64encode(f"run:{expired}".encode()).decode()) is None
    with pytest.raises(SandboxError):
        ProxyCredentials(b"short")


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


def test_output_parsing():
    assert split_output("log\nMARK{\"status\": \"ok\"}\n", "MARK") == ("log\n", {"status": "ok"})
    assert split_output("no marker", "MARK") == ("no marker", None)
