"""Share observability, end to end: a real recipient reads through pyiceberg from Cairn Catalog (which asks Cairn
about every request) and Cairn counts it per share and table: requests, table loads, refusals, failures, tokens and
the last access, kept for a retention period. The overview finds unused shares, shares expiring soon and recipients
reading tables whose quality is failing (the quality module's evaluations), and the share's maker is notified once.

Needs the store and catalog binaries (CAIRN_STORE_BIN, CAIRN_CATALOG_BIN)."""

import time

import httpx
import pytest

pa = pytest.importorskip("pyarrow")

from test_shares_e2e import api, recipient  # noqa: E402
from test_spark_e2e import ANA, BOB, lake, people  # noqa: E402, F401  (fixtures)
from test_trino_e2e import _ready, cairn  # noqa: E402, F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs the store and catalog binaries")


def test_share_usage_is_recorded_and_watched(cairn, lake):  # noqa: F811
    from sqlalchemy import select, update

    from cairn.dataaccess.shares import lakehouse_shares, share_usage
    from cairn.notify.hub import notifications
    from cairn.quality.models import Evaluation, MonitorSpec

    url, s = cairn
    lake["admin"].create_table("analytics.private", schema=pa.schema([("x", pa.int64())]))
    r = api(url, ANA, "POST", "/v1/shares", json={"name": "usage-a", "recipient": "Partner U (data@u.example)",
                                                  "tables": ["analytics.products"], "expires_days": 30})
    assert r.status_code == 200, r.text
    share = r.json()

    # The recipient lists, loads and reads the shared table, and tries one beside it that it was not given.
    cat = recipient(url, lake["catalog"], share["credential"])
    assert cat.list_namespaces() == [("analytics",)]
    assert cat.list_tables("analytics") == [("analytics", "products")]
    rows = cat.load_table("analytics.products").scan().to_arrow().to_pylist()
    assert len(rows) == 3
    from pyiceberg.exceptions import ForbiddenError, NoSuchTableError
    with pytest.raises((ForbiddenError, NoSuchTableError)):
        cat.load_table("analytics.private")
    sid, _ = share["credential"].split(":", 1)
    bad = httpx.post(f"{url}/v1/shares/oauth/tokens", data={"grant_type": "client_credentials", "client_id": sid,
                                                            "client_secret": "wrong"}, timeout=10)
    assert bad.status_code == 401

    # By name or id, for its maker (and operators) only.
    u = api(url, ANA, "GET", "/v1/shares/usage-a/usage")
    assert u.status_code == 200, u.text
    u = u.json()
    assert api(url, ANA, "GET", f"/v1/shares/{share['id']}/usage").json()["totals"] == u["totals"]
    assert api(url, BOB, "GET", "/v1/shares/usage-a/usage").status_code == 404
    t = u["totals"]
    assert t["tokens"] >= 1 and t["requests"] >= 3 and t["loads"] >= 1 and t["refusals"] >= 1 and t["failures"] == 1, t
    assert u["tables_read"] == ["analytics.products"] and t["tables_read"] == 1
    products = next(x for x in u["tables"] if x["table"] == "analytics.products")
    assert products["loads"] >= 1 and products["last_access_ms"] > (time.time() - 300) * 1000
    assert next(x for x in u["tables"] if x["table"] == "analytics.private")["refusals"] >= 1
    assert u["last_access_ms"] and u["by_day"] and u["recipient"] == "Partner U (data@u.example)"

    # The overview: a second share nobody used for 40 days that ends in 2, and failing quality on a table read.
    r = api(url, ANA, "POST", "/v1/shares", json={"name": "usage-b", "recipient": "Partner V",
                                                  "tables": ["analytics.products"], "expires_days": 30})
    quiet = r.json()
    now = int(time.time())
    with s.engine.begin() as conn:
        conn.execute(update(lakehouse_shares).where(lakehouse_shares.c.id == quiet["id"])
                     .values(created_s=now - 40 * 86400, expires_s=now + 2 * 86400))
    products_urn = s.lakehouse.urn(["analytics"], "products")
    mon = s.quality.store.create_monitor(MonitorSpec(name="products volume", dataset_urn=products_urn, type="volume"),
                                         "urn:li:corpuser:ana")
    s.quality.store.add_evaluation(Evaluation(
        id="ev-share-usage-1", monitor_id=mon.id, ts="2026-11-08T00:00:00Z", ts_ms=int(time.time() * 1000), run_id="r1",
        metric_value=0.0, expected_low=10.0, expected_high=100.0, status="FAIL", score=1.0, message="no rows"), None)
    o = api(url, ANA, "GET", "/v1/shares/usage/overview")
    assert o.status_code == 200, o.text
    o = o.json()
    assert "usage-b" in o["unused"] and "usage-a" not in o["unused"]
    assert o["expiring_soon"] == ["usage-b"]
    assert o["quality_failing"] == [{"share": "usage-a", "recipient": "Partner U (data@u.example)", "tables": [
        {"table": "analytics.products", "urn": products_urn, "status": "FAILING"}]}]
    assert api(url, BOB, "GET", "/v1/shares/usage/overview").json()["shares"] == []

    # The maker is told once: expiring and unused for usage-b, nothing for the share in use.
    sent = s.shares.sweep()
    assert sorted((x["share"], x["kind"]) for x in sent) == [("usage-b", "expiring"), ("usage-b", "unused")]
    assert s.shares.sweep() == []
    with s.engine.connect() as conn:
        types = {r.type for r in conn.execute(select(notifications.c.type).where(
            notifications.c.recipient == "urn:li:corpuser:ana"))}
    assert {"share.expiring", "share.unused"} <= types

    # Retention: older days are dropped.
    with s.engine.begin() as conn:
        conn.execute(share_usage.insert().values(share_id=share["id"], day=20200101, table_name="", requests=1, loads=0,
                                                 refusals=0, failures=0, tokens=0, last_ms=1))
    s.shares._pruned = 0.0
    s.shares._count(share["id"], "", requests=1)
    s.shares.flush_usage()
    with s.engine.connect() as conn:
        assert conn.execute(select(share_usage.c.day).where(share_usage.c.day == 20200101)).first() is None
