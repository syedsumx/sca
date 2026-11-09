"""Egress rules for user code (L136): the admin API, the NetworkPolicy a workspace's runs get, and Cairn's egress proxy
on real sockets: an allowed host is reached, everything else is refused and logged, credentials are Cairn's own,
domain fronting (another TLS server name than the host connected to) and the metadata address are refused."""

from __future__ import annotations

import http.server
import json
import logging
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.common.config import Settings
from cairn.network import proxy as P
from cairn.network.egress import Egress
from cairn.services import build_services

KEY = b"k" * 32
ADMIN = {"X-Cairn-Actor": "urn:li:corpuser:admin"}


@pytest.fixture
def api(tmp_path, graph):
    s = build_services(Settings(database_url=f"sqlite:///{tmp_path}/c.db", service_token="svc-secret",
                                dev_trust_actor_header=True, gateway={"key_dir": str(tmp_path / "keys")}),
                       graph=graph, with_ai=False, with_gateway=False)
    return TestClient(create_api_app(s)), s


def test_rules_api_validates_and_lists(api):
    c, s = api
    assert c.post("/v1/network/egress-rules", json={"name": "pypi", "host": "pypi.org", "ports": [443]},
                  headers=ADMIN).status_code == 201
    assert c.post("/v1/network/egress-rules", json={"name": "files", "host": "*.files.example.com"},
                  headers=ADMIN).status_code == 201
    assert c.post("/v1/network/egress-rules", json={"name": "vpc", "workspace": "finance", "cidr": "10.20.0.1/16",
                                                    "ports": [5432]}, headers=ADMIN).status_code == 201
    bad = [{"name": "both", "host": "a.com", "cidr": "10.0.0.0/8"}, {"name": "none"},
           {"name": "all", "cidr": "0.0.0.0/0"}, {"name": "port", "host": "a.com", "ports": [70000]},
           {"name": "Bad Name", "host": "a.com"}, {"name": "h", "host": "a..com"}]
    for b in bad:
        assert c.post("/v1/network/egress-rules", json=b, headers=ADMIN).status_code == 422, b
    assert c.post("/v1/network/egress-rules", json={"name": "pypi", "host": "pypi.org"}, headers=ADMIN).status_code == 409
    out = c.get("/v1/network/egress-rules", headers=ADMIN).json()
    assert [r["name"] for r in out["rules"]] == ["files", "pypi", "vpc"] and out["enforced"] is False
    assert c.get("/v1/network/egress-rules/vpc", headers=ADMIN).json()["cidr"] == "10.20.0.0/16"
    assert c.put("/v1/network/egress-rules/pypi", json={"host": "pypi.org", "ports": [443, 8443]},
                 headers=ADMIN).json()["ports"] == [443, 8443]
    # What a run gets: the global rules, plus its workspace's.
    assert s.egress.effective("*") == [{"host": "*.files.example.com"}, {"host": "pypi.org", "ports": [443, 8443]}]
    assert {"cidr": "10.20.0.0/16", "ports": [5432]} in s.egress.effective("finance")
    pol = c.get("/v1/network/egress-rules/networkpolicy", params={"workspace": "finance", "namespace": "jobs"},
                headers=ADMIN).json()
    assert pol["spec"]["podSelector"]["matchLabels"] == {"cairn.io/user-code": "true", "cairn.io/workspace": "finance"}
    assert pol["spec"]["policyTypes"] == ["Egress"]
    to = [e["to"] for e in pol["spec"]["egress"]]
    assert [{"ipBlock": {"cidr": "10.20.0.0/16"}}] in to and any("namespaceSelector" in t[0] for t in to)
    assert c.delete("/v1/network/egress-rules/vpc", headers=ADMIN).status_code == 204
    assert c.delete("/v1/network/egress-rules/vpc", headers=ADMIN).status_code == 404
    # Operators only.
    assert c.get("/v1/network/egress-rules", headers={"X-Cairn-Actor": "urn:li:corpuser:bob"}).status_code == 403


def test_decisions():
    always = P.parse_always("cairn-api:8080,10.9.0.0/16,store")
    assert P.decide([], always, "cairn-api", 8080, ["10.1.1.1"]) == (True, "always")
    assert P.decide([], always, "cairn-api", 9999, ["10.1.1.1"])[0] is False
    assert P.decide([], always, "store", 9000, ["10.1.1.2"])[0] is True                 # no port: any
    assert P.decide([], always, "anything", 443, ["10.9.3.3"])[0] is True                # the lakehouse's network
    assert P.decide([], always, "pypi.org", 443, ["151.101.0.223"]) == (False, "no egress rule: only Cairn and the lakehouse")
    rules = [{"host": "pypi.org", "ports": [443]}, {"host": "*.example.com"}, {"cidr": "192.168.7.0/24", "ports": [5432]}]
    assert P.decide(rules, [], "pypi.org", 443, ["151.101.0.223"])[0] is True
    assert P.decide(rules, [], "pypi.org", 80, ["151.101.0.223"]) == (False, "port 80 is not allowed for pypi.org")
    assert P.decide(rules, [], "a.example.com", 80, ["93.184.216.34"])[0] is True
    assert P.decide(rules, [], "example.com", 443, ["93.184.216.34"])[0] is False         # *.x is not x itself
    assert P.decide(rules, [], "evil.com", 443, ["93.184.216.34"])[0] is False
    assert P.decide(rules, [], "db", 5432, ["192.168.7.9"])[0] is True                    # by address
    assert P.decide(rules, [], "db", 5433, ["192.168.7.9"])[0] is False
    # An allowed name pointing inside: private refused; the metadata address always, even with private allowed.
    assert P.decide(rules, [], "x.example.com", 443, ["10.0.0.5"])[0] is False
    assert P.decide(rules, [], "x.example.com", 443, ["10.0.0.5"], allow_private=True)[0] is True
    assert P.decide(rules, [], "x.example.com", 80, ["169.254.169.254"], allow_private=True)[0] is False
    assert P.decide(rules, [], "x.example.com", 443, ["93.184.216.34", "10.0.0.5"])[0] is False   # any address


def test_credentials():
    cred = P.issue(KEY, "finance", "run1", [{"host": "pypi.org"}], 60)
    assert P.check(KEY, cred)["w"] == "finance"
    assert P.check(b"x" * 32, cred) is None                                            # not Cairn's
    body, _, sig = cred.partition(".")
    assert P.check(KEY, body[:-2] + "AA." + sig) is None                                # tampered
    assert P.check(KEY, P.issue(KEY, "finance", "run1", [], -1)) is None                 # expired


def _hello(name: str) -> bytes:
    """A minimal TLS 1.2 ClientHello naming ``name``."""
    sni = name.encode()
    ext = (0).to_bytes(2, "big") + (len(sni) + 5).to_bytes(2, "big") + (len(sni) + 3).to_bytes(2, "big") + b"\x00" + \
        len(sni).to_bytes(2, "big") + sni
    body = b"\x03\x03" + b"\x00" * 32 + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00" + len(ext).to_bytes(2, "big") + ext
    hs = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"\x16\x03\x01" + len(hs).to_bytes(2, "big") + hs


def test_server_name():
    assert P.server_name(_hello("pypi.org")) == "pypi.org"
    assert P.server_name(b"GET / HTTP/1.1\r\n") is None


class _Hello(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        out = f"hello from {self.headers.get('host')}".encode()
        self.send_response(200)
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture
def world():
    """Two web servers ('allowed.test' and 'blocked.test', both on loopback) and the proxy, resolving those names."""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hello)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    raw = socket.create_server(("127.0.0.1", 0))        # a TLS-ish server: echoes what it gets
    names = {"allowed.test": "127.0.0.1", "blocked.test": "127.0.0.1", "cairn.test": "127.0.0.1", "meta.test": "169.254.169.254"}
    p = P.EgressProxy("127.0.0.1:0", KEY, P.parse_always(f"cairn.test:{srv.server_address[1]}"),
                      resolve=lambda h, port: [names[h]] if h in names else [], allow_private=True).start()
    yield {"port": srv.server_address[1], "proxy": p, "raw": raw}
    p.close()
    srv.shutdown()
    raw.close()


def _via(proxy_port: int, request: bytes) -> bytes:
    s = socket.create_connection(("127.0.0.1", proxy_port), 5)
    s.sendall(request)
    out = b""
    s.settimeout(5)
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            out += d
    except socket.timeout:
        pass
    s.close()
    return out


def _auth(cred: str) -> str:
    import base64

    return "Proxy-Authorization: Basic " + base64.b64encode(f"egress:{cred}".encode()).decode() + "\r\n"


def test_proxy_on_real_sockets(world, caplog):
    caplog.set_level(logging.WARNING, logger="cairn.egress")
    port, pp = world["port"], world["proxy"].port
    cred = P.issue(KEY, "finance", "run-7", [{"host": "allowed.test", "ports": [port]}], 60)
    ok = _via(pp, f"GET http://allowed.test:{port}/x HTTP/1.1\r\nHost: allowed.test\r\n{_auth(cred)}\r\n".encode())
    assert b"200 OK" in ok and b"hello from allowed.test" in ok
    no = _via(pp, f"GET http://blocked.test:{port}/x HTTP/1.1\r\nHost: blocked.test\r\n{_auth(cred)}\r\n".encode())
    assert no.startswith(b"HTTP/1.1 403") and b"no rule allows this host" in no
    # CONNECT: allowed then the tunnel carries the bytes; refused for another host.
    t = _via(pp, f"CONNECT allowed.test:{port} HTTP/1.1\r\n{_auth(cred)}\r\nGET / HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert t.startswith(b"HTTP/1.1 200 Connection established") and b"hello from x" in t
    assert _via(pp, f"CONNECT blocked.test:{port} HTTP/1.1\r\n{_auth(cred)}\r\n".encode()).startswith(b"HTTP/1.1 403")
    # Domain fronting: CONNECT to the allowed host, then ask TLS for another name.
    fr = _via(pp, f"CONNECT allowed.test:{port} HTTP/1.1\r\n{_auth(cred)}\r\n".encode() + _hello("blocked.test"))
    assert fr == b"HTTP/1.1 200 Connection established\r\n\r\n"          # and nothing reached the server
    # No credential, a forged one, an expired one: only the always list (Cairn) is reachable.
    forged = P.issue(b"z" * 32, "finance", "run-7", [{"host": "allowed.test"}], 60)
    for h in ("", _auth(forged), _auth(P.issue(KEY, "finance", "run-7", [{"host": "allowed.test"}], -5))):
        r = _via(pp, f"GET http://allowed.test:{port}/ HTTP/1.1\r\n{h}\r\n".encode())
        assert r.startswith(b"HTTP/1.1 403") and b"no valid credential" in r, r
    assert b"hello from cairn.test" in _via(pp, f"GET http://cairn.test:{port}/ HTTP/1.1\r\n\r\n".encode())
    # The metadata service, even through an allowed wildcard and with private addresses allowed.
    meta = P.issue(KEY, "finance", "run-8", [{"host": "*.test", "ports": [80]}], 60)
    assert b"link-local" in _via(pp, f"GET http://meta.test/ HTTP/1.1\r\n{_auth(meta)}\r\n".encode())
    time.sleep(0.2)
    denied = [json.loads(r.getMessage()) for r in caplog.records if r.name == "cairn.egress"]
    assert all(d["event"] == "egress.denied" for d in denied)
    targets = [(d["workspace"], d["run"], d["target"]) for d in denied]
    assert ("finance", "run-7", f"blocked.test:{port}") in targets
    assert any("TLS server name blocked.test" in d["reason"] for d in denied)
    assert any(d["workspace"] is None and "no valid credential" in d["reason"] for d in denied)
    assert world["proxy"].denied == len(denied) >= 7


def test_jobs_are_contained_or_refused(tmp_path):
    from cairn.common.db import get_engine, init_db
    from cairn.pipelines.jobs import DockerRunner, JobError, Jobs

    engine = get_engine(f"sqlite:///{tmp_path}/e.db")
    init_db(engine)
    e = Egress(engine, enforce=True, proxy="http://cairn-egress-proxy:3128", key=KEY, docker_network="")
    with pytest.raises(Exception, match="CAIRN_EGRESS_DOCKER_NETWORK"):
        e.contain(DockerRunner("img"), "*")
    e.docker_network = "cairn-user-code"
    assert e.contain(DockerRunner("img"), "*").network == "cairn-user-code"
    env = e.proxy_env("finance", "run-1", 600)
    assert env["HTTPS_PROXY"].startswith("http://egress:") and env["HTTPS_PROXY"].endswith("@cairn-egress-proxy:3128")
    assert P.check(KEY, env["HTTPS_PROXY"].split("egress:", 1)[1].rsplit("@", 1)[0])["w"] == "finance"
    # Enforced without a proxy: runs are refused rather than run unconfined.
    seen = []

    class Rec:
        image, network = "img", ""

        def run(self, *a):
            seen.append(a)
            return 0, ""
    j = Jobs(engine, Rec(), lambda a: True, lambda a: False)
    j.egress = Egress(engine, enforce=True, proxy="", key=KEY)
    with pytest.raises(JobError, match="no egress proxy"):
        j.contained("urn:li:corpuser:ann", "python", "print(1)", {}, 60, "x", "r1")
    assert seen == []
