"""Remote buckets on Azure Blob Storage and Google Cloud Storage that answer badly: stand-in servers (no
emulator needed) that page forever, send endless or stalled bodies, hand out an upload session on another
host, or would resolve dot segments in an object name. The store must give up with an error, keep its
credentials to the endpoint it was given, and never write outside the remote's container and prefix."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, REGION

MASTER_ENV = {"CAIRN_STORE_REMOTE_SYNC_INTERVAL": "3600"}


def admin(st, method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{st.endpoint}/-/admin/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=timeout)
    return r.status_code, (r.json() if r.content else None)


def serve(handler):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Quiet(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def mounted(custom_store, tmp_path, spec, write_back=False, env=None):
    remotes = tmp_path / "remotes.json"
    remotes.write_text(json.dumps([spec]))
    st = custom_store({"CAIRN_STORE_REMOTES_FILE": str(remotes), **MASTER_ENV, **(env or {})})
    st.client().create_bucket(Bucket="mirror")
    assert admin(st, "PUT", "buckets/mirror/remote", {"remote": spec["id"], "write_back": write_back})[0] == 200
    return st


def gcs_spec(endpoint, **kw):
    return {"id": "g", "kind": "gcs", "endpoint": endpoint, "bucket": "lake", "auth": "none", **kw}


def azure_spec(endpoint, **kw):
    return {"id": "az", "kind": "azure", "endpoint": f"{endpoint}/acct", "bucket": "box", "access_key": "acct",
            "sas": "sv=2021-12-02&sig=c2VjcmV0", **kw}


def test_a_listing_that_repeats_its_page_token_is_refused(custom_store, tmp_path):
    pages = []

    class H(Quiet):
        def do_GET(self):
            pages.append(self.path)
            doc = {"items": [{"name": "a.txt", "size": "1", "etag": "e1", "updated": "2026-01-01T00:00:00Z"}],
                   "nextPageToken": "again"}
            self.reply(200, json.dumps(doc).encode())

    srv, url = serve(H)
    try:
        st = mounted(custom_store, tmp_path, gcs_spec(url))
        t = time.time()
        code, out = admin(st, "POST", "buckets/mirror/remote/sync", timeout=30)
        assert code == 502, out
        assert time.time() - t < 20 and len(pages) <= 3, len(pages)
    finally:
        srv.shutdown()


def test_an_endless_error_body_does_not_hold_the_store(custom_store, tmp_path):
    class H(Quiet):
        def do_GET(self):
            self.send_response(500)
            self.send_header("content-type", "application/xml")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            chunk = b"<Error>" + b"x" * 65536 + b"</Error>"
            try:
                while True:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            except OSError:
                pass

    srv, url = serve(H)
    try:
        st = mounted(custom_store, tmp_path, azure_spec(url))
        t = time.time()
        code, out = admin(st, "POST", "buckets/mirror/remote/sync", timeout=30)
        assert code == 502 and time.time() - t < 20, out
    finally:
        srv.shutdown()


def test_an_endless_listing_is_cut_off(custom_store, tmp_path):
    class H(Quiet):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "application/xml")
            self.send_header("transfer-encoding", "chunked")
            self.end_headers()
            chunk = b"<!--" + b"x" * (1 << 20) + b"-->"
            try:
                self.wfile.write(b"%x\r\n%s\r\n" % (6, b"<Enum>"))
                while True:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
            except OSError:
                pass

    srv, url = serve(H)
    try:
        st = mounted(custom_store, tmp_path, azure_spec(url))
        code, out = admin(st, "POST", "buckets/mirror/remote/sync", timeout=60)
        assert code == 502, out
    finally:
        srv.shutdown()


def test_a_stalled_listing_times_out(custom_store, tmp_path):
    stop = threading.Event()

    class H(Quiet):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", "100")
            self.end_headers()
            self.wfile.write(b'{"items": [')
            self.wfile.flush()
            stop.wait(300)

    srv, url = serve(H)
    try:
        st = mounted(custom_store, tmp_path, gcs_spec(url))
        t = time.time()
        code, out = admin(st, "POST", "buckets/mirror/remote/sync", timeout=150)
        assert code == 502 and time.time() - t < 140, out
    finally:
        stop.set()
        srv.shutdown()
