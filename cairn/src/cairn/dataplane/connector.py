"""The data plane connector: runs in the customer's cluster beside its Trino, Spark and jobs, and connects only out, to
Cairn's gateway (``python -m cairn.dataplane.connector``; ``cairn dataplane connect``; ``helm/cairn-dataplane``).

* **Enrolment.** With a single-use token (``CAIRN_DATAPLANE_ENROLLMENT_TOKEN_FILE``) it makes its own Ed25519 and X25519
  keys and registers them; they stay in its state directory (``CAIRN_DATAPLANE_STATE_DIR``, a volume, mode 0600).
  ``CAIRN_DATAPLANE_CLIENT_CERT``/``_KEY`` add a client certificate for a gateway that asks for one (mTLS);
  ``CAIRN_DATAPLANE_CA_FILE`` trusts a private CA.
* **The tunnel.** It long-polls the gateway for calls (each sealed to its key), runs them here and posts the answers
  (sealed to the gateway's key): ``trino.http`` (only to ``CAIRN_DATAPLANE_TRINO_URL``), ``job.run`` (Docker through
  the engine's socket, or Kubernetes Jobs with its service account), ``storage.presign`` (a URL for one object in an
  allowed bucket, signed here with the storage key, which never leaves the cluster) and ``ping``.
* **Trino's access control.** It answers Trino's OPA calls on ``CAIRN_DATAPLANE_OPA_LISTEN`` (``/allow``, ``/batch``,
  ``/row-filters``, ``/column-mask``) by asking Cairn's PDP through the gateway. Anything but a decision from Cairn
  (the gateway away, this connector revoked) is answered 503, and Trino refuses the query.

Only the standard library, httpx and cryptography: it also runs from a bare Python image.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import io
import json
import logging
import os
import signal
import socket
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import quote, urlsplit

import httpx

from cairn import __version__
from cairn.agent import crypto
from cairn.agent.client import AgentState, GatewayHTTPError, _raise, tls_context

logger = logging.getLogger("cairn.dataplane")
STRIP_ENV = {"HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"}
MAX_FILES_ENV = 96 << 10          # what one environment variable may hold (Linux: 128 KiB per string)
MARKER = "=====CAIRN-JOB-OUTPUT====="


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _read(path: str) -> str:
    with open(path) as fh:
        return fh.read().strip()


class ConnectorError(Exception):
    pass


# --------------------------------------------------------------------------- storage: presigned URLs (SigV4)
def presign(endpoint: str, region: str, access_key: str, secret_key: str, method: str, bucket: str, key: str,
            ttl_s: int, now: Optional[datetime.datetime] = None) -> str:
    """An S3 presigned URL (path style, AWS Signature Version 4, query authentication)."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    u = urlsplit(endpoint)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{day}/{region}/s3/aws4_request"
    uri = "/" + quote(bucket, safe="") + "/" + quote(key, safe="/-_.~")
    params = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{access_key}/{scope}", "X-Amz-Date": amz,
              "X-Amz-Expires": str(int(ttl_s)), "X-Amz-SignedHeaders": "host"}
    query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(params.items()))
    canonical = "\n".join([method, uri, query, f"host:{u.netloc}", "", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = ("AWS4" + secret_key).encode()
    for part in (day, region, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{u.scheme}://{u.netloc}{uri}?{query}&X-Amz-Signature={sig}"


# --------------------------------------------------------------------------- jobs: Docker through the engine API
def job_script(kind: str, timeout_s: int) -> str:
    """Unpack the run's files into /tmp/work, then run it as Cairn's jobs do (log, marker, output)."""
    if kind == "notebook":
        run = (f"jupyter nbconvert --to notebook --execute /tmp/work/job.ipynb --output-dir /tmp --output out.ipynb "
               f"--ExecutePreprocessor.timeout={timeout_s} >/tmp/log 2>&1")
        out = "/tmp/out.ipynb"
    else:
        run = '"$(command -v python3 || command -v python)" -u /tmp/work/job.py >/tmp/out.txt 2>/tmp/log'
        out = "/tmp/out.txt"
    # Plain files by their base names only: nothing in the archive can write outside /tmp/work.
    unpack = ('mkdir -p /tmp/work && "$(command -v python3 || command -v python)" -c "import base64,io,os,tarfile;'
              "t=tarfile.open(fileobj=io.BytesIO(base64.b64decode(os.environ['CAIRN_JOB_FILES'])));"
              "[open('/tmp/work/'+os.path.basename(m.name),'wb').write(t.extractfile(m).read()) "
              'for m in t.getmembers() if m.isfile()]" && unset CAIRN_JOB_FILES && cd /tmp/work')
    return f"{unpack} && {{ {run}; rc=$?; cat /tmp/log; echo '{MARKER}'; cat {out} 2>/dev/null; exit $rc; }}"


def pack(files: dict[str, str]) -> str:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(os.path.basename(name))
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def _demux(raw: bytes) -> str:
    """Docker's multiplexed log stream (8-byte frame headers) as text."""
    out, p = [], 0
    while p + 8 <= len(raw):
        size = int.from_bytes(raw[p + 4:p + 8], "big")
        out.append(raw[p + 8:p + 8 + size])
        p += 8 + size
    return b"".join(out).decode(errors="replace") if out else raw.decode(errors="replace")


class EngineRunner:
    """A container per run through the Docker engine's API (the connector has no docker CLI): non-root, no
    capabilities, no new privileges, read-only root, memory, CPU and process limits, on the data plane's network."""

    def __init__(self, socket_path: str, network: str, memory_mb: int = 2048, cpus: float = 2.0):
        self.socket_path, self.network, self.memory_mb, self.cpus = socket_path, network, memory_mb, cpus

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.HTTPTransport(uds=self.socket_path), base_url="http://docker", timeout=60)

    def run(self, run_id: str, kind: str, files: dict[str, str], env: dict[str, str], timeout_s: int, image: str,
            uid: int) -> tuple[int, str]:
        packed = pack(files)
        if len(packed) > MAX_FILES_ENV:
            raise ConnectorError(f"the run's code is over {MAX_FILES_ENV} bytes packed")
        name = f"cairn-dp-job-{run_id[:16]}"
        body = {"Image": image, "Entrypoint": ["sh", "-c"], "Cmd": [job_script(kind, timeout_s)],
                "Env": [f"{k}={v}" for k, v in {**env, "CAIRN_JOB_FILES": packed}.items()],
                "User": f"{uid}:{uid}", "WorkingDir": "/tmp",
                "Labels": {"app.kubernetes.io/part-of": "cairn", "cairn.io/run": run_id[:16]},
                "HostConfig": {"NetworkMode": self.network or "none", "CapDrop": ["ALL"],
                               "SecurityOpt": ["no-new-privileges"], "ReadonlyRootfs": True,
                               "Memory": self.memory_mb << 20, "NanoCpus": int(self.cpus * 1e9), "PidsLimit": 512,
                               "Tmpfs": {"/tmp": "rw,exec,size=512m",
                                         "/home/job": f"rw,size=256m,uid={uid},gid={uid},mode=0700"}}}
        with self._client() as d:
            r = d.post("/containers/create", params={"name": name}, json=body)
            if r.status_code >= 300:
                raise ConnectorError(f"docker: create: {r.status_code} {r.text[:300]}")
            cid = r.json()["Id"]
            try:
                r = d.post(f"/containers/{cid}/start")
                if r.status_code >= 300:
                    raise ConnectorError(f"docker: start: {r.status_code} {r.text[:300]}")
                try:
                    w = d.post(f"/containers/{cid}/wait", timeout=timeout_s + 30)
                    rc = int(w.json().get("StatusCode", 1))
                except httpx.TimeoutException:
                    d.post(f"/containers/{cid}/kill")
                    return 124, f"the run went over its time limit ({timeout_s // 60} minutes) and was stopped"
                logs = d.get(f"/containers/{cid}/logs", params={"stdout": "true", "stderr": "true"})
                return rc, _demux(logs.content)
            finally:
                d.delete(f"/containers/{cid}", params={"force": "true"})


class KubeJobRunner:
    """A Kubernetes Job per run in the data plane's jobs namespace, with the connector's service account: the code in a
    ConfigMap, the environment (the owner's token) in a Secret, both removed with the Job after the run."""

    SA = "/var/run/secrets/kubernetes.io/serviceaccount"

    def __init__(self, namespace: str, poll: float = 2.0):
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        if not host:
            raise ConnectorError("jobs on Kubernetes: not in a cluster")
        self.api, self.namespace, self.poll = f"https://{host}:{port}", namespace, poll

    def _call(self, method: str, path: str, body: Any = None, ok404: bool = False, raw: bool = False) -> Any:
        with open(f"{self.SA}/token") as fh:
            token = fh.read().strip()
        with httpx.Client(base_url=self.api, verify=f"{self.SA}/ca.crt", timeout=30,
                          headers={"Authorization": f"Bearer {token}"}) as c:
            r = c.request(method, path, json=body)
        if r.status_code == 404 and ok404:
            return None
        if r.status_code >= 300:
            raise ConnectorError(f"kubernetes: {method} {path}: {r.status_code} {r.text[:300]}")
        return r.text if raw else (r.json() if r.content else {})

    def run(self, run_id: str, kind: str, files: dict[str, str], env: dict[str, str], timeout_s: int, image: str,
            uid: int) -> tuple[int, str]:
        ns, name = self.namespace, f"cairn-dp-job-{run_id[:12]}"
        labels = {"app.kubernetes.io/part-of": "cairn", "app.kubernetes.io/component": "dataplane-job",
                  "cairn.io/run": run_id[:12], "cairn.io/user-code": "true"}
        base = f"/api/v1/namespaces/{ns}"
        files_env = pack(files)
        if len(files_env) > MAX_FILES_ENV:
            raise ConnectorError(f"the run's code is over {MAX_FILES_ENV} bytes packed")
        try:
            self._call("POST", f"{base}/secrets", {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                                                    "metadata": {"name": name, "labels": labels},
                                                    "stringData": {**env, "CAIRN_JOB_FILES": files_env}})
            pod = {"restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
                   "securityContext": {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid,
                                       "seccompProfile": {"type": "RuntimeDefault"}},
                   "containers": [{"name": "job", "image": image, "command": ["sh", "-c", job_script(kind, timeout_s)],
                                   "envFrom": [{"secretRef": {"name": name}}],
                                   "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"memory": "2Gi"}},
                                   "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                       "capabilities": {"drop": ["ALL"]}},
                                   "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}]}],
                   "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "512Mi"}}]}
            self._call("POST", f"/apis/batch/v1/namespaces/{ns}/jobs", {
                "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "labels": labels},
                "spec": {"backoffLimit": 0, "activeDeadlineSeconds": timeout_s + 30, "ttlSecondsAfterFinished": 600,
                         "template": {"metadata": {"labels": labels}, "spec": pod}}})
            end = time.time() + timeout_s + 300
            while True:
                st = (self._call("GET", f"/apis/batch/v1/namespaces/{ns}/jobs/{name}").get("status") or {})
                if st.get("succeeded") or st.get("failed"):
                    break
                if time.time() > end:
                    return 124, "the run went over its time limit and was stopped"
                time.sleep(self.poll)
            pods = self._call("GET", f"{base}/pods?labelSelector=cairn.io/run={run_id[:12]}").get("items") or []
            text = self._call("GET", f"{base}/pods/{pods[0]['metadata']['name']}/log", raw=True) if pods else ""
            if st.get("failed"):
                reason = ((st.get("conditions") or [{}])[-1] or {}).get("reason", "")
                return (124 if reason == "DeadlineExceeded" else 1), text or f"the job failed ({reason})"
            return 0, text
        finally:
            for path in (f"/apis/batch/v1/namespaces/{ns}/jobs/{name}?propagationPolicy=Background", f"{base}/secrets/{name}"):
                try:
                    self._call("DELETE", path, ok404=True)
                except Exception:  # noqa: BLE001 - the Job's TTL removes what is left
                    logger.warning("could not delete %s", path)


def rotation_proof_bytes(connector_id: str, signing_public: str, encryption_public: str) -> bytes:
    return f"cairn-dataplane-rotate|{connector_id}|{signing_public}|{encryption_public}".encode()


# --------------------------------------------------------------------------- the connector
class Connector:
    def __init__(self, gateway: str, state_dir: str, trino_url: str = "", verify: Any = True, cert: Any = None,
                 jobs: Optional[dict] = None, storage: Optional[dict] = None, wait_s: float = 10.0):
        self.base = gateway.rstrip("/")
        self.base_path = urlsplit(self.base).path.rstrip("/")
        os.makedirs(state_dir, exist_ok=True)
        self.state = AgentState(state_dir)
        self.trino = trino_url.rstrip("/")
        self.jobs = jobs or {}
        self.storage = storage or {}
        self.wait_s = wait_s
        self.http = httpx.Client(timeout=httpx.Timeout(wait_s + 30, connect=10), verify=tls_context(verify, cert))
        self.trino_http = httpx.Client(timeout=httpx.Timeout(120, connect=10))
        self.stop = threading.Event()
        self.authorized = threading.Event()      # cleared when Cairn refuses this connector: everything fails closed
        self.last_error = ""

    # ---- enrolment and signed calls ------------------------------------------------------------------
    @property
    def connector_id(self) -> str:
        return self.state.meta["connector_id"]

    def capabilities(self) -> dict:
        return {"trino": bool(self.trino), "jobs": self.jobs.get("runtime") or None,
                "job_images": list(self.jobs.get("images") or []), "storage": bool(self.storage.get("endpoint")),
                "buckets": list(self.storage.get("buckets") or [])}

    def enroll(self, token: str) -> dict:
        ident = crypto.Identity.generate()
        r = self.http.post(f"{self.base}/dataplanes/tunnel/enroll", json={
            "token": token, "signing_public": ident.signing_public, "encryption_public": ident.encryption_public,
            "hostname": socket.gethostname(), "version": __version__, "capabilities": self.capabilities()})
        _raise(r)
        body = r.json()
        self.state.identity = ident
        self.state.meta = {"connector_id": body["connector_id"], "agent_id": body["connector_id"],
                           "dataplane": body["dataplane"], "gateway_encryption_public": body["gateway_encryption_public"],
                           "gateway_signing_public": body["gateway_signing_public"], "gateway_url": self.base}
        self.state.save()
        return body

    def signed(self, rel: str, payload: dict, timeout: Optional[float] = None) -> Any:
        if self.state.identity is None or not self.state.meta.get("connector_id"):
            raise ConnectorError("not enrolled")
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = crypto.sign_request(self.state.identity, self.connector_id, "POST", rel, body)
        headers["Content-Type"] = "application/json"
        r = self.http.post(f"{self.base}{rel}", content=body, headers=headers,
                           **({"timeout": timeout} if timeout else {}))
        if r.status_code == 401:
            self.authorized.clear()
        _raise(r)
        self.authorized.set()
        return r.json() if r.content else None

    def rotate_keys(self) -> dict:
        new = crypto.Identity.generate()
        self.state.meta["next_identity"] = new.to_dict()
        self.state.save()
        proof = crypto.sign_bytes(new, rotation_proof_bytes(self.connector_id, new.signing_public, new.encryption_public))
        out = self.signed(f"/dataplanes/tunnel/{self.connector_id}/keys/rotate", {
            "signing_public": new.signing_public, "encryption_public": new.encryption_public, "proof": proof})
        old = self.state.identity
        self.state.identity = new
        self.state.meta.pop("next_identity", None)
        self.state.meta["previous_identity"] = old.to_dict() if old else None
        self.state.save()
        return out

    # ---- calls -------------------------------------------------------------------------------------
    def poll_once(self) -> int:
        out = self.signed(f"/dataplanes/tunnel/{self.connector_id}/poll",
                          {"wait_s": self.wait_s, "capabilities": self.capabilities()}, timeout=self.wait_s + 30)
        calls = (out or {}).get("calls") or []
        for c in calls:
            threading.Thread(target=self._serve_call, args=(c,), daemon=True, name=f"dp-{c['call_id'][-8:]}").start()
        return len(calls)

    def _open(self, call: dict) -> dict:
        aad = crypto.aad_for(self.connector_id, call["call_id"])
        for i, ident in enumerate(self.state.openers):
            try:
                return json.loads(crypto.open_sealed(ident.encryption_key, call["envelope"], aad))
            except crypto.CryptoError:
                if i == len(self.state.openers) - 1:
                    raise
        raise crypto.CryptoError("no key to open the call with")

    def _serve_call(self, call: dict) -> None:
        try:
            doc = self._open(call)
            answer = {"result": self.handle(doc["kind"], doc.get("request") or {})}
        except Exception as e:  # noqa: BLE001 - every failure goes back as the call's answer
            logger.warning("call %s (%s) failed: %s", call.get("call_id"), call.get("kind"), e)
            answer = {"error": str(e)[:1500] or type(e).__name__}
        envelope = crypto.seal(self.state.gateway_public, json.dumps(answer).encode(),
                               crypto.aad_for(self.connector_id, call["call_id"], "answer"))
        try:
            self.signed(f"/dataplanes/tunnel/{self.connector_id}/calls/{call['call_id']}/answer", {"envelope": envelope})
        except Exception as e:  # noqa: BLE001 - the caller times out and is told so
            logger.warning("answer to %s not delivered: %s", call.get("call_id"), e)

    def handle(self, kind: str, req: dict) -> Any:
        if kind == "ping":
            return {"ok": True, "capabilities": self.capabilities(), "version": __version__}
        if kind == "trino.http":
            return self._trino(req)
        if kind == "job.run":
            return self._job(req)
        if kind == "storage.presign":
            return self._presign(req)
        raise ConnectorError(f"this connector does not run {kind}")

    def _trino(self, req: dict) -> dict:
        if not self.trino:
            raise ConnectorError("this data plane has no Trino (CAIRN_DATAPLANE_TRINO_URL)")
        url = str(req.get("url") or "")
        if not (url == self.trino or url.startswith(self.trino + "/")):
            raise ConnectorError(f"the tunnel reaches only this data plane's Trino ({self.trino}), not {url[:200]}")
        # The data plane's Trino trusts its connector for who asks (its network policy admits only the connector);
        # Cairn's sign-in token is not passed on into the customer's cluster.
        headers = {k: v for k, v in (req.get("headers") or {}).items() if k.lower() != "authorization"}
        r = self.trino_http.request(str(req.get("method") or "GET"), url, headers=headers,
                                    content=base64.b64decode(req.get("body") or ""))
        keep = {k: v for k, v in r.headers.items() if k.lower() in ("content-type",) or k.lower().startswith("x-trino-")}
        return {"status": r.status_code, "headers": keep, "body": base64.b64encode(r.content).decode()}

    def _job(self, req: dict) -> dict:
        rt = self.jobs.get("runtime")
        if not rt:
            raise ConnectorError("this data plane runs no jobs (CAIRN_DATAPLANE_JOBS_RUNTIME)")
        image = req.get("image") or self.jobs.get("image")
        allowed = set(self.jobs.get("images") or []) | ({self.jobs["image"]} if self.jobs.get("image") else set())
        if image not in allowed:
            raise ConnectorError(f"image {image} is not one this data plane runs (CAIRN_DATAPLANE_JOBS_IMAGES)")
        uid = int(req.get("uid") or 1000)
        if uid <= 0:
            raise ConnectorError("jobs never run as root")
        env = {k: str(v) for k, v in (req.get("env") or {}).items() if k not in STRIP_ENV}
        if self.jobs.get("trino_url"):                     # jobs read through this data plane's Trino
            env["CAIRN_TRINO_URL"] = self.jobs["trino_url"]
        if self.jobs.get("proxy"):                         # and leave the cluster, if at all, through its own proxy
            env.update({k: self.jobs["proxy"] for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")})
        run_id, kind, timeout_s = str(req["run_id"]), str(req["kind"]), int(req["timeout_s"])
        files = {str(k): str(v) for k, v in (req.get("files") or {}).items()}
        if rt == "docker":
            rc, text = EngineRunner(self.jobs.get("socket") or "/var/run/docker.sock", self.jobs.get("network") or "",
                                    int(self.jobs.get("memory_mb") or 2048)).run(run_id, kind, files, env, timeout_s,
                                                                                 image, uid)
        elif rt == "kubernetes":
            rc, text = KubeJobRunner(self.jobs.get("namespace") or "cairn-dataplane-jobs").run(
                run_id, kind, files, env, timeout_s, image, uid)
        else:
            raise ConnectorError(f"unknown jobs runtime {rt}")
        return {"rc": rc, "text": text[-(4 << 20):]}

    def _presign(self, req: dict) -> dict:
        s = self.storage
        if not s.get("endpoint"):
            raise ConnectorError("this data plane signs no storage URLs (CAIRN_DATAPLANE_STORAGE_ENDPOINT)")
        bucket, key, method = str(req.get("bucket") or ""), str(req.get("key") or ""), str(req.get("method") or "GET")
        if bucket not in (s.get("buckets") or []):
            raise ConnectorError(f"bucket {bucket} is not one Cairn may be handed URLs for (CAIRN_DATAPLANE_STORAGE_BUCKETS)")
        if method not in ("GET", "PUT") or not key or ".." in key.split("/"):
            raise ConnectorError("one object (GET or PUT)")
        ttl = max(1, min(int(req.get("ttl_seconds") or 300), int(s.get("max_ttl") or 900)))
        url = presign(s["endpoint"], s.get("region") or "us-east-1", _read(s["access_key_file"]),
                      _read(s["secret_key_file"]), method, bucket, key, ttl)
        return {"url": url, "method": method, "expires_in": ttl}

    # ---- OPA relay for the data plane's Trino --------------------------------------------------------
    def opa_server(self, listen: str) -> ThreadingHTTPServer:
        conn = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                kind = self.path.strip("/").split("/")[-1]
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                status, out = 503, {"error": "Cairn's policy decision is not available"}
                if kind in ("allow", "batch", "row-filters", "column-mask"):
                    try:
                        out = conn.signed(f"/dataplanes/tunnel/{conn.connector_id}/opa/{kind}", json.loads(raw or b"{}"))
                        status = 200
                    except Exception as e:  # noqa: BLE001 - fail closed: Trino refuses without a decision
                        logger.warning("OPA %s: no decision from Cairn: %s", kind, e)
                else:
                    status, out = 404, {"error": "unknown"}
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):                     # liveness: up only while Cairn takes this connector
                ok = conn.authorized.is_set()
                data = json.dumps({"ok": ok, "error": None if ok else conn.last_error}).encode()
                self.send_response(200 if ok else 503)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        host, _, port = listen.rpartition(":")
        srv = ThreadingHTTPServer((host or "0.0.0.0", int(port)), H)
        threading.Thread(target=srv.serve_forever, daemon=True, name="dp-opa").start()
        return srv

    # ---- the loop ----------------------------------------------------------------------------------
    def run_forever(self) -> None:
        delay = 1.0
        while not self.stop.is_set():
            try:
                self.poll_once()
                delay = 1.0
                self.last_error = ""
            except GatewayHTTPError as e:
                self.last_error = str(e)
                if e.status == 401:
                    logger.error("Cairn refused this connector: %s", e)
                else:
                    logger.warning("poll: %s", e)
                self.stop.wait(delay)
                delay = min(60.0, delay * 2)
            except (httpx.HTTPError, ConnectorError, OSError) as e:
                self.last_error = str(e)
                logger.warning("the gateway is not reachable: %s", e)
                self.stop.wait(delay)
                delay = min(60.0, delay * 2)


def from_env() -> Connector:
    cert = (_env("CAIRN_DATAPLANE_CLIENT_CERT"), _env("CAIRN_DATAPLANE_CLIENT_KEY"))
    images = [i.strip() for i in _env("CAIRN_DATAPLANE_JOBS_IMAGES").split(",") if i.strip()]
    jobs = {"runtime": _env("CAIRN_DATAPLANE_JOBS_RUNTIME"), "image": _env("CAIRN_DATAPLANE_JOBS_IMAGE") or None,
            "images": images, "network": _env("CAIRN_DATAPLANE_JOBS_NETWORK"),
            "socket": _env("CAIRN_DATAPLANE_DOCKER_SOCKET", "/var/run/docker.sock"),
            "namespace": _env("CAIRN_DATAPLANE_JOBS_NAMESPACE"), "trino_url": _env("CAIRN_DATAPLANE_JOBS_TRINO_URL"),
            "proxy": _env("CAIRN_DATAPLANE_JOBS_PROXY"), "memory_mb": _env("CAIRN_DATAPLANE_JOBS_MEMORY_MB", "2048")}
    storage = {"endpoint": _env("CAIRN_DATAPLANE_STORAGE_ENDPOINT"), "region": _env("CAIRN_DATAPLANE_STORAGE_REGION", "us-east-1"),
               "access_key_file": _env("CAIRN_DATAPLANE_STORAGE_ACCESS_KEY_FILE"),
               "secret_key_file": _env("CAIRN_DATAPLANE_STORAGE_SECRET_KEY_FILE"),
               "buckets": [b.strip() for b in _env("CAIRN_DATAPLANE_STORAGE_BUCKETS").split(",") if b.strip()],
               "max_ttl": _env("CAIRN_DATAPLANE_STORAGE_MAX_TTL", "900")}
    if storage["endpoint"] and not (storage["access_key_file"] and storage["secret_key_file"]):
        raise SystemExit("CAIRN_DATAPLANE_STORAGE_ENDPOINT needs CAIRN_DATAPLANE_STORAGE_ACCESS_KEY_FILE and _SECRET_KEY_FILE")
    gateway = _env("CAIRN_DATAPLANE_URL")
    if not gateway:
        raise SystemExit("CAIRN_DATAPLANE_URL: the Cairn gateway this data plane connects to")
    return Connector(gateway, _env("CAIRN_DATAPLANE_STATE_DIR", "/var/lib/cairn-dataplane"), _env("CAIRN_DATAPLANE_TRINO_URL"),
                     _env("CAIRN_DATAPLANE_CA_FILE") or True, cert if all(cert) else None, jobs, storage,
                     float(_env("CAIRN_DATAPLANE_POLL_SECONDS", "10")))


def main() -> int:
    logging.basicConfig(level=os.environ.get("CAIRN_LOG_LEVEL", "INFO"), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    c = from_env()
    if not c.state.enrolled or not c.state.meta.get("connector_id"):
        path = _env("CAIRN_DATAPLANE_ENROLLMENT_TOKEN_FILE")
        if not path or not os.path.exists(path):
            raise SystemExit("not enrolled: CAIRN_DATAPLANE_ENROLLMENT_TOKEN_FILE (a token from "
                             "POST /v1/dataplanes/<name>/enrollment-tokens)")
        out = c.enroll(_read(path))
        logger.info("enrolled as %s in data plane %s", out["connector_id"], out["dataplane"])
    listen = _env("CAIRN_DATAPLANE_OPA_LISTEN", "0.0.0.0:8181")
    if listen and listen != "off":
        c.opa_server(listen)
    signal.signal(signal.SIGTERM, lambda *_: c.stop.set())
    c.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
