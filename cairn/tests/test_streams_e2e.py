"""Streams (cairn.pipelines.streams) with a real Kafka broker, Cairn Store and Cairn Catalog: Debezium change
events keep an Iceberg table in step with the source, plain JSON appends, bad messages are set aside, and a
worker that lost its Kafka offsets resumes from the offsets in the table's own snapshots (exactly once).
Needs docker (apache/kafka) and the store and catalog binaries."""

import json
import subprocess
import time
import uuid

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port, wait  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")
KAFKA_IMAGE = "apache/kafka:3.9.1"


@pytest.fixture(scope="module")
def kafka():
    kp, cp = port(), port()
    name = f"cairn-streams-kafka-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--network", "host",
                    "-e", "KAFKA_NODE_ID=1", "-e", "KAFKA_PROCESS_ROLES=broker,controller",
                    "-e", f"KAFKA_LISTENERS=PLAINTEXT://127.0.0.1:{kp},CONTROLLER://127.0.0.1:{cp}",
                    "-e", f"KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://127.0.0.1:{kp}",
                    "-e", "KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER",
                    "-e", "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
                    "-e", f"KAFKA_CONTROLLER_QUORUM_VOTERS=1@127.0.0.1:{cp}",
                    "-e", "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1", "-e", "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0",
                    "-e", "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1", "-e", "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1",
                    KAFKA_IMAGE], check=True, capture_output=True)
    try:
        wait(kp, 90)
        from kafka import KafkaProducer
        from kafka.admin import KafkaAdminClient, NewTopic

        end = time.time() + 90
        while True:
            try:
                admin = KafkaAdminClient(bootstrap_servers=f"127.0.0.1:{kp}")
                break
            except Exception:
                if time.time() > end:
                    raise
                time.sleep(1)
        admin.create_topics([NewTopic("pg.public.customers", 2, 1), NewTopic("clicks", 2, 1)])
        producer = KafkaProducer(bootstrap_servers=f"127.0.0.1:{kp}", acks="all")
        yield {"bootstrap": f"127.0.0.1:{kp}", "admin": admin, "producer": producer}
        producer.close()
        admin.close()
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="module")
def streams(lake, kafka, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.streams import Streams

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('streams')}/cairn.db")
    init_db(engine)
    return Streams(engine, {"bootstrap_servers": kafka["bootstrap"]},
                   {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                    "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
                    "s3.path-style-access": "true"})


def table_rows(s, target):
    from pyiceberg.exceptions import NoSuchTableError

    try:
        return sorted(s._catalog().load_table(tuple(target.split("."))).scan().to_arrow().to_pylist(),
                      key=lambda r: json.dumps(r, sort_keys=True, default=str))
    except NoSuchTableError:
        return []


def wait_for(fn, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.5)
    return fn()


def change(op, before=None, after=None, envelope=True):
    payload = {"before": before, "after": after, "op": op, "ts_ms": int(time.time() * 1000),
               "source": {"connector": "postgresql", "table": "customers"}}
    return json.dumps({"schema": {"type": "struct"}, "payload": payload} if envelope else payload).encode()


def send(kafka, topic, items):
    for key, value in items:
        kafka["producer"].send(topic, key=key, value=value)
    kafka["producer"].flush()


def start(s, stream):
    from cairn.pipelines.streams import StreamWorker

    w = StreamWorker(s, {k: v for k, v in stream.items() if k != "status"})
    w.start()
    return w


def stop(w):
    w.stop.set()
    w.join(60)
    assert not w.is_alive()


def test_debezium_changes_keep_the_table_in_step(streams, kafka):
    s = streams
    st = s.create({"name": "customers", "topic": "pg.public.customers", "format": "debezium", "key": ["id"],
                   "target": "cdc.customers", "create_table": True, "batch_seconds": 1}, "urn:li:corpuser:admin")
    k = lambda i: json.dumps({"id": i}).encode()  # noqa: E731 - Debezium keys: same key, same partition
    send(kafka, "pg.public.customers", [
        (k(1), change("r", after={"id": 1, "name": "Ada", "tier": "gold"})),           # snapshot read
        (k(2), change("c", after={"id": 2, "name": "Bob", "tier": "silver"})),
        (k(3), change("c", after={"id": 3, "name": "Cy", "tier": "bronze"}, envelope=False)),
        (k(2), change("u", before={"id": 2}, after={"id": 2, "name": "Bob", "tier": "gold"})),
        (k(3), change("d", before={"id": 3, "name": "Cy", "tier": "bronze"})),
        (k(4), b"{not json"),                                                          # set aside, not fatal
        (k(3), None),                                                                  # Debezium's tombstone
    ])
    w = start(s, st)
    try:
        want = [{"id": 1, "name": "Ada", "tier": "gold"}, {"id": 2, "name": "Bob", "tier": "gold"}]
        assert wait_for(lambda: table_rows(s, "cdc.customers") == want), table_rows(s, "cdc.customers")
        # Later changes land too: a delete and a re-insert of an existing key.
        send(kafka, "pg.public.customers", [(k(1), change("d", before={"id": 1})),
                                            (k(5), change("c", after={"id": 5, "name": "Eve", "tier": "silver"})),
                                            (k(2), change("u", before={"id": 2}, after={"id": 2, "name": "Rob", "tier": "gold"}))])
        want = [{"id": 2, "name": "Rob", "tier": "gold"}, {"id": 5, "name": "Eve", "tier": "silver"}]
        assert wait_for(lambda: table_rows(s, "cdc.customers") == want), table_rows(s, "cdc.customers")
    finally:
        stop(w)
    rejects = s.rejects(st["id"])
    assert [(r["reason"], r["value"]) for r in rejects] == [("the value is not JSON", "{not json")]
    status = [d for d in s.list() if d["id"] == st["id"]][0]["status"]
    assert status["status"] == "running" and status["rows"] >= 5 and status["error"] is None
    # Every partition's next offset is in the table's snapshots: that is where a worker resumes.
    assert sum(s.offsets(s.get(st["id"])).values()) == 10


def test_a_worker_that_lost_its_kafka_offsets_writes_nothing_twice(streams, kafka):
    s = streams
    st = s.create({"name": "clicks", "topic": "clicks", "format": "json", "target": "raw.clicks", "create_table": True,
                   "batch_seconds": 1}, "urn:li:corpuser:admin")
    send(kafka, "clicks", [(str(i).encode(), json.dumps({"n": i, "page": f"/p{i}"}).encode()) for i in range(5)])
    w = start(s, st)
    try:
        assert wait_for(lambda: len(table_rows(s, "raw.clicks")) == 5)
    finally:
        stop(w)
    # As if the worker died between the table commit and Kafka's: the group's offsets are gone.
    kafka["admin"].delete_consumer_groups([f"cairn-stream-{st['id']}"])
    send(kafka, "clicks", [(str(i).encode(), json.dumps({"n": i, "page": f"/p{i}"}).encode()) for i in range(5, 8)]
         + [(b"x", json.dumps({"n": 99, "referrer": "ad"}).encode())])   # a column the table lacks
    w = start(s, st)
    try:
        assert wait_for(lambda: len(table_rows(s, "raw.clicks")) == 8 and s.rejects(st["id"]))
        time.sleep(3)                                                    # and nothing more arrives
        rows = table_rows(s, "raw.clicks")
    finally:
        stop(w)
    assert sorted(r["n"] for r in rows) == list(range(8))
    assert s.rejects(st["id"])[0]["reason"] == "columns raw.clicks does not have: referrer"


def test_evolve_adds_new_columns(streams, kafka):
    s = streams
    kafka["admin"].create_topics([__import__("kafka.admin", fromlist=["NewTopic"]).NewTopic("events", 1, 1)])
    st = s.create({"name": "events", "topic": "events", "format": "json", "target": "raw.events", "create_table": True,
                   "evolve": True, "batch_seconds": 1}, "urn:li:corpuser:admin")
    send(kafka, "events", [(None, b'{"id": 1, "kind": "a"}')])
    w = start(s, st)
    try:
        assert wait_for(lambda: len(table_rows(s, "raw.events")) == 1)
        send(kafka, "events", [(None, b'{"id": 2, "kind": "b", "region": "EU"}')])
        rows = wait_for(lambda: (r := table_rows(s, "raw.events")) and len(r) == 2 and r)
    finally:
        stop(w)
    assert rows == [{"id": 1, "kind": "a", "region": None}, {"id": 2, "kind": "b", "region": "EU"}]
    assert s.rejects(st["id"]) == []


def test_definitions_are_checked(streams):
    from cairn.pipelines.streams import StreamError

    s = streams
    for body, msg in [({"name": "x", "topic": "t", "format": "debezium", "target": "a.b"}, "key columns"),
                      ({"name": "x", "topic": "t", "format": "csv", "target": "a.b"}, "format"),
                      ({"name": "x", "topic": "t/../", "target": "a.b"}, "Kafka topic"),
                      ({"name": "x", "topic": "t", "target": "nodots"}, "namespace.table"),
                      ({"name": "x", "topic": "t", "target": "a.b", "key": ["id; drop"], "mode": "upsert"}, "column names"),
                      ({"name": "x", "topic": "t", "target": "a.b", "batch_rows": 0}, "batch_rows")]:
        with pytest.raises(StreamError, match=msg):
            s.create(body, "urn:li:corpuser:admin")
    with pytest.raises(StreamError, match="exists"):
        s.create({"name": "clicks", "topic": "t", "target": "a.b"}, "urn:li:corpuser:admin")


def test_the_api_is_for_operators(streams):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.testclient import TestClient

    from cairn.common.security import Principal
    from cairn.pipelines.streams import build_streams_router

    def who(request: Request):
        return Principal(request.headers.get("x-user", "urn:li:corpuser:admin"), "token")

    def authorize(p, privilege, urn):
        if p.urn != "urn:li:corpuser:admin":
            raise HTTPException(403, "no")
    app = FastAPI()
    app.include_router(build_streams_router(streams, who, authorize))
    c = TestClient(app)
    assert c.get("/v1/streams", headers={"x-user": "urn:li:corpuser:eve"}).status_code == 403
    assert c.post("/v1/streams", json={"name": "y", "topic": "t", "target": "a.b"},
                  headers={"x-user": "urn:li:corpuser:eve"}).status_code == 403
    r = c.post("/v1/streams", json={"name": "orders", "topic": "orders", "target": "raw.orders"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert c.post("/v1/streams", json={"name": "bad", "topic": "t", "format": "debezium", "target": "a.b"}).status_code == 400
    assert c.post(f"/v1/streams/{sid}/enabled", json={"enabled": False}).json()["enabled"] is False
    assert [d["enabled"] for d in c.get("/v1/streams").json() if d["id"] == sid] == [False]
    assert c.get(f"/v1/streams/{sid}/rejects").json() == []
    assert c.delete(f"/v1/streams/{sid}").status_code == 204
    assert c.delete(f"/v1/streams/{sid}").status_code == 404


def test_the_source_is_lineage_and_a_failed_write_creates_nothing(streams):
    from cairn.common.core import InMemoryGraph
    from cairn.pipelines.streams import StreamError

    s = streams
    src = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.orders,PROD)"
    target = s.urn("cdc", "orders")
    graph = InMemoryGraph()
    graph.emit([__import__("cairn.common.proposals", fromlist=["make_mcp"]).make_mcp(target, "upstreamLineage", {
        "upstreams": [{"dataset": "urn:li:dataset:(urn:li:dataPlatform:kafka,orders,PROD)", "type": "COPY",
                       "auditStamp": {"time": 0, "actor": "urn:li:corpuser:x"}}]})])
    s.graph = graph
    try:
        with pytest.raises(StreamError, match="dataset URN"):
            s.create({"name": "o", "topic": "orders", "target": "cdc.orders", "source": "postgres.orders"}, "urn:li:corpuser:admin")
        st = s.create({"name": "orders-cdc", "topic": "pg.public.orders", "format": "debezium", "key": ["id"],
                       "target": "cdc.orders", "source": src}, "urn:li:corpuser:admin")
        assert st["source"] == src
        ups = [u["dataset"] for u in graph.get_aspect(target, "upstreamLineage")["upstreams"]]
        assert ups == ["urn:li:dataset:(urn:li:dataPlatform:kafka,orders,PROD)", src]     # merged, not replaced

        def broken(mcps):
            raise RuntimeError("core is down")
        graph.emit = broken
        with pytest.raises(RuntimeError):
            s.create({"name": "orders-2", "topic": "x", "target": "cdc.orders2", "mode": "upsert", "key": "id",
                      "source": src}, "urn:li:corpuser:admin")
        assert "orders-2" not in [d["name"] for d in s.list()]
    finally:
        s.graph = None
