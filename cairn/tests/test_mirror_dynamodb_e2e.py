"""DynamoDB mirrors (L167): a DynamoDB table kept in a lakehouse table through DynamoDB Streams, against AWS's
DynamoDB Local (its streams included) and the real store and catalog. Covered:
- the copy (a consistent scan), then inserts, modifications and removals through the stream, replayed from its start
  over the copy and converging on it;
- a composite key (a string partition key and a numeric sort key, kept as exact text); strings, numbers, binary,
  booleans, nulls, maps, lists and sets; an attribute that first appears later becoming a column;
- each shard's position recorded after the lakehouse commit, and a replay from older positions leaving the table
  right;
- the stream turned off and on again, and a mirror that has not run for longer than the stream keeps records, both
  copying the table again (audited);
- refusals: a table without a stream, a stream without new images, a table that does not exist.
Shard lineage (a parent read to its end before its children) and trimmed positions are covered against a scripted
stream in ``test_mirror_dynamodb.py``; DynamoDB Local cannot split shards on demand."""

import os
import subprocess
import time

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port  # noqa: F401  (fixtures)

IMAGE = "amazon/dynamodb-local:3.3.2"

pytestmark = pytest.mark.skipif(not _ready() or subprocess.run(["docker", "image", "inspect", IMAGE],
                                                                capture_output=True).returncode != 0,
                                reason="needs docker, the DynamoDB Local image, and the store and catalog binaries")
boto3 = pytest.importorskip("boto3")


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.mirror import Mirrors
    from cairn.pipelines.streams import Streams

    p = port()
    name = f"cairn-mirror-dynamodb-{p}-{os.getpid()}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:8000", IMAGE,
                    "-jar", "DynamoDBLocal.jar", "-inMemory"], check=True, capture_output=True)
    ddb = boto3.client("dynamodb", endpoint_url=f"http://127.0.0.1:{p}", region_name="us-east-1",
                       aws_access_key_id="cairntest", aws_secret_access_key="cairntest")
    end = time.time() + 60
    while True:
        try:
            ddb.list_tables()
            break
        except Exception:  # noqa: BLE001 - still starting
            if time.time() > end:
                raise
            time.sleep(1)
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('ddbmirror')}/cairn.db")
    init_db(engine)
    streams = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                   "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                   "s3.region": "us-east-1", "s3.path-style-access": "true"})
    url = f"dynamodb://cairntest:cairntest@127.0.0.1:{p}?region=us-east-1&tls=false"
    audit = []

    class Audit:
        def record(self, *args, **kwargs):
            audit.append(args)
    m = Mirrors(engine, streams, {"ddb": url}.get, audit=Audit())
    yield {"m": m, "ddb": ddb, "streams": streams, "engine": engine, "audit": audit}
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def make(world, table, view="NEW_AND_OLD_IMAGES", sort=True):
    keys = [{"AttributeName": "pk", "KeyType": "HASH"}] + ([{"AttributeName": "sk", "KeyType": "RANGE"}] if sort else [])
    attrs = [{"AttributeName": "pk", "AttributeType": "S"}] + ([{"AttributeName": "sk", "AttributeType": "N"}] if sort else [])
    kw = {"TableName": table, "KeySchema": keys, "AttributeDefinitions": attrs, "BillingMode": "PAY_PER_REQUEST"}
    if view:
        kw["StreamSpecification"] = {"StreamEnabled": True, "StreamViewType": view}
    world["ddb"].create_table(**kw)


def put(world, table, item):
    world["ddb"].put_item(TableName=table, Item=item)


def rows(world, target):
    from pyiceberg.exceptions import NoSuchTableError

    try:
        t = world["streams"]._catalog().load_table(tuple(target.split(".")))
    except NoSuchTableError:
        return []
    return sorted(t.scan().to_arrow().to_pylist(), key=lambda r: (r["pk"], r.get("sk") or ""))


def create(world, name, table):
    return world["m"].create("urn:li:corpuser:ops", {"name": name, "connection": "ddb", "table": table,
                                                     "target": f"ddb.{name}"})


def test_a_dynamodb_table_is_copied_then_kept_in_step(world):
    m = world["m"]
    make(world, "orders-2026")
    put(world, "orders-2026", {"pk": {"S": "c#1"}, "sk": {"N": "1"}, "total": {"N": "12.5"}, "paid": {"BOOL": True},
                               "note": {"NULL": True}, "raw": {"B": b"\x00\xff"},
                               "address": {"M": {"city": {"S": "Leeds"}, "zip": {"N": "12345"}}},
                               "items": {"L": [{"S": "a"}, {"N": "2"}]}, "tags": {"SS": ["b", "a"]}})
    put(world, "orders-2026", {"pk": {"S": "c#1"}, "sk": {"N": "2"}, "total": {"N": "3"}})
    out = create(world, "ddb_orders", "orders-2026")
    assert out["status"] == "running" and out["key"] == ["pk", "sk"], out
    got = rows(world, "ddb.ddb_orders")
    assert [(r["pk"], r["sk"], r["total"]) for r in got] == [("c#1", "1", 12.5), ("c#1", "2", 3.0)]
    a = got[0]
    assert a["paid"] is True and a["note"] is None and a["raw"] == "00ff"
    assert a["address"] == '{"city": "Leeds", "zip": 12345}' and a["items"] == '["a", 2]' and a["tags"] == '["a", "b"]'
    # Changes: an insert with a new attribute, a modification, a removal.
    put(world, "orders-2026", {"pk": {"S": "c#2"}, "sk": {"N": "1"}, "total": {"N": "7"}, "channel": {"S": "web"}})
    world["ddb"].update_item(TableName="orders-2026", Key={"pk": {"S": "c#1"}, "sk": {"N": "1"}},
                             UpdateExpression="SET #t = :t", ExpressionAttributeNames={"#t": "total"},
                             ExpressionAttributeValues={":t": {"N": "99.75"}})
    world["ddb"].delete_item(TableName="orders-2026", Key={"pk": {"S": "c#1"}, "sk": {"N": "2"}})
    m.run("ddb_orders")
    got = rows(world, "ddb.ddb_orders")
    assert [(r["pk"], r["sk"], r["total"], r.get("channel")) for r in got] == \
        [("c#1", "1", 99.75, None), ("c#2", "1", 7.0, "web")], got
    assert m.run("ddb_orders") == 0                                   # nothing new
    assert m.get("ddb_orders")["status"] == "running"


def test_positions_and_a_replay_after_a_crash(world):
    from sqlalchemy import select, update

    from cairn.pipelines.mirror import mirror_shards_table, mirrors_table

    m = world["m"]
    make(world, "ledger", sort=False)
    create(world, "ddb_ledger", "ledger")
    for i in range(1, 31):
        put(world, "ledger", {"pk": {"S": f"k{i:02d}"}, "v": {"N": str(i)}})
    m.run("ddb_ledger")
    assert len(rows(world, "ddb.ddb_ledger")) == 30
    with world["engine"].connect() as c:
        mid = c.execute(select(mirrors_table.c.id).where(mirrors_table.c.name == "ddb_ledger")).scalar()
        shards = c.execute(select(mirror_shards_table).where(mirror_shards_table.c.mirror_id == mid)).all()
    assert shards and all(s.position for s in shards)
    # The process died after the lakehouse commit and before it recorded the positions: it reads them again.
    with world["engine"].begin() as c:
        c.execute(update(mirror_shards_table).where(mirror_shards_table.c.mirror_id == mid).values(position=None))
    world["ddb"].delete_item(TableName="ledger", Key={"pk": {"S": "k07"}})
    m.run("ddb_ledger")
    got = rows(world, "ddb.ddb_ledger")
    assert len(got) == 29 and "k07" not in [r["pk"] for r in got] and sum(r["v"] for r in got) == sum(range(1, 31)) - 7


def test_a_new_stream_or_a_long_pause_copies_the_table_again(world):
    from sqlalchemy import update

    from cairn.pipelines.mirror import mirrors_table

    m = world["m"]
    make(world, "stock", sort=False)
    for i in (1, 2, 3):
        put(world, "stock", {"pk": {"S": f"s{i}"}, "qty": {"N": str(i * 10)}})
    create(world, "ddb_stock", "stock")
    ddb = world["ddb"]
    ddb.update_table(TableName="stock", StreamSpecification={"StreamEnabled": False})
    ddb.delete_item(TableName="stock", Key={"pk": {"S": "s2"}})            # not in any stream the mirror reads
    ddb.update_table(TableName="stock", StreamSpecification={"StreamEnabled": True, "StreamViewType": "NEW_IMAGE"})
    m.run("ddb_stock")
    assert [r["pk"] for r in rows(world, "ddb.ddb_stock")] == ["s1", "s3"]
    assert any(a[1] == "mirror.restarted" and "new stream" in str(a[4]) for a in world["audit"])
    # A day away: the stream may have dropped records the mirror never read.
    with world["engine"].begin() as c:
        c.execute(update(mirrors_table).where(mirrors_table.c.name == "ddb_stock").values(
            last_commit_ms=int(time.time() * 1000) - 25 * 3600 * 1000))
    m.run("ddb_stock")
    assert any(a[1] == "mirror.restarted" and "longer than the stream" in str(a[4]) for a in world["audit"])
    assert [r["pk"] for r in rows(world, "ddb.ddb_stock")] == ["s1", "s3"]
    put(world, "stock", {"pk": {"S": "s4"}, "qty": {"N": "40"}})
    m.run("ddb_stock")
    assert [(r["pk"], r["qty"]) for r in rows(world, "ddb.ddb_stock")] == [("s1", 10.0), ("s3", 30.0), ("s4", 40.0)]


def test_refusals(world):
    from cairn.pipelines.mirror import MirrorError

    m = world["m"]
    make(world, "nostream", view=None)
    make(world, "keysonly", view="KEYS_ONLY")
    for table, words in (("nostream", "has no stream"), ("keysonly", "KEYS_ONLY"), ("missing-table", "no DynamoDB")):
        with pytest.raises(MirrorError) as e:
            create(world, "ddb_refused", table)
        assert e.value.status == 409 and words in str(e.value), e.value
        assert "cairntest" not in str(e.value)
    assert "update-table" in str(pytest.raises(MirrorError, create, world, "ddb_refused", "nostream").value)
    with pytest.raises(MirrorError):
        create(world, "ddb_refused", "a;b")
    assert "ddb_refused" not in [x["name"] for x in m.list()]
