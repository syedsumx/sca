"""MongoDB mirrors (Openflow's MongoDB connector, Cairn's way): a MongoDB 7 collection kept in a lakehouse table through
change streams, on a real single-node replica set, with the real store and catalog. Covered:
- the copy, then inserts, updates, replacements and deletes; ObjectIds, decimals, dates and embedded documents;
- a field that appears later becomes a column;
- a multi-document transaction arrives whole, and an aborted one leaves nothing;
- replaying from an older position after a crash leaves the table right (nothing doubled);
- a dropped collection fails the mirror with what to do; a missing collection and a server that is not a replica
  set are refused."""

import datetime as dt
import json
import os
import subprocess
import time

import pytest

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake, port  # noqa: F401  (fixtures)

IMAGE = "mongo:7.0"

pytestmark = pytest.mark.skipif(not _ready() or subprocess.run(["docker", "image", "inspect", IMAGE],
                                                                capture_output=True).returncode != 0,
                                reason="needs docker, the MongoDB image, and the store and catalog binaries")
pytest.importorskip("pymongo")


def start_mongo(replica_set=True):
    from pymongo import MongoClient

    p = port()
    name = f"cairn-mirror-mongo-{p}-{os.getpid()}"
    args = ["--replSet", "rs0", "--bind_ip_all"] if replica_set else ["--bind_ip_all"]
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{p}:27017", IMAGE, *args],
                   check=True, capture_output=True)
    url = f"mongodb://127.0.0.1:{p}/?directConnection=true"
    end = time.time() + 120
    while True:
        try:
            c = MongoClient(url, serverSelectionTimeoutMS=2000)
            c.admin.command("ping")
            break
        except Exception:  # noqa: BLE001 - starting
            if time.time() > end:
                raise
            time.sleep(1)
    if replica_set:
        c.admin.command("replSetInitiate", {"_id": "rs0", "members": [{"_id": 0, "host": "127.0.0.1:27017"}]})
        while not c.admin.command("hello").get("isWritablePrimary"):
            time.sleep(0.5)
    c.close()
    return name, url


@pytest.fixture(scope="module")
def world(lake, tmp_path_factory):  # noqa: F811
    from pymongo import MongoClient
    from sqlalchemy import create_engine

    from cairn.common.db import init_db
    from cairn.pipelines.mirror import Mirrors
    from cairn.pipelines.streams import Streams

    rs, url = start_mongo()
    single, single_url = start_mongo(replica_set=False)
    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('mgmirror')}/cairn.db")
    init_db(engine)
    streams = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                                   "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                                   "s3.region": "us-east-1", "s3.path-style-access": "true"})
    m = Mirrors(engine, streams, {"shop": url, "single": single_url}.get)
    client = MongoClient(url)
    yield {"m": m, "streams": streams, "db": client.shop, "client": client, "engine": engine}
    client.close()
    subprocess.run(["docker", "rm", "-f", rs, single], capture_output=True)


def rows(world, target):
    from pyiceberg.exceptions import NoSuchTableError

    try:
        t = world["streams"]._catalog().load_table(tuple(target.split(".")))
    except NoSuchTableError:
        return []
    return sorted(t.scan().to_arrow().to_pylist(), key=lambda r: str(r["_id"]))


def test_a_collection_is_copied_then_kept_in_step(world):
    from bson import Decimal128, ObjectId

    m, db = world["m"], world["db"]
    a, b = ObjectId(), ObjectId()
    db.customers.insert_many([
        {"_id": a, "name": "Ana", "score": Decimal128("9.50"), "born": dt.datetime(1990, 4, 1),
         "address": {"city": "Munich", "zip": "80331"}, "tags": ["vip"]},
        {"_id": b, "name": "Bob", "score": None}])
    out = m.create("urn:li:corpuser:ops", {"name": "mg_customers", "connection": "shop", "table": "shop.customers",
                                           "target": "shop_mg.customers"})
    assert out["status"] == "running" and out["key"] == ["_id"], out
    got = {r["_id"]: r for r in rows(world, "shop_mg.customers")}
    assert set(got) == {str(a), str(b)} and got[str(a)]["score"] == 9.5
    assert got[str(a)]["born"] == dt.datetime(1990, 4, 1, tzinfo=dt.timezone.utc)
    assert json.loads(got[str(a)]["address"]) == {"city": "Munich", "zip": "80331"}
    # Changes since: an insert with a new field, an update, a replacement and a delete.
    c = ObjectId()
    db.customers.insert_one({"_id": c, "name": "Cal", "segment": "retail"})
    db.customers.update_one({"_id": a}, {"$set": {"score": Decimal128("7.25")}})
    db.customers.replace_one({"_id": c}, {"name": "Cal Jr", "segment": "wholesale"})
    db.customers.delete_one({"_id": b})
    assert m.run("mg_customers") >= 3                                 # two changes to Cal are one upsert
    got = {r["_id"]: r for r in rows(world, "shop_mg.customers")}
    assert set(got) == {str(a), str(c)} and got[str(a)]["score"] == 7.25
    assert got[str(c)]["name"] == "Cal Jr" and got[str(c)]["segment"] == "wholesale"
    assert m.run("mg_customers") == 0
    assert m.get("mg_customers")["last_lsn"].startswith("{")


def test_transactions_arrive_whole_and_aborted_ones_leave_nothing(world):
    m, db, client = world["m"], world["db"], world["client"]
    db.create_collection("orders")
    m.create("urn:li:corpuser:ops", {"name": "mg_orders", "connection": "shop", "table": "shop.orders",
                                     "target": "shop_mg.orders"})
    with client.start_session() as s:
        with s.start_transaction():
            db.orders.insert_many([{"_id": i, "v": i} for i in range(1, 6)], session=s)
    s = client.start_session()
    s.start_transaction()
    db.orders.insert_one({"_id": 99, "v": 99}, session=s)
    s.abort_transaction()
    s.end_session()
    assert m.run("mg_orders") == 5
    assert [r["_id"] for r in rows(world, "shop_mg.orders")] == [1, 2, 3, 4, 5]


def test_a_replay_after_a_crash_leaves_the_table_right(world):
    from sqlalchemy import update

    from cairn.pipelines.mirror import mirrors_table

    m, db = world["m"], world["db"]
    db.create_collection("events")
    m.create("urn:li:corpuser:ops", {"name": "mg_events", "connection": "shop", "table": "shop.events",
                                     "target": "shop_mg.events"})
    before = m.get("mg_events")["last_lsn"]
    db.events.insert_many([{"_id": i, "v": i} for i in range(1, 41)])
    m.run("mg_events")
    assert len(rows(world, "shop_mg.events")) == 40
    with world["engine"].begin() as c:
        c.execute(update(mirrors_table).where(mirrors_table.c.name == "mg_events").values(last_lsn=before))
    db.events.update_one({"_id": 7}, {"$set": {"v": -1}})
    m.run("mg_events")
    got = sorted(rows(world, "shop_mg.events"), key=lambda r: r["_id"])
    assert len(got) == 40 and got[6]["v"] == -1


def test_a_dropped_collection_and_refusals(world):
    from cairn.pipelines.mirror import MirrorError

    m, db = world["m"], world["db"]
    db.carts.insert_one({"_id": 1})
    m.create("urn:li:corpuser:ops", {"name": "mg_carts", "connection": "shop", "table": "shop.carts",
                                     "target": "shop_mg.carts"})
    db.carts.drop()
    m.run("mg_carts")
    got = m.get("mg_carts")
    assert got["status"] == "failed" and "create it again" in got["error"], got
    with pytest.raises(MirrorError) as e:
        m.create("urn:li:corpuser:ops", {"name": "mg_x", "connection": "shop", "table": "shop.nothing",
                                         "target": "shop_mg.x"})
    assert e.value.status == 409 and "no collection" in str(e.value)
    with pytest.raises(MirrorError) as e:
        m.create("urn:li:corpuser:ops", {"name": "mg_y", "connection": "single", "table": "shop.carts",
                                         "target": "shop_mg.y"})
    assert e.value.status == 409 and "replica set" in str(e.value)
