"""The Kafka protocol on the message queues, through kafka-python's producer, consumer (with consumer
groups) and admin client: SASL/PLAIN sign-in (refused with a wrong secret; topic and read-only limits of
keys), partitions chosen by key as Kafka's partitioner does, keys and headers both ways between Kafka and
the HTTP queue API, group offsets shared with it, partitions split between a group's members and handed
back when one leaves, seeking by time, CreateTopics, TLS (SASL_SSL), and the startup refusals. Also the
HTTP API's partitions: added, never removed."""

import json
import os
import shutil
import subprocess
import time

import pytest
import requests
from botocore.auth import S3SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials

from conftest import ADMIN, REGION, Store, _free_port

kafka = pytest.importorskip("kafka")
from kafka import KafkaAdminClient, KafkaConsumer, KafkaProducer, TopicPartition  # noqa: E402
from kafka.admin import NewTopic  # noqa: E402
from kafka.errors import KafkaError  # noqa: E402
from kafka.partitioner.default import murmur2  # noqa: E402


def api(st, method, path, body=None, prefix="queues"):
    data = json.dumps(body).encode() if body is not None else b""
    req = AWSRequest(method=method, url=f"{st.endpoint}/-/{prefix}/v1/{path}", data=data)
    S3SigV4Auth(Credentials(*ADMIN), "s3", REGION).add_auth(req)
    r = requests.request(method, req.url, data=data, headers=dict(req.headers), timeout=60)
    return r.status_code, (r.json() if r.content else None)


def sasl(port, user=ADMIN[0], password=ADMIN[1], **kw):
    return {"bootstrap_servers": f"127.0.0.1:{port}", "security_protocol": kw.pop("security_protocol", "SASL_PLAINTEXT"),
            "sasl_mechanism": "PLAIN", "sasl_plain_username": user, "sasl_plain_password": password,
            "request_timeout_ms": 20000, **kw}


@pytest.fixture
def ks(custom_store):
    port = _free_port()
    st = custom_store({"CAIRN_STORE_KAFKA_ADDR": f"127.0.0.1:{port}", "CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT": "true"})
    return st, port


def consume(port, topic, group, want, timeout=30, **kw):
    c = KafkaConsumer(topic, **sasl(port, group_id=group, auto_offset_reset="earliest", enable_auto_commit=False,
                                    consumer_timeout_ms=2000, **kw))
    got = []
    end = time.time() + timeout
    while len(got) < want and time.time() < end:
        for msgs in c.poll(timeout_ms=1000).values():
            got.extend(msgs)
    return c, got


def test_produce_consume_and_share_with_the_http_api(ks):
    st, port = ks
    assert api(st, "PUT", "topics/orders", {"partitions": 3})[0] == 200
    p = KafkaProducer(**sasl(port, key_serializer=str.encode, value_serializer=str.encode))
    sent = {}
    for i in range(30):
        md = p.send("orders", key=f"customer-{i % 7}", value=f"order {i}", headers=[("source", b"kafka")]).get(timeout=20)
        sent.setdefault(md.partition, []).append(i)
        assert md.partition == (murmur2(f"customer-{i % 7}".encode()) & 0x7FFFFFFF) % 3  # Kafka's own partitioner
    p.flush()
    p.close()
    assert len(sent) == 3
    # The same messages through the HTTP API, partition by partition, keys and order intact.
    for part, idx in sent.items():
        code, got = api(st, "GET", f"topics/orders/messages?after=0&partition={part}&max=100")
        assert code == 200 and [m["value"] for m in got["messages"]] == [f"order {i}" for i in idx]
        assert got["messages"][0]["key"].startswith("customer-")
    # An HTTP publish with a key lands where Kafka's partitioner would put it.
    code, out = api(st, "POST", "topics/orders/messages?batch", {"messages": [{"key": "customer-3", "value": "from http"}]})
    assert code == 201 and out["partition"] == (murmur2(b"customer-3") & 0x7FFFFFFF) % 3
    c, got = consume(port, "orders", "billing", 31)
    assert len(got) == 31
    by_part = {}
    for m in got:
        by_part.setdefault(m.partition, []).append(m)
    for msgs in by_part.values():
        assert [m.offset for m in msgs] == list(range(len(msgs)))  # gapless offsets from 0, per partition
    kafka_msg = next(m for m in got if m.value != b"from http")
    assert kafka_msg.headers == [("source", b"kafka")] and kafka_msg.key.startswith(b"customer-")
    assert any(m.value == b"from http" and m.key == b"customer-3" for m in got)
    c.commit()
    c.close()
    # The group's committed offsets are the HTTP API's group offsets.
    code, t = api(st, "GET", "topics/orders")
    assert code == 200 and t["partitions"] == 3 and t["messages"] == 31
    for d in t["partition_detail"]:
        assert d["groups"]["billing"]["offset"] == d["messages"] and d["groups"]["billing"]["lag"] == 0
    # A second consumer in the group has nothing left; a new group starts over.
    c2, again = consume(port, "orders", "billing", 1, timeout=4)
    c2.close()
    assert again == []
    c3, fresh = consume(port, "orders", "audit", 31)
    c3.close()
    assert len(fresh) == 31


def test_group_members_split_partitions_and_take_over(ks):
    st, port = ks
    api(st, "PUT", "topics/events", {"partitions": 4})
    p = KafkaProducer(**sasl(port))
    for i in range(40):
        p.send("events", value=f"e{i}".encode(), partition=i % 4)
    p.flush()
    p.close()
    a = KafkaConsumer("events", **sasl(port, group_id="workers", auto_offset_reset="earliest", enable_auto_commit=False,
                                       session_timeout_ms=10000, heartbeat_interval_ms=500))
    b = KafkaConsumer("events", **sasl(port, group_id="workers", auto_offset_reset="earliest", enable_auto_commit=False,
                                       session_timeout_ms=10000, heartbeat_interval_ms=500))
    end = time.time() + 40
    while time.time() < end:
        a.poll(timeout_ms=300)
        b.poll(timeout_ms=300)
        if len(a.assignment()) == 2 and len(b.assignment()) == 2:
            break
    assert len(a.assignment()) == 2 and len(b.assignment()) == 2, (a.assignment(), b.assignment())
    assert not (a.assignment() & b.assignment())
    b.close()  # leaves the group: a takes every partition
    end = time.time() + 40
    while time.time() < end and len(a.assignment()) != 4:
        a.poll(timeout_ms=300)
    assert len(a.assignment()) == 4
    a.close()


def test_offsets_seeking_and_admin(ks):
    st, port = ks
    adm = KafkaAdminClient(**sasl(port))
    adm.create_topics([NewTopic("metrics", num_partitions=2, replication_factor=1)])
    assert api(st, "GET", "topics/metrics")[1]["partitions"] == 2
    with pytest.raises(KafkaError):
        adm.create_topics([NewTopic("metrics", num_partitions=1, replication_factor=1)])
    with pytest.raises(KafkaError):
        adm.create_topics([NewTopic("Bad Name", num_partitions=1, replication_factor=1)])
    adm.close()
    p = KafkaProducer(**sasl(port))
    p.send("metrics", value=b"early", partition=0).get(timeout=20)
    time.sleep(1.2)
    cut = int(time.time() * 1000)
    time.sleep(0.2)
    p.send("metrics", value=b"late", partition=0).get(timeout=20)
    p.close()
    c = KafkaConsumer(**sasl(port, enable_auto_commit=False))
    tp = TopicPartition("metrics", 0)
    c.assign([tp])
    assert c.beginning_offsets([tp])[tp] == 0 and c.end_offsets([tp])[tp] == 2
    assert c.offsets_for_times({tp: cut})[tp].offset == 1
    c.seek(tp, 1)
    got = []
    end = time.time() + 15
    while not got and time.time() < end:
        got = [m.value for ms in c.poll(timeout_ms=1000).values() for m in ms]
    assert got == [b"late"]
    c.close()


def test_sign_in_and_key_limits(ks):
    st, port = ks
    api(st, "PUT", "topics/orders-eu", {})
    api(st, "PUT", "topics/payroll", {})
    with pytest.raises(KafkaError):
        KafkaProducer(**sasl(port, password="wrong-secret", api_version_auto_timeout_ms=5000)).send("orders-eu", b"x").get(timeout=10)
    code, k = api(st, "POST", "keys", {"buckets": ["orders-*"]}, prefix="admin")
    assert code == 201
    p = KafkaProducer(**sasl(port, user=k["access_key"], password=k["secret_key"]))
    p.send("orders-eu", b"allowed").get(timeout=20)
    with pytest.raises(KafkaError):
        p.send("payroll", b"not allowed").get(timeout=20)
    p.close()
    code, ro = api(st, "POST", "keys", {"buckets": ["orders-*"], "read_only": True}, prefix="admin")
    rp = KafkaProducer(**sasl(port, user=ro["access_key"], password=ro["secret_key"], retries=0))
    with pytest.raises(KafkaError):
        rp.send("orders-eu", b"read-only keys do not produce").get(timeout=20)
    rp.close()
    c, got = consume(port, "orders-eu", "orders-readers", 1, user=ro["access_key"], password=ro["secret_key"])
    assert [m.value for m in got] == [b"allowed"]
    c.commit()
    c.close()
    assert api(st, "GET", "topics/orders-eu")[1]["groups"]["orders-readers"]["offset"] == 1


def test_http_partitions_are_added_never_removed(ks):
    st, _ = ks
    assert api(st, "PUT", "topics/grow", {"partitions": 2})[0] == 200
    assert api(st, "PUT", "topics/grow", {"partitions": 5})[1]["partitions"] == 5
    assert api(st, "PUT", "topics/grow", {"retention_seconds": 60})[1]["partitions"] == 5  # unchanged when not given
    assert api(st, "PUT", "topics/grow", {"partitions": 3})[0] == 400
    assert api(st, "PUT", "topics/grow", {"partitions": 65})[0] == 400
    assert api(st, "POST", "topics/grow/messages?partition=4", None)[0] == 201
    assert api(st, "POST", "topics/grow/messages?partition=5", None)[0] == 400
    assert api(st, "POST", "topics/grow/groups/g", {"seq": 1, "partition": 4})[1]["offset"] == 1
    assert [t["name"] for t in api(st, "GET", "topics")[1]["topics"]] == ["grow"]  # partitions are not topics
    assert api(st, "DELETE", "topics/grow")[0] == 204
    assert api(st, "GET", "topics")[1]["topics"] == []


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_sasl_ssl(custom_store, tmp_path):
    d = tmp_path
    run = lambda *a: subprocess.run(["openssl", *a], cwd=d, check=True, capture_output=True)  # noqa: E731
    run("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem", "-days", "2", "-subj", "/CN=cairn-ca")
    run("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key", "-out", "srv.csr", "-subj", "/CN=localhost")
    (d / "ext.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    run("x509", "-req", "-in", "srv.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "srv.pem", "-days", "2",
        "-extfile", "ext.cnf")
    port = _free_port()
    st = custom_store({"CAIRN_STORE_KAFKA_ADDR": f"127.0.0.1:{port}", "CAIRN_STORE_KAFKA_ADVERTISED": f"localhost:{port}",
                       "CAIRN_STORE_TLS_CERT": str(d / "srv.pem"), "CAIRN_STORE_TLS_KEY": str(d / "srv.key"),
                       "CAIRN_STORE_CA_FILE": str(d / "ca.pem")}, )
    s3 = st.client()  # noqa: F841
    tls = {"security_protocol": "SASL_SSL", "ssl_cafile": str(d / "ca.pem"), "bootstrap_servers": f"localhost:{port}"}
    adm = KafkaAdminClient(**{**sasl(port), **tls})
    adm.create_topics([NewTopic("secure", num_partitions=1, replication_factor=1)])
    adm.close()
    p = KafkaProducer(**{**sasl(port), **tls})
    p.send("secure", b"over tls").get(timeout=20)
    p.close()
    c = KafkaConsumer("secure", **{**sasl(port, group_id="tls", auto_offset_reset="earliest"), **tls})
    got = []
    end = time.time() + 20
    while not got and time.time() < end:
        got = [m.value for ms in c.poll(timeout_ms=1000).values() for m in ms]
    c.close()
    assert got == [b"over tls"]


@pytest.mark.parametrize("env", [
    {"CAIRN_STORE_KAFKA_ADDR": "127.0.0.1:{port}"},  # no TLS and no opt-in to plaintext
    {"CAIRN_STORE_KAFKA_ADDR": "0.0.0.0:{port}", "CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT": "true"},  # nothing to advertise
    {"CAIRN_STORE_KAFKA_ADDR": "127.0.0.1:{port}", "CAIRN_STORE_KAFKA_ADVERTISED": "nohost", "CAIRN_STORE_KAFKA_ALLOW_PLAINTEXT": "true"},
])
def test_listener_settings_that_do_not_hold_are_refused(env, binary, tmp_path):
    port = _free_port()
    st = Store(binary, str(tmp_path / "d"), _free_port(), env={k: v.format(port=port) for k, v in env.items()})
    with pytest.raises(RuntimeError):
        st.start()
    assert os.path.exists(tmp_path)
