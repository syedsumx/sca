"""Stand-ins for the Databricks Jobs API 2.2, the Snowflake SQL API v2 and BigQuery's REST API (jobs.insert, jobs.get,
getQueryResults, jobs.cancel, and Google's OAuth token endpoint), answering with the documented request and response
shapes, over TLS with a private CA, for tests/test_flows_platforms.py. They check what the real services check:
Snowflake's key-pair JWT (signature, issuer with the public key's fingerprint, subject, lifetime), Google's
service-account assertion (signature, audience, scope), Databricks' OAuth M2M client credentials and personal access
tokens, idempotency tokens, request ids and job ids. There are no emulators for Databricks or Snowflake and no
accounts here, so this is what the request and response handling is tested against."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import json
import os
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID


def tls_files(d: str, hosts=("127.0.0.1", "localhost")) -> tuple[str, str, str]:
    """(ca.pem, server.pem, server-key.pem): a private CA and a server certificate for ``hosts``."""
    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca = (x509.CertificateBuilder().subject_name(name("cairn-test-ca")).issuer_name(name("cairn-test-ca"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(days=2))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = []
    for h in hosts:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(h)))
        except ValueError:
            sans.append(x509.DNSName(h))
    cert = (x509.CertificateBuilder().subject_name(name(hosts[0])).issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=2)).add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    paths = (os.path.join(d, "ca.pem"), os.path.join(d, "server.pem"), os.path.join(d, "server-key.pem"))
    with open(paths[0], "wb") as f:
        f.write(ca.public_bytes(serialization.Encoding.PEM))
    with open(paths[1], "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(paths[2], "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    os.chmod(paths[2], 0o644)
    return paths


def rsa_pem(passphrase: bytes | None = None) -> tuple[str, object]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    enc = serialization.BestAvailableEncryption(passphrase) if passphrase else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc).decode(), key.public_key()


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_jwt(token: str, public_key) -> dict:
    head, body, sig = token.split(".")
    public_key.verify(_b64d(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert json.loads(_b64d(head))["alg"] == "RS256"
    return json.loads(_b64d(body))


class Platforms:
    """One HTTPS server answering as Databricks, Snowflake, BigQuery and Google's token endpoint."""

    def __init__(self, d: str):
        self.ca, cert, key = tls_files(d)
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        # Databricks
        self.pat = "dapi-" + uuid.uuid4().hex
        self.client = ("sp-" + uuid.uuid4().hex[:8], "secret-" + uuid.uuid4().hex)
        self.oauth_tokens: set[str] = set()
        self.runs: dict[int, dict] = {}
        self.idem: dict[str, int] = {}
        self.cancelled: list = []
        # Snowflake: the account's user with its registered public key
        self.sf_key_pem, self.sf_public = rsa_pem(b"open sesame")
        self.statements: dict[str, dict] = {}
        self.request_ids: dict[str, str] = {}
        # Google
        self.sa_pem, self.sa_public = rsa_pem()
        self.google_tokens: set[str] = set()
        self.jobs: dict[str, dict] = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, doc=None):
                raw = json.dumps(doc).encode() if doc is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type") or ""
                if "json" in ctype and raw:
                    return json.loads(raw)
                if "form" in ctype:
                    return {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                return raw.decode()

            def do_GET(self):
                outer.handle(self, "GET", None)

            def do_POST(self):
                outer.handle(self, "POST", self._body())

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.url = f"https://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def sa_json(self) -> str:
        return json.dumps({"type": "service_account", "project_id": "proj", "private_key_id": "k1",
                           "private_key": self.sa_pem, "client_email": "cairn-flows@proj.iam.gserviceaccount.com",
                           "token_uri": f"{self.url}/token"})

    # ---- dispatch
    def handle(self, h, method, body):
        u = urlsplit(h.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        with self.lock:
            self.requests.append({"method": method, "path": u.path, "query": q, "body": body,
                                  "headers": {k.lower(): v for k, v in h.headers.items()}})
        try:
            if u.path.startswith("/oidc/") or u.path.startswith("/api/2.2/"):
                return self.databricks(h, method, u.path, q, body)
            if u.path.startswith("/api/v2/statements"):
                return self.snowflake(h, method, u.path, q, body)
            if u.path == "/token" or u.path.startswith("/bigquery/v2/"):
                return self.bigquery(h, method, u.path, q, body)
            h._send(404, {"message": "no such endpoint"})
        except AssertionError as e:
            h._send(400, {"message": f"bad request: {e}"})

    # ---- Databricks Jobs API 2.2
    def databricks(self, h, method, path, q, body):
        if path == "/oidc/v1/token":
            auth = h.headers.get("Authorization", "")
            assert auth == "Basic " + base64.b64encode(f"{self.client[0]}:{self.client[1]}".encode()).decode(), "client"
            assert body == {"grant_type": "client_credentials", "scope": "all-apis"}, body
            tok = "dbx-oauth-" + uuid.uuid4().hex
            self.oauth_tokens.add(tok)
            return h._send(200, {"access_token": tok, "token_type": "Bearer", "expires_in": 3600})
        auth = h.headers.get("Authorization", "")
        if auth not in (f"Bearer {self.pat}", *[f"Bearer {t}" for t in self.oauth_tokens]):
            return h._send(401, {"error_code": "UNAUTHENTICATED", "message": "Invalid access token."})
        if path in ("/api/2.2/jobs/run-now", "/api/2.2/jobs/runs/submit") and method == "POST":
            tok = body.get("idempotency_token")
            assert tok and len(tok) <= 64, "idempotency_token"
            if tok in self.idem:
                return h._send(200, {"run_id": self.idem[tok]})
            if path.endswith("run-now"):
                assert isinstance(body["job_id"], int), "job_id is an int64"
                assert all(isinstance(v, str) for v in (body.get("job_parameters") or {}).values()), "job_parameters"
                if body["job_id"] == 404:
                    return h._send(400, {"error_code": "INVALID_PARAMETER_VALUE", "message": "Job 404 does not exist."})
                job = body["job_id"]
            else:
                assert isinstance(body.get("tasks"), list) and body.get("run_name"), "runs/submit body"
                job = None
            rid = 1000 + len(self.runs)
            self.idem[tok] = rid
            self.runs[rid] = {"job_id": job, "body": body, "looks": 0, "cancelled": False}
            return h._send(200, {"run_id": rid, "number_in_job": rid})
        if path == "/api/2.2/jobs/runs/get" and method == "GET":
            rid = int(q["run_id"])
            run = self.runs.get(rid)
            if run is None:
                return h._send(400, {"error_code": "RESOURCE_DOES_NOT_EXIST", "message": f"Run {rid} does not exist."})
            run["looks"] += 1
            doc = {"job_id": run["job_id"], "run_id": rid, "run_page_url": f"{self.url}/?o=1#job/{run['job_id']}/run/{rid}",
                   "tasks": [{"task_key": "main", "state": {"life_cycle_state": "RUNNING"}}]}
            if run["cancelled"]:
                doc.update(state={"life_cycle_state": "TERMINATED", "result_state": "CANCELED",
                                  "state_message": "Run cancelled."},
                           status={"state": "TERMINATED", "termination_details": {"code": "CANCELED", "type": "SUCCESS",
                                                                                 "message": "Run cancelled."}})
            elif run["job_id"] == 777 or run["looks"] < 2:      # 777 runs until it is cancelled
                doc.update(state={"life_cycle_state": "RUNNING", "state_message": ""}, status={"state": "RUNNING"})
            elif run["job_id"] == 666:
                doc.update(state={"life_cycle_state": "TERMINATED", "result_state": "FAILED",
                                  "state_message": "Task main failed with message: division by zero."},
                           status={"state": "TERMINATED", "termination_details": {
                               "code": "RUN_EXECUTION_ERROR", "type": "CLIENT_ERROR",
                               "message": "Task main failed with message: division by zero."}})
            else:
                doc.update(state={"life_cycle_state": "TERMINATED", "result_state": "SUCCESS", "state_message": ""},
                           status={"state": "TERMINATED", "termination_details": {"code": "SUCCESS", "type": "SUCCESS",
                                                                                 "message": ""}},
                           run_duration=4200,
                           tasks=[{"task_key": "main", "state": {"life_cycle_state": "TERMINATED", "result_state": "SUCCESS"},
                                   "status": {"state": "TERMINATED", "termination_details": {"code": "SUCCESS"}}}])
            return h._send(200, doc)
        if path == "/api/2.2/jobs/runs/cancel" and method == "POST":
            rid = int(body["run_id"])
            self.runs[rid]["cancelled"] = True
            self.cancelled.append(("databricks", rid))
            return h._send(200, {})
        h._send(404, {"error_code": "ENDPOINT_NOT_FOUND", "message": path})

    # ---- Snowflake SQL API v2
    def _sf_auth(self, h):
        assert h.headers.get("X-Snowflake-Authorization-Token-Type") == "KEYPAIR_JWT", "token type"
        auth = h.headers.get("Authorization", "")
        assert auth.startswith("Bearer "), "bearer"
        claims = verify_jwt(auth[7:], self.sf_public)
        der = self.sf_public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        fp = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
        assert claims["iss"] == f"MYORG-ACCT.CAIRN_FLOWS.{fp}", claims["iss"]
        assert claims["sub"] == "MYORG-ACCT.CAIRN_FLOWS", claims["sub"]
        assert claims["exp"] - claims["iat"] <= 3600 and claims["iat"] <= time.time() + 60, "lifetime"

    def snowflake(self, h, method, path, q, body):
        try:
            self._sf_auth(h)
        except Exception as e:  # noqa: BLE001 - what Snowflake answers to a bad JWT
            return h._send(401, {"code": "390144", "message": f"JWT token is invalid. ({e})"})
        if path == "/api/v2/statements" and method == "POST":
            assert q.get("async") == "true" and q.get("requestId"), "async with a requestId"
            uuid.UUID(q["requestId"])
            if q["requestId"] in self.request_ids:
                hd = self.request_ids[q["requestId"]]
            else:
                hd = str(uuid.uuid4())
                self.request_ids[q["requestId"]] = hd
                self.statements[hd] = {"body": body, "looks": 0, "cancelled": False}
            return h._send(202, {"code": "333334", "message": "Asynchronous execution in progress. Use provided query id "
                                 "to perform query monitoring and management.", "statementHandle": hd,
                                 "statementStatusUrl": f"/api/v2/statements/{hd}"})
        parts = path.split("/")
        hd = parts[4] if len(parts) > 4 else ""
        st = self.statements.get(hd)
        if st is None:
            return h._send(404, {"code": "000709", "message": f"Statement {hd} not found"})
        if path.endswith("/cancel") and method == "POST":
            st["cancelled"] = True
            self.cancelled.append(("snowflake", hd))
            return h._send(200, {"code": "000604", "sqlState": "57014", "message": "SQL execution canceled",
                                 "statementHandle": hd})
        st["looks"] += 1
        text = st["body"]["statement"]
        if st["cancelled"]:
            return h._send(422, {"code": "000604", "sqlState": "57014", "message": "SQL execution canceled",
                                 "statementHandle": hd})
        if "SLEEP" in text or st["looks"] < 2:
            return h._send(202, {"code": "333334", "message": "Asynchronous execution in progress.", "statementHandle": hd,
                                 "statementStatusUrl": f"/api/v2/statements/{hd}"})
        if "MISSING_TABLE" in text:
            return h._send(422, {"code": "002003", "sqlState": "42S02", "statementHandle": hd,
                                 "message": "SQL compilation error:\nObject 'MISSING_TABLE' does not exist or not authorized."})
        if text.startswith("EXECUTE TASK"):
            return h._send(200, {"resultSetMetaData": {"numRows": 1, "format": "jsonv2", "rowType": [
                {"name": "status", "type": "text", "nullable": True}]}, "data": [["Task executed"]],
                "code": "090001", "statementHandle": hd, "message": "Statement executed successfully."})
        return h._send(200, {"resultSetMetaData": {"numRows": 2, "format": "jsonv2", "rowType": [
            {"name": "REGION", "type": "text", "nullable": True}, {"name": "N", "type": "fixed", "nullable": False}],
            "partitionInfo": [{"rowCount": 2, "uncompressedSize": 30}]},
            "data": [["EU", "3"], ["US", "1"]], "code": "090001", "statementHandle": hd,
            "message": "Statement executed successfully.", "createdOn": int(time.time() * 1000)})

    # ---- Google OAuth and BigQuery
    def bigquery(self, h, method, path, q, body):
        if path == "/token":
            assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer", "grant_type"
            head = json.loads(_b64d(body["assertion"].split(".")[0]))
            assert head.get("kid") == "k1", "kid"
            claims = verify_jwt(body["assertion"], self.sa_public)
            assert claims["aud"] == f"{self.url}/token" and claims["iss"].endswith(".iam.gserviceaccount.com"), claims
            assert claims["scope"] == "https://www.googleapis.com/auth/bigquery", claims["scope"]
            tok = "ya29." + uuid.uuid4().hex
            self.google_tokens.add(tok)
            return h._send(200, {"access_token": tok, "expires_in": 3599, "token_type": "Bearer"})
        if h.headers.get("Authorization", "")[7:] not in self.google_tokens:
            return h._send(401, {"error": {"code": 401, "message": "Request had invalid authentication credentials.",
                                           "status": "UNAUTHENTICATED"}})
        parts = path.split("/")                       # /bigquery/v2/projects/<p>/jobs[/<id>[/cancel]] or /queries/<id>
        project = parts[4]
        if parts[5] == "jobs" and len(parts) == 6 and method == "POST":
            ref = body["jobReference"]
            assert ref["projectId"] == project and ref["jobId"], "jobReference"
            assert body["configuration"]["query"]["useLegacySql"] is False, "standard SQL"
            assert all(k == k.lower() for k in body["configuration"].get("labels", {})), "labels are lower case"
            if ref["jobId"] in self.jobs:
                return h._send(409, {"error": {"code": 409, "message": f"Already Exists: Job {project}:{ref['jobId']}",
                                               "status": "ALREADY_EXISTS"}})
            self.jobs[ref["jobId"]] = {"body": body, "looks": 0, "cancelled": False}
            return h._send(200, {"kind": "bigquery#job", "jobReference": {**ref, "location": ref.get("location", "US")},
                                 "configuration": body["configuration"], "status": {"state": "RUNNING"}})
        job_id = parts[6]
        job = self.jobs.get(job_id)
        if job is None:
            return h._send(404, {"error": {"code": 404, "message": f"Not found: Job {project}:{job_id}",
                                           "status": "NOT_FOUND"}})
        query = job["body"]["configuration"]["query"]["query"]
        if parts[5] == "jobs" and len(parts) == 8 and parts[7] == "cancel":
            job["cancelled"] = True
            self.cancelled.append(("bigquery", job_id))
            return h._send(200, {"kind": "bigquery#jobCancelResponse", "job": {"status": {"state": "RUNNING"}}})
        ref = {"projectId": project, "jobId": job_id, "location": "EU"}
        if parts[5] == "jobs":
            job["looks"] += 1
            if job["cancelled"]:
                return h._send(200, {"jobReference": ref, "status": {"state": "DONE", "errorResult": {
                    "reason": "stopped", "message": "Job execution was cancelled: User requested cancellation"}}})
            if "SLEEP" in query or job["looks"] < 2:
                return h._send(200, {"jobReference": ref, "status": {"state": "RUNNING"}})
            if "missing" in query:
                return h._send(200, {"jobReference": ref, "status": {"state": "DONE", "errorResult": {
                    "reason": "notFound", "location": "missing", "message": "Not found: Table proj:ds.missing was not "
                    "found in location EU"}, "errors": [{"reason": "notFound"}]}})
            return h._send(200, {"jobReference": ref, "status": {"state": "DONE"},
                                 "statistics": {"query": {"totalBytesProcessed": "1024"}}})
        if parts[5] == "queries":
            assert q.get("maxResults") == "20", "maxResults"
            if "BADRESULT" in query:               # the job is DONE, the query fails when its results are read
                return h._send(400, {"error": {"code": 400, "status": "INVALID_ARGUMENT",
                                               "message": "Function not found: badresult at [1:8]"}})
            return h._send(200, {"kind": "bigquery#getQueryResultsResponse", "jobReference": ref, "jobComplete": True,
                                 "totalRows": "2", "schema": {"fields": [{"name": "region", "type": "STRING"},
                                                                         {"name": "n", "type": "INTEGER"}]},
                                 "rows": [{"f": [{"v": "EU"}, {"v": "3"}]}, {"f": [{"v": "US"}, {"v": "1"}]}]})
        h._send(404, {"error": {"code": 404, "message": path}})
