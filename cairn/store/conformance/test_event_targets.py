"""Event notifications to Amazon SQS (against ElasticMQ), Google Cloud Pub/Sub (against its emulator) and
NATS (a real server, with and without a token, and over TLS): the S3 event record arrives, events
queued while a target is down are delivered in order once it is back, FIFO queues get a group and a
deduplication id, NATS messages carry Nats-Msg-Id, and target definitions that do not fit their type are
refused at startup. Needs docker."""

import base64
import json
import os
import shutil
import socket
import subprocess
import time

import pytest
import requests

from conftest import Store, _free_port

ELASTICMQ = "softwaremill/elasticmq-native:1.6.9"
PUBSUB = "thekevjames/gcloud-pubsub-emulator@sha256:0c9f363655f9d09cb464290bdde93f3cc8bdf4168e488a8ffcd80e51cd994860"
NATS = "nats:2.10.22-alpine"


def docker_ok():
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0


pytestmark = pytest.mark.skipif(not docker_ok(), reason="needs docker (ElasticMQ, Pub/Sub emulator, NATS)")


def run(image, port, inner, *args, env=(), volumes=()):
    name = f"cairn-ev-{os.getpid()}-{port}"
    cmd = ["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{port}:{inner}"]
    for e in env:
        cmd += ["-e", e]
    for v in volumes:
        cmd += ["-v", v]
    subprocess.run([*cmd, image, *args], check=True, capture_output=True, timeout=600)
    return name


def stop(name):
    subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60)


def wait_port(port, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 1).close()
            return
        except OSError:
            time.sleep(0.2)
    raise AssertionError(f"port {port} never opened")


def wait_for(fn, timeout=30, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.3)
    raise AssertionError(f"timed out waiting for {what}")


def notify(s3, bucket, target, prefix=""):
    cfg = {"Id": f"to-{target}", "QueueArn": f"arn:cairn:store:::{target}", "Events": ["s3:ObjectCreated:*", "s3:ObjectRemoved:*"]}
    if prefix:
        cfg["Filter"] = {"Key": {"FilterRules": [{"Name": "prefix", "Value": prefix}]}}
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration={"QueueConfigurations": [cfg]})


def store_with(custom_store, tmp_path, targets):
    f = tmp_path / "targets.json"
    f.write_text(json.dumps(targets))
    return custom_store({"CAIRN_STORE_EVENT_TARGETS_FILE": str(f)})


# --- SQS --------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sqs():
    port = _free_port()
    name = run(ELASTICMQ, port, 9324)
    try:
        wait_port(port)
        import boto3

        c = boto3.client("sqs", endpoint_url=f"http://127.0.0.1:{port}", region_name="elasticmq", aws_access_key_id="x",
                         aws_secret_access_key="x")
        wait_for(lambda: _try(c.list_queues), what="ElasticMQ")
        yield c, port, name
    finally:
        stop(name)


def _try(fn):
    try:
        fn()
        return True
    except Exception:
        return False


def host_url(url, port):
    """ElasticMQ reports its in-container address; the store reaches it on the mapped port."""
    return f"http://127.0.0.1:{port}/" + url.split("/", 3)[3]


def receive_all(c, url, want, timeout=30):
    got = []
    end = time.time() + timeout
    while len(got) < want and time.time() < end:
        r = c.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1, MessageAttributeNames=["All"],
                              AttributeNames=["All"])
        for m in r.get("Messages", []):
            got.append(m)
            c.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
    return got


def test_sqs_standard_and_fifo_queues(sqs, custom_store, tmp_path):
    c, port, _ = sqs
    url = c.create_queue(QueueName="lake-events")["QueueUrl"]
    fifo = c.create_queue(QueueName="lake-ordered.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
    base = {"type": "sqs", "region": "elasticmq", "access_key": "AKIDCAIRN", "secret_key": "cairn-sqs-secret"}
    st = store_with(custom_store, tmp_path, [{"id": "q", "queue_url": host_url(url, port), **base},
                                             {"id": "qf", "queue_url": host_url(fifo, port), **base}])
    s3 = st.client()
    s3.create_bucket(Bucket="raw")
    s3.create_bucket(Bucket="ordered")
    notify(s3, "raw", "q")
    notify(s3, "ordered", "qf")
    s3.put_object(Bucket="raw", Key="a b/1.csv", Body=b"x,y\n")
    s3.delete_object(Bucket="raw", Key="a b/1.csv")
    msgs = receive_all(c, url, 2)
    recs = [json.loads(m["Body"])["Records"][0] for m in msgs]
    want = [("ObjectCreated:Put", "a%20b/1.csv"), ("ObjectRemoved:Delete", "a%20b/1.csv")]
    assert [(r["eventName"], r["s3"]["object"]["key"]) for r in recs] == want
    assert recs[0]["s3"]["bucket"]["name"] == "raw" and recs[0]["s3"]["object"]["size"] == 4
    assert all(m["MessageAttributes"]["x-cairn-event-id"]["StringValue"].isdigit() for m in msgs)
    for i in range(3):
        s3.put_object(Bucket="ordered", Key=f"k{i}", Body=b"v")
    msgs = receive_all(c, fifo, 3)
    assert [json.loads(m["Body"])["Records"][0]["s3"]["object"]["key"] for m in msgs] == ["k0", "k1", "k2"]
    assert {m["Attributes"]["MessageGroupId"] for m in msgs} == {"ordered"}
    assert len({m["Attributes"]["MessageDeduplicationId"] for m in msgs}) == 3


def test_sqs_down_then_back_keeps_order(sqs, custom_store, tmp_path):
    c, port, name = sqs
    # A FIFO queue: a standard queue keeps no order of its own, so only a FIFO queue shows the store's.
    url = c.create_queue(QueueName="outage.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
    st = store_with(custom_store, tmp_path, [{"id": "q", "type": "sqs", "queue_url": host_url(url, port), "region": "elasticmq",
                                              "access_key": "AKIDCAIRN", "secret_key": "cairn-sqs-secret"}])
    s3 = st.client()
    s3.create_bucket(Bucket="outage")
    notify(s3, "outage", "q")
    subprocess.run(["docker", "pause", name], check=True, capture_output=True)
    try:
        for i in range(5):
            s3.put_object(Bucket="outage", Key=f"o{i}", Body=b"v")
        # A paused queue does not answer: the attempt times out, is counted, and is retried later.
        wait_for(lambda: "cairn_store_events_failed_total 0" not in requests.get(f"{st.endpoint}/-/metrics", timeout=10).text, 40,
                 "a failed attempt")
        assert "cairn_store_events_pending 5" in requests.get(f"{st.endpoint}/-/metrics", timeout=10).text
    finally:
        subprocess.run(["docker", "unpause", name], check=True, capture_output=True)
    # At least once: a send that timed out during the outage may still land, so consumers deduplicate on
    # the event id. Deduplicated, every event is there, in order.
    seen, keys = set(), []
    end = time.time() + 60
    while len(keys) < 5 and time.time() < end:
        for m in receive_all(c, url, 1, timeout=5):
            eid = m["MessageAttributes"]["x-cairn-event-id"]["StringValue"]
            if eid not in seen:
                seen.add(eid)
                keys.append((int(eid), json.loads(m["Body"])["Records"][0]["s3"]["object"]["key"]))
    assert [k for _, k in keys] == [f"o{i}" for i in range(5)] and keys == sorted(keys)


# --- Pub/Sub ----------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def pubsub():
    port = _free_port()
    name = run(PUBSUB, port, 8681, env=["PUBSUB_PROJECT1=cairn-test,lake-events:lake-sub"])
    try:
        wait_port(port, timeout=180)
        base = f"http://127.0.0.1:{port}"
        topics = f"{base}/v1/projects/cairn-test/topics"
        wait_for(lambda: _try(lambda: requests.get(topics, timeout=2).raise_for_status()), 180, "Pub/Sub emulator")
        wait_for(lambda: requests.get(f"{base}/v1/projects/cairn-test/subscriptions/lake-sub", timeout=2).status_code == 200, 120,
                 "subscription")
        yield base
    finally:
        stop(name)


def pull(base, sub, want, timeout=30):
    got = []
    end = time.time() + timeout
    while len(got) < want and time.time() < end:
        r = requests.post(f"{base}/v1/projects/cairn-test/subscriptions/{sub}:pull", json={"maxMessages": 10}, timeout=10).json()
        msgs = r.get("receivedMessages", [])
        if msgs:
            requests.post(f"{base}/v1/projects/cairn-test/subscriptions/{sub}:acknowledge",
                          json={"ackIds": [m["ackId"] for m in msgs]}, timeout=10)
        got += [m["message"] for m in msgs]
        if not msgs:
            time.sleep(0.3)
    return got


def test_pubsub_topic(pubsub, custom_store, tmp_path):
    st = store_with(custom_store, tmp_path, [{"id": "ps", "type": "pubsub", "topic": "projects/cairn-test/topics/lake-events",
                                              "endpoint": pubsub, "auth": "none"}])
    s3 = st.client()
    s3.create_bucket(Bucket="lake")
    notify(s3, "lake", "ps", prefix="raw/")
    s3.put_object(Bucket="lake", Key="other/skip.txt", Body=b"no")
    s3.put_object(Bucket="lake", Key="raw/e1.json", Body=b"{}")
    msgs = pull(pubsub, "lake-sub", 1)
    assert len(msgs) == 1
    rec = json.loads(base64.b64decode(msgs[0]["data"]))["Records"][0]
    assert rec["s3"]["object"]["key"] == "raw/e1.json" and rec["eventName"] == "ObjectCreated:Put"
    assert msgs[0]["attributes"]["key"] == "lake/raw/e1.json" and msgs[0]["attributes"]["x-cairn-event-id"].isdigit()


# --- NATS -------------------------------------------------------------------------------------------------

class NatsSub:
    """A minimal NATS subscriber: collects messages (with headers) on a subject."""

    def __init__(self, port, subject, token=""):
        self.s = socket.create_connection(("127.0.0.1", port), 5)
        self.f = self.s.makefile("rb")
        assert self.f.readline().startswith(b"INFO ")
        connect = {"verbose": False, "headers": True, "protocol": 1, **({"auth_token": token} if token else {})}
        self.s.sendall(f"CONNECT {json.dumps(connect)}\r\nSUB {subject} 1\r\nPING\r\n".encode())
        while self.f.readline().strip() != b"PONG":
            pass
        self.s.settimeout(30)

    def next(self):
        while True:
            line = self.f.readline().strip()
            if line == b"PING":
                self.s.sendall(b"PONG\r\n")
                continue
            parts = line.split()
            if parts and parts[0] == b"HMSG":
                hlen, total = int(parts[-2]), int(parts[-1])
                data = self.f.read(total + 2)[:total]
                headers = dict(h.split(": ", 1) for h in data[:hlen].decode().split("\r\n")[1:] if ": " in h)
                return headers, json.loads(data[hlen:])
            if parts and parts[0] == b"MSG":
                data = self.f.read(int(parts[-1]) + 2)[:-2]
                return {}, json.loads(data)


@pytest.fixture(scope="module")
def nats(tmp_path_factory):
    port, tport, kport = _free_port(), _free_port(), _free_port()
    d = tmp_path_factory.mktemp("nats")
    ssl = ["openssl"]
    subprocess.run([*ssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2",
                    "-subj", "/CN=cairn-test-ca"], cwd=d, check=True, capture_output=True)
    subprocess.run([*ssl, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=localhost"],
                   cwd=d, check=True, capture_output=True)
    (d / "ext.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    subprocess.run([*ssl, "x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem",
                    "-days", "2", "-extfile", "ext.cnf"], cwd=d, check=True, capture_output=True)
    os.chmod(d, 0o755)
    for f in ("srv.pem", "srv.key", "ca.pem"):
        os.chmod(d / f, 0o644)
    plain = run(NATS, port, 4222)
    secured = run(NATS, kport, 4222, "--auth", "s3cret-token-123")
    tls = run(NATS, tport, 4222, "--tls", "--tlscert", "/certs/srv.pem", "--tlskey", "/certs/srv.key", volumes=[f"{d}:/certs:ro"])
    try:
        for p in (port, kport, tport):
            wait_port(p)
        yield {"plain": port, "token": kport, "tls": tport, "ca": str(d / "ca.pem"), "names": (plain, secured, tls)}
    finally:
        for n in (plain, secured, tls):
            stop(n)


def test_nats_subjects_plain_token_and_tls(nats, custom_store, tmp_path, binary):
    targets = [
        {"id": "n", "type": "nats", "url": f"nats://127.0.0.1:{nats['plain']}", "subject": "cairn.lake"},
        {"id": "nk", "type": "nats", "url": f"nats://127.0.0.1:{nats['token']}", "subject": "cairn.secure", "token": "s3cret-token-123"},
        {"id": "nt", "type": "nats", "url": f"tls://localhost:{nats['tls']}", "subject": "cairn.tls"},
    ]
    f = tmp_path / "targets.json"
    f.write_text(json.dumps(targets))
    st = custom_store({"CAIRN_STORE_EVENT_TARGETS_FILE": str(f), "CAIRN_STORE_CA_FILE": nats["ca"]})
    s3 = st.client()
    subs = {"n": NatsSub(nats["plain"], "cairn.lake"), "nk": NatsSub(nats["token"], "cairn.secure", "s3cret-token-123")}
    for b, t in (("plain", "n"), ("secure", "nk"), ("tls", "nt")):
        s3.create_bucket(Bucket=f"bkt-{b}")
        notify(s3, f"bkt-{b}", t)
    s3.put_object(Bucket="bkt-plain", Key="one.txt", Body=b"1")
    s3.put_object(Bucket="bkt-plain", Key="two.txt", Body=b"22")
    h1, m1 = subs["n"].next()
    h2, m2 = subs["n"].next()
    assert [m["Records"][0]["s3"]["object"]["key"] for m in (m1, m2)] == ["one.txt", "two.txt"]
    assert int(h2["Nats-Msg-Id"]) > int(h1["Nats-Msg-Id"])  # JetStream deduplicates on it
    s3.put_object(Bucket="bkt-secure", Key="s.txt", Body=b"s")
    assert subs["nk"].next()[1]["Records"][0]["s3"]["bucket"]["name"] == "bkt-secure"
    # Over TLS (the CA from CAIRN_STORE_CA_FILE): a subscriber would need TLS too, so check delivery by the metrics.
    s3.put_object(Bucket="bkt-tls", Key="t.txt", Body=b"t")
    metrics = f"{st.endpoint}/-/metrics"
    wait_for(lambda: "cairn_store_events_delivered_total 4" in requests.get(metrics, timeout=10).text, what="TLS delivery")


def test_nats_wrong_token_is_retried_not_dropped(nats, custom_store, tmp_path):
    st = store_with(custom_store, tmp_path, [{"id": "nk", "type": "nats", "url": f"nats://127.0.0.1:{nats['token']}",
                                              "subject": "cairn.retry", "token": "wrong-token-000"}])
    s3 = st.client()
    s3.create_bucket(Bucket="retry")
    notify(s3, "retry", "nk")
    s3.put_object(Bucket="retry", Key="x", Body=b"x")
    wait_for(lambda: "cairn_store_events_failed_total 0" not in requests.get(f"{st.endpoint}/-/metrics", timeout=10).text,
             what="a failed attempt")
    assert "cairn_store_events_delivered_total 0" in requests.get(f"{st.endpoint}/-/metrics", timeout=10).text


@pytest.mark.parametrize("target", [
    {"id": "x", "type": "sqs", "queue_url": "ftp://q", "access_key": "a", "secret_key": "s"},
    {"id": "x", "type": "sqs", "queue_url": "http://q/1/q", "access_key": "a"},
    {"id": "x", "type": "pubsub", "topic": "lake-events", "auth": "none"},
    {"id": "x", "type": "pubsub", "topic": "projects/p/topics/t"},
    {"id": "x", "type": "nats", "url": "http://n:4222", "subject": "s"},
    {"id": "x", "type": "nats", "url": "nats://n:4222", "subject": "bad subject"},
    {"id": "x", "type": "nats", "url": "nats://n:4222", "subject": "s", "token": "t", "user": "u", "password": "p"},
])
def test_targets_that_do_not_fit_their_type_are_refused(target, binary, tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps([target]))
    st = Store(binary, str(tmp_path / "d"), _free_port(), env={"CAIRN_STORE_EVENT_TARGETS_FILE": str(f)})
    with pytest.raises(RuntimeError):
        st.start()
