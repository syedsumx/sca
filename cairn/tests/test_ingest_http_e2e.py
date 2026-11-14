"""Rows over HTTP into lakehouse tables (http streams), with the real Cairn Store and Cairn Catalog: each
request is one Iceberg commit; a channel's offset lives in the table's snapshots, so a retried request
writes nothing twice and a producer resumes where the table says; upserts keep the latest row per key;
rows that do not fit are set aside; only those allowed may post. Needs the store and catalog binaries
(and docker, like the rest of the lakehouse tests)."""

import pytest

from test_streams_e2e import table_rows
from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")


@pytest.fixture(scope="module")
def api(lake, tmp_path_factory):  # noqa: F811
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from starlette.requests import Request

    from cairn.common.db import init_db
    from cairn.common.security import Principal
    from cairn.pipelines.streams import Streams, build_ingest_router

    engine = create_engine(f"sqlite:///{tmp_path_factory.mktemp('ingest')}/cairn.db")
    init_db(engine)
    s = Streams(engine, {}, {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
                             "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1],
                             "s3.region": "us-east-1", "s3.path-style-access": "true"})
    grants = {("ops", "MANAGE_INGESTION", None)}

    def authorize(p, privilege, urn, raise_on_deny=True):
        ok = (p.urn.split(":")[-1], privilege, urn) in grants
        if not ok and raise_on_deny:
            raise HTTPException(403, f"{privilege} needed")
        return ok

    def who(request: Request) -> Principal:
        return Principal("urn:li:corpuser:" + request.headers["x-user"], "token")
    app = FastAPI()
    app.include_router(build_ingest_router(s, who, authorize))
    return {"s": s, "c": TestClient(app), "grants": grants}


def post(api, user, name, **body):
    return api["c"].post(f"/v1/ingest/{name}/rows", json=body, headers={"x-user": user})


def test_rows_land_exactly_once_per_channel(api):
    s = api["s"]
    s.create({"name": "web-events", "format": "http", "target": "web.events", "create_table": True}, "urn:li:corpuser:ops")
    rows = [{"id": i, "page": f"/p{i}"} for i in range(3)]
    r = post(api, "ops", "web-events", channel="edge-1", offset=0, rows=rows)
    assert r.status_code == 200, r.text
    assert r.json() == {"written": 3, "duplicates": 0, "rejected": 0, "next_offset": 3}
    # The same request again (a retry after a lost answer): nothing twice.
    assert post(api, "ops", "web-events", channel="edge-1", offset=0, rows=rows).json() == \
        {"written": 0, "duplicates": 3, "rejected": 0, "next_offset": 3}
    # Overlapping: the committed part is skipped, the rest written.
    r = post(api, "ops", "web-events", channel="edge-1", offset=2, rows=[{"id": 2, "page": "/p2"}, {"id": 3, "page": "/p3"}])
    assert r.json() == {"written": 1, "duplicates": 1, "rejected": 0, "next_offset": 4}
    # A gap is refused; another channel has its own offsets; a producer resumes from the table.
    assert post(api, "ops", "web-events", channel="edge-1", offset=9, rows=rows).status_code == 409
    assert post(api, "ops", "web-events", channel="edge-2", offset=0, rows=[{"id": 100, "page": "/x"}]).json()["written"] == 1
    assert api["c"].get("/v1/ingest/web-events/channels/edge-1", headers={"x-user": "ops"}).json() == \
        {"channel": "edge-1", "next_offset": 4}
    assert sorted(r["id"] for r in table_rows(s, "web.events")) == [0, 1, 2, 3, 100]
    # A row that does not fit is set aside with the reason; the others are written.
    r = post(api, "ops", "web-events", channel="edge-1", offset=4, rows=[{"id": 4, "page": "/p4"}, {"id": 5, "nope": 1}])
    assert r.json() == {"written": 1, "duplicates": 0, "rejected": 1, "next_offset": 6}
    sid = s.by_name("web-events")["id"]
    assert any("[edge-1]" in x["reason"] and "nope" in x["reason"] for x in s.rejects(sid))
    # Bad requests.
    assert post(api, "ops", "web-events", channel="bad channel!", rows=rows).status_code == 400
    assert post(api, "ops", "web-events", channel="edge-1", rows=["not an object"]).status_code == 400
    assert post(api, "ops", "nope", channel="edge-1", rows=rows).status_code == 404
    # Without an offset, rows are appended at the channel's offset (at least once).
    assert post(api, "ops", "web-events", channel="edge-3", rows=[{"id": 7, "page": "/7"}]).json()["next_offset"] == 1


def test_upserts_keep_the_latest_row_and_only_the_allowed_post(api):
    s = api["s"]
    s.create({"name": "accounts", "format": "http", "mode": "upsert", "key": ["id"], "target": "crm.accounts",
              "create_table": True}, "urn:li:corpuser:ops")
    post(api, "ops", "accounts", channel="sync", offset=0,
         rows=[{"id": 1, "tier": "free"}, {"id": 2, "tier": "free"}])
    post(api, "ops", "accounts", channel="sync", offset=2, rows=[{"id": 1, "tier": "pro"}, {"id": 1, "tier": "team"}])
    assert table_rows(s, "crm.accounts") == [{"id": 1, "tier": "team"}, {"id": 2, "tier": "free"}]
    # bob may not post until a policy says so, and then only to this stream.
    assert post(api, "bob", "accounts", channel="b", rows=[{"id": 3, "tier": "free"}]).status_code == 403
    api["grants"].add(("bob", "INGEST_STREAM", "urn:li:stream:accounts"))
    assert post(api, "bob", "accounts", channel="b", rows=[{"id": 3, "tier": "free"}]).status_code == 200
    assert post(api, "bob", "web-events", channel="b", rows=[{"id": 3}]).status_code == 403
    # Kafka streams take no posted rows; a paused stream takes none either.
    s.set_enabled(s.by_name("accounts")["id"], False, "urn:li:corpuser:ops")
    assert post(api, "ops", "accounts", channel="sync", offset=4, rows=[{"id": 9, "tier": "x"}]).status_code == 409
