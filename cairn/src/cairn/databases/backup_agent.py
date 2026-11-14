"""The backup sidecar of a managed PostgreSQL instance on Docker (``cairn.databases.docker_runtime``).

It runs next to the instance's PostgreSQL container, in that container's network namespace, and is the only thing
that holds the object store's credentials. Standard library only: it runs in a plain Python image, copied in by
Cairn with its configuration (``/agent/config.json``) and credentials (``/agent/credentials``, readable by its user
only), never on a command line or in the environment.

It listens on ``bind``:``port`` on the instance's own network (only the instance and it are there):

* ``GET /archive/<segment>``: PostgreSQL's ``archive_command`` names a finished WAL file; the agent reads it from
  ``pg_wal`` (the data volume, mounted read-only) and answers 200 only once it is in the store
  (``<prefix>/wal/<segment>``), so PostgreSQL keeps the file until it is safe. A file already there with the same
  content is a success (PostgreSQL may archive one twice); with other content, a refusal.
* ``GET /wal/<segment>``: ``restore_command`` fetches one (from ``restore_prefix`` first, the instance a restore
  came from, then the instance's own prefix); 404 when there is none, which ends recovery there.
* ``POST /base/<id>``: uploads the base backup that ``pg_basebackup`` wrote to ``/spool/base/<id>`` (its tar files
  and manifest), then a ``meta.json`` with the WAL range the manifest names, and removes the local copy.
* ``GET /ready``: 200 once it serves.

``python backup_agent.py fetch-base <prefix of the backup> <directory>`` downloads a base backup (a restore's
first step, in a one-shot container).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http.client
import http.server
import json
import os
import re
import shutil
import ssl
import sys
import threading
import urllib.parse
from typing import Optional

CONFIG = os.environ.get("CAIRN_AGENT_CONFIG", "/agent/config.json")
CREDENTIALS = os.environ.get("CAIRN_AGENT_CREDENTIALS", "/agent/credentials")
WAL = re.compile(r"^([0-9A-F]{24}(\.partial|\.[0-9A-F]{8}\.backup)?|[0-9A-F]{8}\.history)$")
BASE_ID = re.compile(r"^[a-z0-9-]{1,64}$")
PART = 256 * 1024 * 1024           # multipart above this size
UNSIGNED = "UNSIGNED-PAYLOAD"


class StoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Store:
    """The few S3 calls the agent needs, signed with SigV4 (unsigned payloads: TLS or a local network carries them)."""

    def __init__(self, endpoint: str, bucket: str, access_key: str, secret_key: str, region: str = "us-east-1",
                 ca_file: str = ""):
        u = urllib.parse.urlsplit(endpoint)
        self.https, self.host, self.base = u.scheme == "https", u.netloc, u.path.rstrip("/")
        self.bucket, self.ak, self.sk, self.region = bucket, access_key, secret_key, region
        self.ctx = ssl.create_default_context(cafile=ca_file or None) if self.https else None

    def _conn(self):
        if self.https:
            return http.client.HTTPSConnection(self.host, timeout=300, context=self.ctx)
        return http.client.HTTPConnection(self.host, timeout=300)

    def _headers(self, method: str, path: str, query: dict, payload: str) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
        cq = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(str(v), safe='-_.~')}"
                      for k, v in sorted(query.items()))
        h = {"host": self.host, "x-amz-content-sha256": payload, "x-amz-date": amz}
        signed = ";".join(sorted(h))
        canonical = "\n".join([method, urllib.parse.quote(path, safe="/-_.~"), cq,
                               "".join(f"{k}:{h[k]}\n" for k in sorted(h)), signed, payload])
        scope = f"{day}/{self.region}/s3/aws4_request"
        to_sign = f"AWS4-HMAC-SHA256\n{amz}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
        k = f"AWS4{self.sk}".encode()
        for part in (day, self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
        return {"x-amz-content-sha256": payload, "x-amz-date": amz,
                "Authorization": f"AWS4-HMAC-SHA256 Credential={self.ak}/{scope}, SignedHeaders={signed}, Signature={sig}"}

    def request(self, method: str, key: str, query: Optional[dict] = None, body=None, length: Optional[int] = None,
                ok=(200, 204), stream: bool = False):
        query = query or {}
        path = f"{self.base}/{self.bucket}/{key}" if key else f"{self.base}/{self.bucket}"
        payload = hashlib.sha256(body).hexdigest() if isinstance(body, bytes) else UNSIGNED
        headers = self._headers(method, path, query, payload)
        if length is not None:
            headers["Content-Length"] = str(length)
        elif isinstance(body, bytes):
            headers["Content-Length"] = str(len(body))
        url = urllib.parse.quote(path, safe="/-_.~") + ("?" + urllib.parse.urlencode(sorted(query.items()))
                                                       if query else "")
        c = self._conn()
        c.request(method, url, body=body, headers=headers)
        r = c.getresponse()
        if r.status not in ok:
            text = r.read()[:300].decode(errors="replace")
            c.close()
            raise StoreError(r.status, f"store: {method} {key}: {r.status} {text}")
        if stream:
            return r, c
        data = r.read()
        c.close()
        return r, data

    def put_file(self, key: str, path: str) -> int:
        size = os.path.getsize(path)
        if size <= PART:
            with open(path, "rb") as fh:
                self.request("PUT", key, body=fh, length=size)
            return size
        _, data = self.request("POST", key, {"uploads": ""})
        upload = re.search(rb"<UploadId>([^<]+)</UploadId>", data).group(1).decode()
        etags = []
        try:
            with open(path, "rb") as fh:
                n = 0
                while True:
                    chunk = fh.read(PART)
                    if not chunk:
                        break
                    n += 1
                    r, _ = self.request("PUT", key, {"partNumber": n, "uploadId": upload}, body=chunk)
                    etags.append((n, r.getheader("ETag")))
            doc = "<CompleteMultipartUpload>" + "".join(
                f"<Part><PartNumber>{n}</PartNumber><ETag>{e}</ETag></Part>" for n, e in etags) + "</CompleteMultipartUpload>"
            self.request("POST", key, {"uploadId": upload}, body=doc.encode())
        except Exception:
            try:
                self.request("DELETE", key, {"uploadId": upload}, ok=(200, 204, 404))
            except Exception:  # noqa: BLE001 - the store expires abandoned uploads
                pass
            raise
        return size

    def get_to(self, key: str, path: str) -> bool:
        """Downloads ``key`` to ``path`` (False when there is no such object)."""
        try:
            r, c = self.request("GET", key, stream=True)
        except StoreError as e:
            if e.status == 404:
                return False
            raise
        tmp = path + ".part"
        try:
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(r, fh, 1024 * 1024)
        finally:
            c.close()
        os.replace(tmp, path)
        return True

    def get(self, key: str) -> Optional[bytes]:
        try:
            return self.request("GET", key)[1]
        except StoreError as e:
            if e.status == 404:
                return None
            raise


def load() -> tuple[dict, Store]:
    with open(CONFIG) as fh:
        cfg = json.load(fh)
    with open(CREDENTIALS) as fh:
        cred = json.load(fh)
    return cfg, Store(cfg["endpoint"], cfg["bucket"], cred["access_key"], cred["secret_key"],
                      cfg.get("region") or "us-east-1", cfg.get("ca_file") or "")


def manifest_range(path: str) -> dict:
    """The WAL range a ``backup_manifest`` names (the backup is consistent at its end)."""
    try:
        with open(path) as fh:
            ranges = json.load(fh).get("WAL-Ranges") or []
    except (OSError, ValueError):
        return {}
    if not ranges:
        return {}
    return {"timeline": ranges[-1].get("Timeline"), "start_lsn": ranges[0].get("Start-LSN"),
            "end_lsn": ranges[-1].get("End-LSN")}


def make_handler(cfg: dict, store: Store):
    prefix = cfg["prefix"].strip("/")
    sources = [p.strip("/") for p in (cfg.get("restore_prefix"), cfg["prefix"]) if p]
    spool = cfg.get("spool") or "/spool"
    pg_wal = cfg.get("pg_wal") or "/data/pgdata/pg_wal"
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _answer(self, status: int, body: bytes = b"", ctype: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body and self.command != "HEAD":
                self.wfile.write(body)

        def log_message(self, fmt, *args):         # one line per request, no bodies
            sys.stderr.write("agent: " + (fmt % args) + "\n")

        def do_GET(self):
            if self.path == "/ready":
                return self._answer(200, b"ok")
            m = re.match(r"^/archive/([^/]+)$", self.path)
            if m and WAL.match(m.group(1)):
                return self._archive(m.group(1))
            m = re.match(r"^/wal/([^/]+)$", self.path)
            if not m or not WAL.match(m.group(1)):
                return self._answer(404)
            name = m.group(1)
            for p in sources:
                try:
                    r, c = store.request("GET", f"{p}/wal/{name}", stream=True)
                except StoreError as e:
                    if e.status == 404:
                        continue
                    return self._answer(502, str(e).encode())
                try:
                    data = r.read()
                finally:
                    c.close()
                return self._answer(200, data, "application/octet-stream")
            return self._answer(404)

        def _archive(self, name: str):
            try:
                with open(os.path.join(pg_wal, name), "rb") as fh:
                    data = fh.read()
            except OSError as e:
                return self._answer(404, str(e).encode()[:300])
            key = f"{prefix}/wal/{name}"
            try:
                there = store.get(key)
                if there is not None:
                    # PostgreSQL may archive a file twice (a crash after the upload): the same file is fine.
                    return self._answer(200 if there == data else 409, b"exists")
                store.request("PUT", key, body=data)
            except Exception as e:  # noqa: BLE001 - not archived: PostgreSQL keeps the file and tries again
                return self._answer(502, str(e).encode()[:300])
            return self._answer(200, b"archived")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            m = re.match(r"^/base/([^/]+)$", self.path)
            if m and BASE_ID.match(m.group(1)):
                self.rfile.read(length)
                bid = m.group(1)
                src = os.path.join(spool, "base", bid)
                if not os.path.isdir(src):
                    return self._answer(404, b"no such backup in the spool")
                with lock:
                    try:
                        files, size = [], 0
                        for f in sorted(os.listdir(src)):
                            size += store.put_file(f"{prefix}/base/{bid}/{f}", os.path.join(src, f))
                            files.append(f)
                        meta = {"id": bid, "files": files, "bytes": size,
                                **manifest_range(os.path.join(src, "backup_manifest"))}
                        store.request("PUT", f"{prefix}/base/{bid}/meta.json", body=json.dumps(meta).encode())
                    except Exception as e:  # noqa: BLE001 - the backup failed; Cairn records why
                        return self._answer(502, str(e).encode()[:300])
                    shutil.rmtree(src, ignore_errors=True)
                return self._answer(200, json.dumps(meta).encode(), "application/json")
            return self._answer(404)

    return Handler


def serve() -> None:
    cfg, store = load()
    srv = http.server.ThreadingHTTPServer((cfg.get("bind") or "127.0.0.1", int(cfg.get("port") or 8008)), make_handler(cfg, store))
    srv.daemon_threads = True
    sys.stderr.write("agent: serving\n")
    srv.serve_forever()


def fetch_base(prefix: str, out: str) -> None:
    """Downloads one base backup's files (``<prefix>/meta.json`` lists them) into ``out``."""
    _, store = load()
    meta = store.get(f"{prefix}/meta.json")
    if meta is None:
        raise SystemExit(f"no base backup at {prefix}")
    os.makedirs(out, exist_ok=True)
    for f in json.loads(meta)["files"]:
        if "/" in f or f.startswith("."):
            raise SystemExit(f"unexpected file in the backup: {f}")
        if not store.get_to(f"{prefix}/{f}", os.path.join(out, f)):
            raise SystemExit(f"missing from the backup: {f}")
    for f in os.listdir(out):
        os.chmod(os.path.join(out, f), 0o644)
    os.chmod(out, 0o755)
    sys.stderr.write("agent: base backup fetched\n")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "fetch-base":
        fetch_base(sys.argv[2].strip("/"), sys.argv[3])
    else:
        serve()
