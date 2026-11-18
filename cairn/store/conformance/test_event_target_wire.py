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

    def __init__(self, answer, port=None):
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

        self.port = port or _free_port()
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


def rs256_claims(token: str, pub, audience: str) -> dict:
    """The claims of an RS256 JWT after checking its signature and audience (cryptography only: the store jobs have
    no PyJWT)."""
    import base64

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def b64(part: str) -> bytes:
        return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))

    head, body, sig = token.split(".")
    assert json.loads(b64(head))["alg"] == "RS256"
    pub.verify(b64(sig), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    claims = json.loads(b64(body))
    assert claims["aud"] == audience
    return claims


def test_pubsub_service_account_token_has_a_pubsub_scope(custom_store, tmp_path):
    from urllib.parse import parse_qs

    tokens = {}

    def token(h, body):
        claims = rs256_claims(parse_qs(body.decode())["assertion"][0], pub, token_uri)
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


# --- A target that answers an error slowly must not hold up the other targets ------------------------------

def trickle(h, body):
    """An error answer whose body never ends: a chunk every half second."""
    h.send_response(500)
    h.send_header("transfer-encoding", "chunked")
    h.end_headers()
    end = time.time() + 120
    try:
        while time.time() < end:
            h.wfile.write(b"1\r\nx\r\n")
            h.wfile.flush()
            time.sleep(0.5)
    except OSError:
        pass
    h.close_connection = True


def test_a_slow_error_body_does_not_stall_delivery_to_other_targets(custom_store, tmp_path):
    slow, hook = Recorder(trickle), Recorder(lambda h, b: (200, b""))
    try:
        service_account_key(tmp_path / "sa.json", f"http://127.0.0.1:{slow.port}/token")
        st = store_with(custom_store, tmp_path, [
            {"id": "pst", "type": "pubsub", "topic": "projects/p/topics/t", "endpoint": f"http://127.0.0.1:{hook.port}",
             "credentials_file": str(tmp_path / "sa.json")},
            {"id": "q", "type": "sqs", "queue_url": f"http://127.0.0.1:{slow.port}/1/q", "region": "r", "access_key": "a",
             "secret_key": "s"},
            {"id": "ps", "type": "pubsub", "topic": "projects/p/topics/t", "endpoint": f"http://127.0.0.1:{slow.port}",
             "auth": "none"},
            {"id": "w", "type": "webhook", "url": f"http://127.0.0.1:{hook.port}/hook", "secret": "x" * 32}])
        s3 = st.client()
        for b, t in (("slow-sqs", "q"), ("slow-ps", "ps"), ("slow-token", "pst"), ("fast", "w")):
            s3.create_bucket(Bucket=b)
            notify(s3, b, t)
        s3.put_object(Bucket="slow-sqs", Key="a", Body=b"1")
        s3.put_object(Bucket="slow-ps", Key="b", Body=b"1")
        s3.put_object(Bucket="slow-token", Key="b", Body=b"1")
        wait_for(lambda: len(slow.requests) >= 1, what="the first attempt")
        s3.put_object(Bucket="fast", Key="c", Body=b"1")
        # Each stalled target costs a couple of seconds at most, not the whole delivery loop.
        wait_for(lambda: [r for r in hook.requests if r[1] == "/hook"], timeout=25,
                 what="the webhook event, behind three stalled targets")
        assert {r[1] for r in slow.requests} == {"/1/q", "/v1/projects/p/topics/t:publish", "/token"}  # each was tried
    finally:
        slow.close()
        hook.close()


# --- NATS: a server's lines are read with a limit ------------------------------------------------------------

def test_nats_server_lines_without_end_are_not_read_without_limit(custom_store, tmp_path):
    """A server that sends a line that never ends (in its INFO, or instead of PONG) is dropped early, not
    buffered until the time limit: on loopback that is hundreds of megabytes of store memory."""
    cap = 256 << 20
    sent = {}
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def serve():
        n = 0
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            # The first connection never ends its INFO line; the next ones send a good INFO, then an endless line.
            mode = "info" if n == 0 else "reply"
            n += 1
            total = 0
            try:
                if mode == "info":
                    conn.sendall(b'INFO {"server_id":"x","headers":true,"max_payload":1048576,"junk":"')
                else:
                    conn.sendall(b'INFO {"server_id":"x","headers":true,"max_payload":1048576}\r\n')
                    conn.settimeout(5)
                    conn.recv(65536)  # CONNECT, HPUB, PING
                    conn.sendall(b"-ERR '")
                chunk = b"x" * (1 << 20)
                conn.settimeout(15)
                while total < cap:
                    conn.sendall(chunk)
                    total += len(chunk)
            except OSError:
                pass
            sent.setdefault(mode, total)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        st = store_with(custom_store, tmp_path, [{"id": "n", "type": "nats", "url": f"nats://127.0.0.1:{port}", "subject": "s"}])
        s3 = st.client()
        s3.create_bucket(Bucket="natsy")
        notify(s3, "natsy", "n")
        s3.put_object(Bucket="natsy", Key="k", Body=b"1")
        wait_for(lambda: len(sent) == 2, timeout=60, what="two attempts")
        assert sent["info"] < 16 << 20, sent
        assert sent["reply"] < 16 << 20, sent
    finally:
        srv.close()


def test_nats_publish_on_the_wire(custom_store, tmp_path):
    """CONNECT with the token, HPUB with Nats-Msg-Id, then PING; delivered once PONG comes back."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]
    got = []

    def serve():
        conn, _ = srv.accept()
        conn.sendall(b'INFO {"server_id":"x","headers":true,"auth_required":true,"max_payload":1048576}\r\n')
        data = b""
        conn.settimeout(10)
        while not data.endswith(b"PING\r\n"):
            data += conn.recv(65536)
        got.append(data)
        conn.sendall(b"+OK\r\nPONG\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        st = store_with(custom_store, tmp_path, [{"id": "n", "type": "nats", "url": f"nats://127.0.0.1:{port}",
                                                  "subject": "cairn.lake", "token": "tok-123"}])
        s3 = st.client()
        s3.create_bucket(Bucket="wire")
        notify(s3, "wire", "n")
        s3.put_object(Bucket="wire", Key="k", Body=b"1")
        wait_for(lambda: "cairn_store_events_delivered_total 1" in requests.get(f"{st.endpoint}/-/metrics", timeout=10).text,
                 what="delivery")
        lines = got[0].split(b"\r\n")
        assert json.loads(lines[0].removeprefix(b"CONNECT "))["auth_token"] == "tok-123"
        assert lines[1].startswith(b"HPUB cairn.lake ") and lines[2] == b"NATS/1.0" and lines[3].startswith(b"Nats-Msg-Id: ")
        assert json.loads(lines[5])["Records"][0]["s3"]["bucket"]["name"] == "wire"
    finally:
        srv.close()


# --- Pub/Sub: a long key must not wedge the topic --------------------------------------------------------------

def test_pubsub_long_keys_fit_the_attribute_limit(custom_store, tmp_path):
    """Pub/Sub refuses attribute values over 1024 bytes; <bucket>/<key> can be up to 1088. Such an event
    must still be published, or it holds up every later event for that topic (they go in order)."""
    published = []

    def publish(h, body):
        msgs = json.loads(body)["messages"]
        for m in msgs:
            if any(len(v.encode()) > 1024 for v in m.get("attributes", {}).values()):
                return 400, b'{"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "attribute value too long"}}'
        published.extend(msgs)
        return 200, b'{"messageIds": ["1"]}'

    ps = Recorder(publish)
    try:
        st = store_with(custom_store, tmp_path, [{"id": "ps", "type": "pubsub", "topic": "projects/p/topics/t",
                                                  "endpoint": f"http://127.0.0.1:{ps.port}", "auth": "none"}])
        s3 = st.client()
        bucket = "long-keys-" + "b" * 40
        s3.create_bucket(Bucket=bucket)
        notify(s3, bucket, "ps")
        long_key = "k" * 1000
        s3.put_object(Bucket=bucket, Key=long_key, Body=b"1")
        s3.put_object(Bucket=bucket, Key="short", Body=b"1")
        wait_for(lambda: len(published) == 2, timeout=20, what="both events")
        rec = [json.loads(base64.b64decode(m["data"]))["Records"][0] for m in published]
        assert [r["s3"]["object"]["key"] for r in rec] == [long_key, "short"]
        assert published[1]["attributes"]["key"] == f"{bucket}/short"
        assert all(m["attributes"]["x-cairn-event-id"] for m in published)
    finally:
        ps.close()


# --- A target that is down with many events waiting must not hold up the others ------------------------------

def test_a_down_target_with_many_waiting_events_does_not_hold_up_others(custom_store, tmp_path):
    from urllib.parse import parse_qs

    hook = Recorder(lambda h, b: (200, b""))
    dead = _free_port()
    sqs = None
    try:
        st = store_with(custom_store, tmp_path, [
            {"id": "q", "type": "sqs", "queue_url": f"http://127.0.0.1:{dead}/1/q", "region": "r", "access_key": "a",
             "secret_key": "s"},
            {"id": "w", "type": "webhook", "url": f"http://127.0.0.1:{hook.port}/hook", "secret": "x" * 32}])
        s3 = st.client()
        for b, t in (("down", "q"), ("fast", "w")):
            s3.create_bucket(Bucket=b)
            notify(s3, b, t)
        n = 1200  # more than a page of the outbox
        for i in range(n):
            s3.put_object(Bucket="down", Key=f"k{i:05}", Body=b"1")
        s3.put_object(Bucket="fast", Key="c", Body=b"1")
        wait_for(lambda: hook.requests, timeout=20, what="the webhook event, behind the down queue's events")
        # The queue comes back: its events arrive, all of them and in order.
        sqs = Recorder(lambda h, b: (200, b"<SendMessageResponse/>"), port=dead)
        wait_for(lambda: len(sqs.requests) >= n, timeout=60, what="the queue's events")
        keys = [json.loads(parse_qs(r[3].decode())["MessageBody"][0])["Records"][0]["s3"]["object"]["key"] for r in sqs.requests]
        assert keys == [f"k{i:05}" for i in range(n)]
    finally:
        hook.close()
        if sqs:
            sqs.close()


# --- SQS: what a bucket's rule puts in the event must not make the queue refuse it --------------------------

SQS_MAX = 262_144


def sqs_like(h, body):
    """Answers as SQS does: a body over 256 KiB, or with characters outside SQS's set, is refused."""
    from urllib.parse import parse_qs

    msg = parse_qs(body.decode())["MessageBody"][0]
    def ok(c):
        return c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF or 0xE000 <= ord(c) <= 0xFFFD or ord(c) >= 0x10000
    if len(msg.encode()) > SQS_MAX:
        return 400, b"<ErrorResponse><Error><Code>InvalidParameterValue</Code></Error></ErrorResponse>"
    if not all(ok(c) for c in msg):
        return 400, b"<ErrorResponse><Error><Code>InvalidMessageContents</Code></Error></ErrorResponse>"
    return 200, b"<SendMessageResponse/>"


def sqs_records(sqs):
    """The events SQS took (the ones it answered 200)."""
    from urllib.parse import parse_qs

    out = []
    for r in sqs.requests:
        if sqs_like(None, r[3])[0] == 200:
            out.append(json.loads(parse_qs(r[3].decode())["MessageBody"][0])["Records"][0])
    return out


def put_rule(s3, bucket, target, rule_id):
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration={"QueueConfigurations": [
        {"Id": rule_id, "QueueArn": f"arn:cairn:store:::{target}", "Events": ["s3:ObjectCreated:*"]}]})


def test_sqs_rule_ids_that_sqs_would_refuse_do_not_stop_the_queue(custom_store, tmp_path):
    sqs = Recorder(sqs_like)
    try:
        st = store_with(custom_store, tmp_path, [{"id": "q", "type": "sqs", "queue_url": f"http://127.0.0.1:{sqs.port}/1/q",
                                                  "region": "r", "access_key": "a", "secret_key": "s"}])
        s3 = st.client()
        for b in ("odd", "next"):
            s3.create_bucket(Bucket=b)
        # A character SQS refuses in a message (U+FFFF) is escaped in the event's JSON.
        put_rule(s3, "odd", "q", "a\uffffb")
        notify(s3, "next", "q")
        s3.put_object(Bucket="odd", Key="k", Body=b"1")
        s3.put_object(Bucket="next", Key="k", Body=b"1")
        recs = wait_for(lambda: len(sqs_records(sqs)) >= 2 and sqs_records(sqs), timeout=20, what="both events")
        assert [r["s3"]["bucket"]["name"] for r in recs] == ["odd", "next"]
        assert recs[0]["s3"]["configurationId"] == "a\uffffb"
    finally:
        sqs.close()


def test_a_rule_id_too_long_for_an_event_is_refused(custom_store, tmp_path):
    from botocore.exceptions import ClientError

    sqs = Recorder(sqs_like)
    try:
        st = store_with(custom_store, tmp_path, [{"id": "q", "type": "sqs", "queue_url": f"http://127.0.0.1:{sqs.port}/1/q",
                                                  "region": "r", "access_key": "a", "secret_key": "s"}])
        s3 = st.client()
        for b in ("big", "next"):
            s3.create_bucket(Bucket=b)
        # A 200,000-character id makes an event over SQS's 256 KiB limit (each '"' is escaped in JSON).
        try:
            put_rule(s3, "big", "q", '"' * 200_000)
            refused = False
        except ClientError as err:
            refused = err.response["Error"]["Code"] == "InvalidArgument"
        notify(s3, "next", "q")
        s3.put_object(Bucket="big", Key="k", Body=b"1")
        s3.put_object(Bucket="next", Key="k", Body=b"1")
        wait_for(lambda: any(r["s3"]["bucket"]["name"] == "next" for r in sqs_records(sqs)), timeout=20,
                 what="the next bucket's event")
        assert refused
        put_rule(s3, "big", "q", "r" * 255)  # as long as lifecycle rule ids may be
    finally:
        sqs.close()
