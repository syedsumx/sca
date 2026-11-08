"""Streaming transformations (L104): a SELECT applied to each micro-batch of an HTTP ingest stream before it lands, with
the real Cairn Store and Cairn Catalog, read back through a real Trino. Checked: the transform is validated when the
stream is defined (one row-for-row SELECT over batch, nothing else read, key kept, functions that exist, no file
access); rows land transformed in the same commit as the request; filtered rows are skipped and failing rows go to
the rejects with the reason while the rest commit; a request whose rows are all filtered still moves the channel's
offset on; retries write nothing twice; upserts keep their key through the transform. Needs docker and the store
and catalog binaries."""

import time
from decimal import Decimal

import pytest

pytest.importorskip("duckdb")

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, cairn, lake, trino  # noqa: E402, F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")

CLEAN = ("SELECT id, lower(email) AS email, CAST(amount AS DECIMAL(12, 2)) AS amount, "
         "CAST(ts AS TIMESTAMP) AS ts, upper(geo.country) AS country FROM batch WHERE status <> 'test'")


@pytest.fixture(scope="module")
def api(lake, tmp_path_factory):  # noqa: F811
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from starlette.requests import Request

    from cairn.common.db import init_db
    from cairn.common.security import Principal
    from cairn.pipelines.streams import Streams, build_ingest_router, build_streams_router

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('transform')}/cairn.db")
    init_db(engine)
    s = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                             "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                             "s3.region": "us-east-1", "s3.path-style-access": "true"})

    def authorize(p, privilege, urn, raise_on_deny=True):
        ok = p.urn == "urn:li:corpuser:ops"
        if not ok and raise_on_deny:
            raise HTTPException(403, f"{privilege} needed")
        return ok

    def who(request: Request) -> Principal:
        return Principal("urn:li:corpuser:" + request.headers["x-user"], "token")
    app = FastAPI()
    app.include_router(build_ingest_router(s, who, authorize))
    app.include_router(build_streams_router(s, who, authorize))
    return {"s": s, "c": TestClient(app)}


def define(api, **body):
    return api["c"].post("/v1/streams", json={"format": "http", "create_table": True, **body}, headers={"x-user": "ops"})


def post(api, name, **body):
    r = api["c"].post(f"/v1/ingest/{name}/rows", json=body, headers={"x-user": "ops"})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_transform_is_checked_when_the_stream_is_defined(api):
    for sql, why in [("SELECT region, count(*) AS n FROM batch GROUP BY region", "not allowed"),
                     ("SELECT b.id FROM batch b JOIN rt.customers c ON b.id = c.id", "not allowed"),
                     ("SELECT id FROM rt.customers", "FROM batch"),
                     ("SELECT * FROM read_csv('/etc/passwd')", "FROM batch"),
                     ("SELECT id, (SELECT 1) AS one FROM batch", "subqueries"),
                     ("SELECT id, upper(email) FROM batch", "AS"),
                     ("SELECT id, no_such_function(email) AS e FROM batch", "does not run"),
                     ("SELECT id, getenv('HOME') AS home FROM batch", "does not run"),
                     ("DELETE FROM batch", "one SELECT"),
                     ("SELECT id FROM batch; SELECT 1", "one SELECT")]:
        r = define(api, name="bad", target="rt.bad", transform=sql)
        assert r.status_code == 400 and why in r.json()["detail"], (sql, r.text)
    r = define(api, name="bad", target="rt.bad", mode="upsert", key=["id"], transform="SELECT email FROM batch")
    assert r.status_code == 400 and "key column id" in r.json()["detail"]
    r = define(api, name="bad", format="debezium", topic="t", target="rt.bad", key=["id"], transform="SELECT * FROM batch")
    assert r.status_code == 400 and "json and http" in r.json()["detail"]


def test_rows_land_transformed_within_the_request_and_bad_rows_are_set_aside(api, trino):  # noqa: F811
    r = define(api, name="orders-clean", target="rt.orders", transform=CLEAN, bad_rows="quarantine")
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert api["s"].get(sid)["transform"] == CLEAN
    rows = [{"id": 1, "email": "Ana@Example.COM", "amount": "12.5", "ts": "2026-11-08 10:00:00", "status": "paid",
             "geo": {"country": "pt", "city": "Porto"}},
            {"id": 2, "email": "QA@example.com", "amount": "1", "ts": "2026-11-08 10:01:00", "status": "test",
             "geo": {"country": "de", "city": "Berlin"}},                                   # filtered
            {"id": 3, "email": "Bo@Example.com", "amount": "twelve", "ts": "2026-11-08 10:02:00", "status": "paid",
             "geo": {"country": "fr", "city": "Lyon"}},                                     # the cast fails: set aside
            {"id": 4, "email": "Li@Example.com", "amount": 7, "ts": "2026-11-08 10:03:00", "status": "new",
             "geo": {"country": "es", "city": "Vigo"}}]
    t0 = time.time()
    assert post(api, "orders-clean", channel="edge", offset=0, rows=rows) == \
        {"written": 2, "duplicates": 0, "rejected": 1, "filtered": 1, "next_offset": 4}
    got = trino("SELECT id, email, amount, ts, country FROM lake.rt.orders ORDER BY id")
    seconds = time.time() - t0
    assert [(r["id"], r["email"], Decimal(str(r["amount"])), r["country"]) for r in got] == \
        [(1, "ana@example.com", Decimal("12.50"), "PT"), (4, "li@example.com", Decimal("7.00"), "ES")]
    assert str(got[0]["ts"]).startswith("2026-11-08 10:00:00")
    print(f"\nposted to readable through Trino in {seconds:.1f} s")
    assert seconds < 60
    cols = {r["Column"]: r["Type"] for r in trino("DESCRIBE lake.rt.orders")}
    assert cols == {"id": "bigint", "email": "varchar", "amount": "decimal(12,2)", "ts": "timestamp(6)",
                    "country": "varchar"}                 # the transform's types, not the raw JSON's
    rej = api["c"].get(f"/v1/streams/{sid}/rejects", headers={"x-user": "ops"}).json()
    assert len(rej) == 1 and rej[0]["offset"] == 2 and "Could not convert" in rej[0]["reason"]
    assert '"twelve"' in rej[0]["value"]                  # the row as it arrived, to see what it was

    # A retry writes nothing twice; a request whose rows are all filtered still moves the channel on.
    assert post(api, "orders-clean", channel="edge", offset=0, rows=rows)["duplicates"] == 4
    assert post(api, "orders-clean", channel="edge", offset=4, rows=[
        {"id": 5, "email": "x@y", "amount": 1, "ts": "2026-11-08 11:00:00", "status": "test", "geo": {"country": "it"}}]) == \
        {"written": 0, "duplicates": 0, "rejected": 0, "filtered": 1, "next_offset": 5}
    assert post(api, "orders-clean", channel="edge", offset=5, rows=[
        {"id": 6, "email": "Zoe@Y", "amount": "3.333", "ts": "2026-11-08 11:05:00", "status": "paid",
         "geo": {"country": "nl"}}])["next_offset"] == 6
    assert [(r["id"], r["email"]) for r in trino("SELECT id, email FROM lake.rt.orders WHERE id > 4")] == [(6, "zoe@y")]


def test_upserts_keep_their_key_through_the_transform(api, trino):  # noqa: F811
    r = define(api, name="customers", target="rt.customers", mode="upsert", key=["id"],
               transform="SELECT id, trim(name) AS name, coalesce(tier, 'basic') AS tier FROM batch WHERE id IS NOT NULL")
    assert r.status_code == 201, r.text
    post(api, "customers", channel="crm", rows=[{"id": 1, "name": " Ana ", "tier": None}, {"id": 2, "name": "Bo  "}])
    post(api, "customers", channel="crm", rows=[{"id": 1, "name": "Ana Lima", "tier": "gold"}, {"name": "no id"}])
    got = trino("SELECT id, name, tier FROM lake.rt.customers ORDER BY id")
    assert [(r["id"], r["name"], r["tier"]) for r in got] == [(1, "Ana Lima", "gold"), (2, "Bo", "basic")]
    # Changing the transform is checked the same way.
    sid = r.json()["id"]
    bad = api["c"].put(f"/v1/streams/{sid}", json={"name": "customers", "format": "http", "target": "rt.customers",
                                                   "mode": "upsert", "key": ["id"], "transform": "SELECT name FROM batch"},
                       headers={"x-user": "ops"})
    assert bad.status_code == 400 and "key column id" in bad.json()["detail"]


def test_the_engine_is_sandboxed():
    from cairn.pipelines.stream_transform import _connect

    con = _connect()
    for sql, why in [("SELECT * FROM read_csv('/etc/passwd')", "disabled"),
                     ("COPY (SELECT 1) TO '/tmp/cairn-x.csv'", "disabled"),
                     ("SET enable_external_access = true", "configuration"),
                     ("INSTALL httpfs", None)]:
        with pytest.raises(Exception, match=why):
            con.execute(sql)
    con.close()
