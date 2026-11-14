"""Online feature store, with the real Cairn Store and Cairn Catalog: a feature table's owners serve it online,
Cairn fills it from the lakehouse table and keeps it in step (only changed entities written), and each lookup
is governed as a read of the table: no read, no values; masks applied as Trino applies them; a row filter
refuses the lookup. Needs the store and catalog binaries (and docker, like the rest of the lakehouse tests)."""

import datetime as dt
import json
import os
from decimal import Decimal

import pytest

pa = pytest.importorskip("pyarrow")

from test_trino_e2e import CATALOG_TOKEN, STORE_KEY, _ready, lake  # noqa: E402, F401  (fixtures)

pytestmark = pytest.mark.skipif(not _ready(), reason="needs docker and the store and catalog binaries")

DS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,shop.customer_stats,PROD)"
PII = "urn:li:glossaryTerm:Classification.PII.Email"
ANA, BOB, CAL, EVE = (f"urn:li:corpuser:{u}" for u in ("ana", "bob", "cal", "eve"))


@pytest.fixture(scope="module")
def online(lake, tmp_path_factory):  # noqa: F811
    from fastapi.testclient import TestClient
    from pyiceberg.catalog.rest import RestCatalog

    from cairn.app import create_api_app
    from cairn.common.config import Settings
    from cairn.common.core import InMemoryGraph
    from cairn.governance.abac import Obligation, Policy, PolicyBundle, Target
    from cairn.services import build_services

    props = {"uri": lake["catalog"], "token": CATALOG_TOKEN, "s3.endpoint": lake["store"],
             "s3.access-key-id": STORE_KEY[0], "s3.secret-access-key": STORE_KEY[1], "s3.region": "us-east-1",
             "s3.path-style-access": "true"}
    cat = RestCatalog("test", **props)
    cat.create_namespace_if_not_exists("shop")
    schema = pa.schema([("customer_id", pa.int64()), ("email", pa.string()), ("orders_90d", pa.int32()),
                        ("spend_90d", pa.float64()), ("credit", pa.decimal128(10, 2)), ("first_order", pa.date32()),
                        ("updated_at", pa.timestamp("us", tz="UTC"))])
    t0 = dt.datetime(2026, 11, 1, 12, tzinfo=dt.timezone.utc)
    rows = [{"customer_id": 1, "email": "kim@acme.com", "orders_90d": 4, "spend_90d": 123.5, "credit": Decimal("1250.75"),
             "first_order": dt.date(2024, 3, 17), "updated_at": t0},
            {"customer_id": 1, "email": "kim@acme.com", "orders_90d": 7, "spend_90d": 456.25, "credit": Decimal("1250.75"),
             "first_order": dt.date(2024, 3, 17), "updated_at": t0 + dt.timedelta(hours=1)},     # Kim's latest
            {"customer_id": 2, "email": "lee@acme.com", "orders_90d": 1, "spend_90d": 9.99, "credit": Decimal("0.00"),
             "first_order": dt.date(2025, 7, 2), "updated_at": t0},
            {"customer_id": 3, "email": "max@acme.com", "orders_90d": 0, "spend_90d": 0.0, "credit": Decimal("88.10"),
             "first_order": dt.date(2026, 1, 9), "updated_at": dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)}]  # stale
    tbl = cat.create_table("shop.customer_stats", schema=schema)
    tbl.append(pa.Table.from_pylist(rows, schema=schema))

    os.environ["CAIRN_ONLINE_FEATURES_CATALOG"] = json.dumps(props)
    tmp = tmp_path_factory.mktemp("online")
    try:
        graph = InMemoryGraph()
        graph.aspects[(DS, "schemaMetadata")] = {"fields": [
            {"fieldPath": "customer_id", "nativeDataType": "bigint"},
            {"fieldPath": "email", "nativeDataType": "varchar", "glossaryTerms": {"terms": [{"urn": PII}]}},
            {"fieldPath": "orders_90d", "nativeDataType": "int"}, {"fieldPath": "spend_90d", "nativeDataType": "double"},
            {"fieldPath": "credit", "nativeDataType": "decimal(10,2)"}, {"fieldPath": "first_order", "nativeDataType": "date"},
            {"fieldPath": "updated_at", "nativeDataType": "timestamp"}]}
        s = build_services(Settings(database_url=f"sqlite:///{tmp}/cairn.db", service_token="svc-secret",
                                    dev_trust_actor_header=True, gateway={"key_dir": str(tmp / "keys")}),
                           graph=graph, with_ai=False, with_gateway=False)
        sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
        anyof = lambda *us: {"any": [sub(u) for u in us]}  # noqa: E731
        s.pdp.set_bundle(PolicyBundle(policies=[
            Policy(id="ana-owns", effect="allow", actions=["MANAGE_DATASET", "VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
                   target=Target(resource_urns=["*shop.*"]), condition=sub(ANA)),
            Policy(id="readers", effect="allow", actions=["VIEW_ENTITY_PAGE", "VIEW_DATASET_PROFILE"],
                   target=Target(resource_urns=["*shop.*"]), condition=anyof(BOB, CAL)),
            Policy(id="eve-sees", effect="allow", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=["*shop.*"]),
                   condition=sub(EVE)),
            Policy(id="mask-bob", effect="obligation", actions=["VIEW_DATASET_PROFILE"], condition=sub(BOB),
                   obligations=[Obligation(type="mask_columns", params={"labels": ["PII"], "strategy": "partial"})]),
            Policy(id="cal-eu", effect="obligation", actions=["VIEW_DATASET_PROFILE"], target=Target(resource_urns=["*shop.*"]),
                   obligations=[Obligation(type="row_filter", params={"expression": "region = 'EU'"})], condition=sub(CAL))]))
        assert s.online_features is not None
        app = create_api_app(s)
        clients = {u: TestClient(app, headers={"X-Cairn-Actor": u}) for u in (ANA, BOB, CAL, EVE)}
        r = clients[ANA].post("/v1/ml/feature-tables", json={"name": "customer_stats", "table": "shop.customer_stats",
                                                              "keys": ["customer_id"]})
        assert r.status_code == 201, r.text
        yield s, clients, tbl, schema
    finally:
        os.environ.pop("CAIRN_ONLINE_FEATURES_CATALOG", None)


def lookup(c, ids, **kw):
    return c.post("/v1/ml/feature-tables/customer_stats/online/lookup",
                  json={"entities": [{"customer_id": i} for i in ids], **kw})


def test_owners_serve_a_feature_table_online_and_it_keeps_in_step(online):
    s, c, tbl, schema = online
    # Only its owners turn it on; it is filled at once, the latest row per entity winning.
    assert c[BOB].put("/v1/ml/feature-tables/customer_stats/online", json={}).status_code == 403
    assert lookup(c[ANA], [1]).status_code == 404                                    # not served yet
    bad = c[ANA].put("/v1/ml/feature-tables/customer_stats/online", json={"timestamp_column": "nope"})
    assert bad.status_code == 400 and "no column nope" in bad.json()["detail"]
    assert c[ANA].get("/v1/ml/feature-tables/customer_stats/online").status_code == 404   # nothing half-made
    r = c[ANA].put("/v1/ml/feature-tables/customer_stats/online",
                   json={"timestamp_column": "updated_at", "ttl_seconds": 365 * 86400})
    assert r.status_code == 200, r.text
    assert r.json()["entities"] == 3 and r.json()["source_snapshot"] == tbl.current_snapshot().snapshot_id

    out = lookup(c[ANA], [1, 2, 3, 99]).json()
    by = {x["key"]["customer_id"]: x for x in out["results"]}
    assert by[1]["status"] == "found" and by[1]["features"]["orders_90d"] == 7          # the latest row
    assert by[1]["features"]["email"] == "kim@acme.com" and by[1]["features"]["credit"] == "1250.75"
    assert by[1]["features"]["first_order"] == "2024-03-17" and out["masked"] == []
    assert by[2]["features"]["spend_90d"] == 9.99
    assert by[3]["status"] == "expired" and by[3]["features"]["orders_90d"] is None     # older than its TTL
    assert by[99]["status"] == "missing"
    # Keys as the caller has them: "2" finds the bigint key 2; a subset of features.
    sub = c[ANA].post("/v1/ml/feature-tables/customer_stats/online/lookup",
                      json={"entities": [{"customer_id": "2"}], "features": ["orders_90d"]}).json()["results"][0]
    assert sub["status"] == "found" and sub["features"] == {"orders_90d": 1}

    # Nothing moved: nothing read. Then the table changes: one entity updated, one removed, one added.
    assert s.online_features.sync("customer_stats")["changed"] is False
    assert s.online_features.run_due() == 0
    rows = [r for r in tbl.scan().to_arrow().to_pylist() if r["customer_id"] != 2]
    now = dt.datetime.now(dt.timezone.utc)
    rows.append({**rows[0], "customer_id": 1, "orders_90d": 8, "updated_at": now})
    rows.append({**rows[0], "customer_id": 4, "email": "ola@acme.com", "orders_90d": 2, "updated_at": now})
    tbl.overwrite(pa.Table.from_pylist(rows, schema=schema))
    assert s.online_features.run_due() == 1                                      # the scheduler saw the new snapshot
    st = c[ANA].get("/v1/ml/feature-tables/customer_stats/online").json()
    assert st["entities"] == 3 and st["source_snapshot"] == tbl.current_snapshot().snapshot_id
    by = {x["key"]["customer_id"]: x for x in lookup(c[ANA], [1, 2, 4]).json()["results"]}
    assert by[1]["features"]["orders_90d"] == 8 and by[2]["status"] == "missing" and by[4]["features"]["orders_90d"] == 2
    forced = c[ANA].post("/v1/ml/feature-tables/customer_stats/online/sync").json()
    assert forced["changed"] and forced["written"] == 0 and forced["removed"] == 0      # nothing to write

    with s.engine.connect() as conn:
        from sqlalchemy import func, select

        from cairn.ml.online import online_feature_values
        assert conn.execute(select(func.count()).select_from(online_feature_values)).scalar() == 3
    assert any(e["action"] == "ml.online_features_enabled" for e in s.audit.entries("ml", limit=20))


def test_lookups_are_governed_as_reads_of_the_table(online):
    s, c, tbl, _ = online
    if c[ANA].get("/v1/ml/feature-tables/customer_stats/online").status_code == 404:
        assert c[ANA].put("/v1/ml/feature-tables/customer_stats/online", json={"timestamp_column": "updated_at"}).status_code == 200
    raw = {x["key"]["customer_id"]: x["features"] for x in lookup(c[ANA], [1]).json()["results"]}[1]
    # Bob reads the table with emails masked: so are his lookups, as Trino would mask them.
    out = lookup(c[BOB], [1]).json()
    assert out["masked"] == ["email"]
    got = out["results"][0]["features"]
    assert got["email"] == "k•••@•••.com" and got["orders_90d"] == raw["orders_90d"]
    # Cal's reads are row-filtered: an entity lookup cannot apply SQL, so it is refused, not unfiltered.
    r = lookup(c[CAL], [1])
    assert r.status_code == 403 and "row filter" in r.json()["detail"]
    # Eve may see the table in the catalog, not read it.
    r = lookup(c[EVE], [1])
    assert r.status_code == 403 and "read access" in r.json()["detail"]
    # Malformed requests.
    for body, msg in [({"entities": [{"id": 1}]}, "exactly the keys"), ({"entities": []}, "entities"),
                      ({"entities": [{"customer_id": 1}], "features": ["nope"]}, "no feature nope"),
                      ({"entities": [{"customer_id": 1.5}]}, "not a long")]:
        r = c[ANA].post("/v1/ml/feature-tables/customer_stats/online/lookup", json=body)
        assert r.status_code == 400 and msg in r.json()["detail"], (body, r.text)
    # The service principal does not look features up as nobody.
    svc = c[ANA].post("/v1/ml/feature-tables/customer_stats/online/lookup", json={"entities": [{"customer_id": 1}]},
                      headers={"Authorization": "Bearer svc-secret"})
    assert svc.status_code == 403


def test_masks_match_trinos_for_each_type():
    from cairn.ml.online import mask_value

    assert mask_value("kim@acme.com", "string", "partial") == "k•••@•••.com"
    assert mask_value("kim@acme.com", "string", "hash").startswith("sha256:")
    assert mask_value(37, "int", "partial") == 30 and mask_value(-123456, "long", "partial") == -100000
    assert mask_value(37, "int", "redact") == 0 and mask_value(0, "int", "partial") == 0
    h = mask_value(37, "int", "hash")
    assert h == mask_value(37, "int", "hash") and -2 ** 31 < h < 2 ** 31
    assert mask_value(456.25, "double", "partial") == 400.0
    assert mask_value("1250.75", "decimal(10, 2)", "partial") == "1000.00"
    assert mask_value("0.05", "decimal(10, 2)", "partial") == "0.05"
    assert mask_value("1250.75", "decimal(10, 2)", "redact") == "0.00"
    assert mask_value("2024-03-17", "date", "partial") == "2024-03-01"
    assert mask_value("2024-03-17", "date", "redact") == "1970-01-01"
    assert mask_value("2026-11-01T12:30:00+00:00", "timestamptz", "partial") == "2026-11-01T00:00:00+00:00"
    assert mask_value(True, "boolean", "partial") is None and mask_value("x", "string", "null") is None
