"""Event targets against stand-in servers (no docker): what the store sends to SQS, Pub/Sub and NATS on the
wire, and how it copes with servers that misbehave. Real-server checks are in test_event_targets.py."""

import base64
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from conftest import _free_port


def wait_for(fn, timeout=30, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


def notify(s3, bucket, target):
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration={"QueueConfigurations": [
        {"Id": f"to-{target}", "QueueArn": f"arn:cairn:store:::{target}", "Events": ["s3:ObjectCreated:*"]}]})


def store_with(custom_store, tmp_path, targets):
    f = tmp_path / "targets.json"
    f.write_text(json.dumps(targets))
    return custom_store({"CAIRN_STORE_EVENT_TARGETS_FILE": str(f)})


class Recorder:
    """A threaded HTTP server; `answer(handler, body)` returns (status, body) or handles the response itself
    (returning None)."""

    def __init__(self, answer):
        self.requests, self.answer = [], answer
        rec = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)))
                rec.requests.append((self.command, self.path, dict(self.headers), body))
                out = rec.answer(self, body)
                if out is not None:
                    status, data = out
                    self.send_response(status)
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.port = _free_port()
        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


# --- SQS: SigV4 for the sqs service ------------------------------------------------------------------------

def test_sqs_send_message_carries_a_valid_sigv4_signature(custom_store, tmp_path):
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    sqs = Recorder(lambda h, b: (200, b"<SendMessageResponse/>"))
    try:
        url = f"http://127.0.0.1:{sqs.port}/000000000000/lake.fifo"
        st = store_with(custom_store, tmp_path, [{"id": "q", "type": "sqs", "queue_url": url, "region": "eu-west-1",
                                                  "access_key": "AKIDEXAMPLE", "secret_key": "s3cr3t/key+x"}])
        s3 = st.client()
        s3.create_bucket(Bucket="sig")
        notify(s3, "sig", "q")
        s3.put_object(Bucket="sig", Key="a b+c.txt", Body=b"1")
        wait_for(lambda: sqs.requests, what="SendMessage")
        method, path, headers, body = sqs.requests[0]
        hdr = {k.lower(): v for k, v in headers.items()}
        auth = hdr["authorization"]
        assert "/eu-west-1/sqs/aws4_request" in auth
        signed = auth.split("SignedHeaders=")[1].split(",")[0].split(";")
        # Recompute the signature as AWS does, from what arrived.
        req = AWSRequest(method=method, url=f"http://127.0.0.1:{sqs.port}{path}", data=body,
                         headers={k: hdr[k] for k in signed})
        req.context["timestamp"] = hdr["x-amz-date"]
        signer = SigV4Auth(Credentials("AKIDEXAMPLE", "s3cr3t/key+x"), "sqs", "eu-west-1")
        signer._canonical_headers = signer.canonical_headers
        canonical = signer.canonical_request(req)
        assert auth.endswith("Signature=" + signer.signature(signer.string_to_sign(req, canonical), req))
        from urllib.parse import parse_qs
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        assert form["Action"] == "SendMessage" and form["MessageGroupId"] == "sig"
        assert json.loads(form["MessageBody"])["Records"][0]["s3"]["object"]["key"] == "a%20b%2Bc.txt"
    finally:
        sqs.close()


# --- Pub/Sub: the service account's token must be good for Pub/Sub -----------------------------------------

def service_account_key(path, token_uri):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    path.write_text(json.dumps({"type": "service_account", "client_email": "events@p.iam.gserviceaccount.com",
                                "private_key": pem.decode(), "token_uri": token_uri}))
    return key.public_key()


def test_pubsub_service_account_token_has_a_pubsub_scope(custom_store, tmp_path):
    import jwt
    from urllib.parse import parse_qs

    tokens = {}

    def token(h, body):
        claims = jwt.decode(parse_qs(body.decode())["assertion"][0], pub, algorithms=["RS256"], audience=token_uri)
        t = f"tok-{len(tokens)}"
        tokens[t] = claims["scope"].split()
        return 200, json.dumps({"access_token": t, "expires_in": 3600, "token_type": "Bearer"}).encode()

    def publish(h, body):
        # As Pub/Sub does: a token without a Pub/Sub scope is refused.
        scopes = tokens.get(h.headers.get("authorization", "").removeprefix("Bearer "), [])
        if not {"https://www.googleapis.com/auth/pubsub", "https://www.googleapis.com/auth/cloud-platform"} & set(scopes):
            return 403, b'{"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "insufficient scopes"}}'
        return 200, b'{"messageIds": ["1"]}'

    tok, ps = Recorder(token), Recorder(publish)
    try:
        token_uri = f"http://127.0.0.1:{tok.port}/token"
        pub = service_account_key(tmp_path / "sa.json", token_uri)
        st = store_with(custom_store, tmp_path, [{"id": "ps", "type": "pubsub", "topic": "projects/p/topics/t",
                                                  "endpoint": f"http://127.0.0.1:{ps.port}", "auth": "service_account",
                                                  "credentials_file": str(tmp_path / "sa.json")}])
        s3 = st.client()
        s3.create_bucket(Bucket="scoped")
        notify(s3, "scoped", "ps")
        s3.put_object(Bucket="scoped", Key="k", Body=b"1")
        metrics = f"{st.endpoint}/-/metrics"

        def attempted():
            m = requests.get(metrics, timeout=10).text
            return "cairn_store_events_delivered_total 1" in m or "cairn_store_events_failed_total 0" not in m

        wait_for(attempted, what="a publish attempt")
        assert "cairn_store_events_delivered_total 1" in requests.get(metrics, timeout=10).text, tokens
        assert ps.requests[-1][1] == "/v1/projects/p/topics/t:publish"
    finally:
        tok.close()
        ps.close()
