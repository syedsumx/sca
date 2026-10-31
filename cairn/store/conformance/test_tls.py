"""TLS: HTTPS with a CA-signed certificate, mutual TLS, certificate reloading, aws:SecureTransport, SSE-C
over TLS, the healthcheck command, and HTTPS webhook delivery trusting an internal CA."""

import hashlib
import hmac
import http.server
import json
import os
import shutil
import ssl
import subprocess
import threading
import time

import boto3
import pytest
import requests
from botocore.config import Config
from botocore.exceptions import ConnectionClosedError, EndpointConnectionError, SSLError

from conftest import ADMIN, REGION


def openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    d = tmp_path_factory.mktemp("pki")
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
            "-subj", "/CN=Cairn Test CA", cwd=d)
    (d / "san.ext").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    for name in ("server", "server2", "client"):
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}", cwd=d)
        openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", f"{name}.pem",
                "-days", "2", "-extfile", "san.ext", cwd=d)
    return d


def tls_store(custom_store, pki, **env):
    st = custom_store({"CAIRN_STORE_TLS_CERT": str(pki / "cert.pem"), "CAIRN_STORE_TLS_KEY": str(pki / "cert.key"), **env})
    st.endpoint = st.endpoint.replace("http://", "https://")
    return st


@pytest.fixture
def certs(pki, tmp_path):
    """The store's current certificate files (copied, so a test can replace them)."""
    shutil.copy(pki / "server.pem", pki / "cert.pem")
    shutil.copy(pki / "server.key", pki / "cert.key")
    return pki


def client(st, pki, **kw):
    return boto3.client("s3", endpoint_url=st.endpoint, aws_access_key_id=ADMIN[0], aws_secret_access_key=ADMIN[1],
                        region_name=REGION, verify=str(pki / "ca.pem"),
                        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 0}, **kw))


def test_https_and_secure_transport(custom_store, certs):
    st = tls_store(custom_store, certs)
    s3 = client(st, certs)
    s3.create_bucket(Bucket="tls")
    s3.put_bucket_policy(Bucket="tls", Policy=json.dumps({"Statement": [{"Effect": "Deny", "Principal": "*", "Action": "s3:*",
                                                                         "Resource": "arn:aws:s3:::tls/*",
                                                                         "Condition": {"Bool": {"aws:SecureTransport": "false"}}}]}))
    s3.put_object(Bucket="tls", Key="k", Body=b"over tls")
    assert s3.get_object(Bucket="tls", Key="k")["Body"].read() == b"over tls"
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get(st.endpoint.replace("https://", "http://") + "/-/healthy", timeout=5)
    key = os.urandom(32)
    s3.put_object(Bucket="tls", Key="c", Body=b"customer key", SSECustomerAlgorithm="AES256", SSECustomerKey=key)
    assert s3.get_object(Bucket="tls", Key="c", SSECustomerAlgorithm="AES256", SSECustomerKey=key)["Body"].read() == b"customer key"
    env = {**os.environ, "CAIRN_STORE_ADDR": f"127.0.0.1:{st.port}", "CAIRN_STORE_TLS_CERT": str(certs / "cert.pem")}
    assert subprocess.run([st.binary, "healthcheck"], env=env, timeout=20).returncode == 0


def test_mutual_tls(custom_store, certs):
    st = tls_store(custom_store, certs, CAIRN_STORE_TLS_CLIENT_CA=str(certs / "ca.pem"))
    with pytest.raises((SSLError, ConnectionClosedError, EndpointConnectionError)):  # no client certificate: refused
        client(st, certs).list_buckets()
    ok = client(st, certs, client_cert=(str(certs / "client.pem"), str(certs / "client.key")))
    assert "Buckets" in ok.list_buckets()
    env = {**os.environ, "CAIRN_STORE_ADDR": f"127.0.0.1:{st.port}", "CAIRN_STORE_TLS_CERT": str(certs / "cert.pem"),
           "CAIRN_STORE_TLS_PROBE_CERT": str(certs / "client.pem"), "CAIRN_STORE_TLS_PROBE_KEY": str(certs / "client.key")}
    assert subprocess.run([st.binary, "healthcheck"], env=env, timeout=20).returncode == 0


def served_cert(st):
    host, port = "127.0.0.1", st.port
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
    import socket
    with socket.create_connection((host, port), timeout=5) as raw, ctx.wrap_socket(raw) as s:
        return hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()


def test_certificate_reload(custom_store, certs):
    st = tls_store(custom_store, certs, CAIRN_STORE_TLS_RELOAD_SECONDS="1")
    before = served_cert(st)
    (certs / "cert.key").write_bytes((certs / "server2.key").read_bytes())
    (certs / "cert.pem").write_bytes((certs / "server2.pem").read_bytes())
    deadline = time.time() + 10
    while time.time() < deadline and served_cert(st) == before:
        time.sleep(0.5)
    assert served_cert(st) != before
    (certs / "cert.pem").write_text("not a certificate")  # a broken renewal is refused; the current one stays
    time.sleep(2.5)
    assert "Buckets" in client(st, certs).list_buckets()


def test_https_webhooks_with_an_internal_ca(custom_store, certs, tmp_path):
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            want = "sha256=" + hmac.new(b"webhook-secret-0123456789", body, hashlib.sha256).hexdigest()
            got.append((self.headers["X-Cairn-Signature"] == want, json.loads(body)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(certs / "server.pem"), str(certs / "server.key"))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    targets = tmp_path / "t.json"
    targets.write_text(json.dumps([{"type": "webhook", "id": "secure", "url": f"https://localhost:{srv.server_port}/e",
                                    "secret": "webhook-secret-0123456789"}]))
    st = custom_store({"CAIRN_STORE_EVENT_TARGETS_FILE": str(targets), "CAIRN_STORE_CA_FILE": str(certs / "ca.pem")})
    s3 = st.client()
    s3.create_bucket(Bucket="hooks")
    s3.put_bucket_notification_configuration(Bucket="hooks", NotificationConfiguration={"QueueConfigurations": [
        {"QueueArn": "arn:cairn:store:::secure", "Events": ["s3:ObjectCreated:*"]}]})
    s3.put_object(Bucket="hooks", Key="k", Body=b"x")
    deadline = time.time() + 10
    while time.time() < deadline and not got:
        time.sleep(0.2)
    srv.shutdown()
    assert got and got[0][0] and got[0][1]["Records"][0]["s3"]["object"]["key"] == "k"
