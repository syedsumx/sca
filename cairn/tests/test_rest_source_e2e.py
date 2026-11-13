"""The declarative REST source (L165) end to end: a JSON API described by a spec lands in a governed Iceberg table in
Cairn Catalog (on Cairn Store), incrementally or whole, and is read through Trino under Cairn's policies. The API
is a local server answering real HTTP (tests/rest_api_standin.py), not a vendor's; its key is in a real secret
scope."""

from __future__ import annotations

import json

import pytest

from rest_api_standin import Api, customer, serve
from test_saas_e2e import OWNER, world  # noqa: F401  (fixture)
from test_trino_e2e import _ready, cairn, lake, port, trino  # noqa: F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker, the store and catalog binaries")
BOB = "urn:li:corpuser:bob"
SPEC = {"records": "data.items", "modified": "updated_at", "cursor_param": "updated_since",
        "pagination": {"type": "cursor", "param": "starting_after", "path": "meta.next"}}


@pytest.fixture
def api(world, monkeypatch):  # noqa: F811
    monkeypatch.setenv("CAIRN_SAAS_ALLOW_PRIVATE_NETWORKS", "true")       # the API is on this machine
    a = Api()
    p = port()
    server = serve(a, p)
    a.base = f"http://127.0.0.1:{p}"
    scopes = world["scopes"]
    scopes.create(OWNER, {"name": "api-keys"})
    scopes.put(OWNER, "api-keys", "base_url", f"{a.base}/bearer-cursor")
    scopes.put(OWNER, "api-keys", "token", a.token)
    yield a
    server.shutdown()


def test_a_rest_api_lands_incrementally_in_a_governed_table(world, api, trino):  # noqa: F811
    syncs = world["syncs"]
    for i, (region, day) in enumerate([("EU", 1), ("US", 1), ("EU", 2), ("US", 2), ("EU", 3)]):
        api.customers[str(i)] = {**customer(i, day, city="Lyon" if region == "EU" else "Austin"), "region": region}
    api.shuffle = True                                   # the API answers newest first: nothing may be skipped
    trino("DROP TABLE IF EXISTS lake.sales.api_customers")
    body = {"name": "api-customers", "kind": "rest", "object": "customers", "fields": ["name", "region", "address.city", "vip"],
            "target": "sales.api_customers", "secret_scope": "api-keys", "spec": SPEC, "cron": "0 * * * *"}
    saved = syncs.save(OWNER, body)
    assert saved["spec"]["pagination"]["type"] == "cursor" and saved["cursor"] is None
    first = syncs.run_now(OWNER, "api-customers", wait=True)
    assert first["status"] == "ok" and first["rows"] == 5, first
    rows = trino("SELECT id, name, region, address_city, vip, _modified FROM lake.sales.api_customers ORDER BY id")
    assert [r["id"] for r in rows] == [f"cus_{i:03d}" for i in range(5)]
    assert rows[0]["address_city"] == "Lyon" and rows[1]["address_city"] == "Austin" and rows[0]["vip"] == "true"
    # Governed like any sales table: bob's row filter (region = 'EU') applies to what was loaded.
    assert sorted(r["name"] for r in trino("SELECT name FROM lake.sales.api_customers", user="bob")) == \
        ["Customer 0", "Customer 2", "Customer 4"]
    # The cursor is the newest modification time of the whole run, though the newest came first.
    assert syncs.get(OWNER, "api-customers")["cursor"] == "2026-11-03T10:00:00Z"

    # One changes, one is new: the next run asks from the cursor and upserts by id.
    api.customers["1"] = {**customer(1, 4, name="Customer One (renamed)", city="Austin"), "region": "US"}
    api.customers["9"] = {**customer(9, 4), "region": "EU"}
    api.asked.clear()
    second = syncs.run_now(OWNER, "api-customers", wait=True)
    assert second["status"] == "ok" and second["rows"] == 3, second           # the two, and the one at the cursor again
    assert all("updated_since=2026-11-03T10" in a for a in api.asked)
    rows = trino("SELECT id, name FROM lake.sales.api_customers ORDER BY id")
    assert len(rows) == 6 and len({r["id"] for r in rows}) == 6
    assert {r["name"] for r in rows} >= {"Customer One (renamed)", "Customer 9"}

    # A run that fails part-way keeps what it loaded and does not move the cursor, so nothing is skipped next time.
    api.customers["10"] = {**customer(10, 6), "region": "EU"}
    api.customers["11"] = {**customer(11, 5), "region": "EU"}
    api.customers["12"] = {**customer(12, 5), "region": "US"}
    syncs.batch = 2                                      # commit after each page
    calls = {"n": 0}
    real_rows = api.rows

    def flaky(since):
        calls["n"] += 1
        if calls["n"] == 2:
            api.fail[:] = [500, 500, 500, 500]           # the second page: the API is down
        return real_rows(since)
    api.rows = flaky
    failed = syncs.run_now(OWNER, "api-customers", wait=True)
    assert failed["status"] == "failed" and "500" in failed["error"], failed
    assert syncs.get(OWNER, "api-customers")["cursor"] == "2026-11-04T10:00:00Z"       # where it was
    assert world["told"] and world["told"][-1][0] == "saas.sync_failed"
    api.rows, api.fail[:] = real_rows, []
    again = syncs.run_now(OWNER, "api-customers", wait=True)
    assert again["status"] == "ok", again
    ids = {r["id"] for r in trino("SELECT id FROM lake.sales.api_customers")}
    assert ids >= {"cus_010", "cus_011", "cus_012"} and len(ids) == 9
    assert syncs.get(OWNER, "api-customers")["cursor"] == "2026-11-06T10:00:00Z"
    assert api.token not in json.dumps(world["audited"], default=str)             # the key is never audited
    trino("DROP TABLE lake.sales.api_customers")


def test_without_a_modification_time_the_table_follows_the_api(world, api, trino):  # noqa: F811
    syncs, scopes = world["syncs"], world["scopes"]
    scopes.put(OWNER, "api-keys", "base_url", f"{api.base}/bearer-page")
    for i in range(5):
        api.customers[str(i)] = customer(i, 1)
    trino("DROP TABLE IF EXISTS lake.sales.api_all")
    syncs.save(OWNER, {"name": "api-all", "kind": "rest", "object": "customers", "fields": ["name", "status"],
                       "target": "sales.api_all", "secret_scope": "api-keys",
                       "spec": {"records": "results", "pagination": {"type": "page", "size": 2}}})
    assert syncs.run_now(OWNER, "api-all", wait=True)["rows"] == 5
    del api.customers["4"]                               # gone from the API: gone from the table
    api.customers["0"]["name"] = "Renamed"
    out = syncs.run_now(OWNER, "api-all", wait=True)
    assert out["status"] == "ok" and out["rows"] == 4 and out["cursor"] is None
    rows = trino("SELECT id, name FROM lake.sales.api_all ORDER BY id")
    assert [r["id"] for r in rows] == [f"cus_{i:03d}" for i in range(4)] and rows[0]["name"] == "Renamed"
    trino("DROP TABLE lake.sales.api_all")


def test_a_rest_sync_is_checked_when_saved_and_when_run(world, api, monkeypatch):  # noqa: F811
    from cairn.pipelines.saas import SaasError

    syncs = world["syncs"]
    ok = {"name": "x", "kind": "rest", "object": "customers", "fields": ["name"], "target": "sales.x",
          "secret_scope": "api-keys", "spec": {"records": ""}}
    for change, why in (({"object": "../admin"}, "object"), ({"object": "customers?x=1"}, "object"),
                        ({"object": "https://evil.example.com/x"}, "object"),
                        ({"fields": ["a b"]}, "fields"), ({"fields": ["address.city", "address_city"]}, "column"),
                        ({"fields": ["id"]}, "column"), ({"spec": None}, "spec"),
                        ({"spec": {"auth": {"type": "header", "name": "Host"}}}, "spec.auth"),
                        ({"kind": "hubspot", "object": "contacts"}, "only for kind rest")):
        with pytest.raises(SaasError, match=why):
            syncs.save(OWNER, {**ok, **change})
    with pytest.raises(SaasError) as e:                  # someone who may not write the table
        syncs.save(BOB, ok)
    assert e.value.status == 403
    # Private addresses are refused unless an operator allows them (the default): the run fails, nothing is fetched.
    syncs.save(OWNER, ok)
    monkeypatch.delenv("CAIRN_SAAS_ALLOW_PRIVATE_NETWORKS")
    api.asked.clear()
    out = syncs.run_now(OWNER, "x", wait=True)
    assert out["status"] == "failed" and "private" in out["error"] and api.asked == []
    # Changing how the API is read starts from the beginning.
    with world["syncs"].engine.begin() as conn:
        from sqlalchemy import update

        from cairn.pipelines.saas import saas_syncs

        conn.execute(update(saas_syncs).where(saas_syncs.c.name == "x").values(cursor="2026-01-01"))
    changed = syncs.save(OWNER, {**ok, "spec": {"records": "", "params": {"status": "active"}}}, "x")
    assert changed["cursor"] is None
