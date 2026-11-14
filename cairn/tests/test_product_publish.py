"""Publishing data products as Snowflake listings and Delta Sharing shares, kept in sync."""

from __future__ import annotations

import yaml
import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.context.layer import query_patterns
from cairn.governance.abac import Policy, PolicyBundle
from cairn.marketplace.publish import PublishError, PublishOptions
from cairn.services import build_services

PRODUCT = "urn:li:dataProduct:orders"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,shop.public.orders,PROD)"
ITEMS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,shop.public.items,PROD)"
PG = "urn:li:dataset:(urn:li:dataPlatform:postgres,app.public.orders,PROD)"
DBX = "urn:li:dataset:(urn:li:dataPlatform:databricks,main.sales.orders,PROD)"
OWNER, ADMIN, ANA = "urn:li:corpuser:olga", "urn:li:corpuser:admin", "urn:li:corpuser:ana"


def seed(graph, assets):
    graph.aspects[(PRODUCT, "dataProductProperties")] = {
        "name": "Orders", "description": "Every order, deduplicated, refreshed hourly.",
        "assets": [{"destinationUrn": u} for u in assets], "customProperties": {"sla.freshness": "hourly"}}
    graph.aspects[(PRODUCT, "ownership")] = {"owners": [{"owner": OWNER, "type": "DATAOWNER"}]}
    for u, d in ((ORDERS, "One row per order."), (ITEMS, ""), (DBX, "Orders in the lakehouse.")):
        graph.aspects[(u, "datasetProperties")] = {"name": u.split(",")[1].split(".")[-1], "description": d}
        graph.aspects[(u, "schemaMetadata")] = {"fields": [{"fieldPath": "id", "nativeDataType": "NUMBER"}]}


@pytest.fixture
def stack(settings, graph):
    seed(graph, [ORDERS, PG, DBX])
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    with s.engine.begin() as conn:
        conn.execute(query_patterns.insert().values(
            urn="urn:li:query:q1", title="Revenue by day", text="revenue by day",
            sql="select date_trunc('day', ts), sum(amount) from shop.public.orders group by 1",
            tables=[ORDERS], vector=[0.0], updated_ms=1))
    ran = []
    s.publisher.run_sql = lambda conn, stmts: ran.append((conn, stmts)) or [None] * len(stmts)
    return s, graph, ran


def test_snowflake_listing_from_the_catalog(stack):
    s, graph, _ = stack
    p = s.publisher.plan(PRODUCT, "snowflake", PublishOptions(share="orders_share", consumers=["ACME.PROD"]))
    assert p["tables"] == ["shop.public.orders"] and p["skipped"] == [PG, DBX]
    assert p["statements"][:5] == [
        'CREATE SHARE IF NOT EXISTS "ORDERS_SHARE"', 'GRANT USAGE ON DATABASE "SHOP" TO SHARE "ORDERS_SHARE"',
        'GRANT USAGE ON SCHEMA "SHOP"."PUBLIC" TO SHARE "ORDERS_SHARE"',
        'GRANT SELECT ON TABLE "SHOP"."PUBLIC"."ORDERS" TO SHARE "ORDERS_SHARE"',
        p["statements"][4]]
    assert p["statements"][4].startswith('CREATE EXTERNAL LISTING IF NOT EXISTS "ORDERS_SHARE" SHARE "ORDERS_SHARE" AS $$')
    assert p["statements"][4].endswith("$$ PUBLISH = FALSE REVIEW = FALSE")
    m = yaml.safe_load(p["manifest"])
    assert m["title"] == "Orders" and m["targets"] == {"accounts": ["ACME.PROD"]}
    assert m["listing_terms"] == {"type": "STANDARD"} and m["data_attributes"] == {"refresh_rate": "HOURLY"}
    assert m["data_dictionary"]["featured"]["database_objects"] == [{"name": "SHOP.PUBLIC.ORDERS", "domain": "TABLE"}]
    assert m["usage_examples"][0]["title"] == "Revenue by day" and "sum(amount)" in m["usage_examples"][0]["query"]
    assert p["published"] is None and p["in_sync"] is False


def test_publish_records_then_follows_the_product(stack):
    s, graph, ran = stack
    opts = PublishOptions(share="orders_share")
    first = s.publisher.publish(PRODUCT, "snowflake", opts, OWNER, connection="wh")
    assert first["applied"] and first["in_sync"] and ran[0][0] == "wh"
    assert s.publisher.publications()[0] | {"published_ms": 0} == {
        "product": PRODUCT, "target": "snowflake", "share": "orders_share", "tables": ["shop.public.orders"],
        "published_by": OWNER, "published_ms": 0, "in_sync": True, "problem": None}

    seed(graph, [ITEMS, PG])                                                  # orders out, items in
    again = s.publisher.plan(PRODUCT, "snowflake", opts)
    assert not again["in_sync"]
    assert 'GRANT SELECT ON TABLE "SHOP"."PUBLIC"."ITEMS" TO SHARE "ORDERS_SHARE"' in again["statements"]
    assert 'REVOKE SELECT ON TABLE "SHOP"."PUBLIC"."ORDERS" FROM SHARE "ORDERS_SHARE"' in again["statements"]
    assert again["statements"][-1].startswith('ALTER LISTING "ORDERS_SHARE" AS $$')
    assert s.publisher.sweep() == 1 and s.publisher.sweep() == 0            # owners told once per change
    inbox = s.notify.inbox(OWNER)
    assert inbox[0]["type"] == "product.listing_outdated"
    s.publisher.publish(PRODUCT, "snowflake", opts, OWNER)                   # applied by hand
    assert s.publisher.publications()[0]["in_sync"] is True


def test_failed_statements_are_not_recorded(stack):
    s, graph, _ = stack
    s.publisher.run_sql = lambda conn, stmts: ["insufficient privileges"] + [None] * (len(stmts) - 1)
    out = s.publisher.publish(PRODUCT, "snowflake", PublishOptions(share="orders_share"), OWNER, connection="wh")
    assert out["applied"] is False and out["failed"][0]["error"] == "insufficient privileges"
    assert s.publisher.publications() == []
    assert list(s.audit.export(category="marketplace"))[-1]["action"] == "product.publish_failed"


def test_delta_sharing(stack):
    s, graph, _ = stack
    p = s.publisher.plan(PRODUCT, "databricks", PublishOptions(share="orders", consumers=["acme"]))
    assert p["tables"] == ["main.sales.orders"] and p["manifest"] is None
    assert p["statements"] == [
        "CREATE SHARE IF NOT EXISTS `orders` COMMENT 'Every order, deduplicated, refreshed hourly.'",
        "COMMENT ON SHARE `orders` IS 'Every order, deduplicated, refreshed hourly.'",
        "ALTER SHARE `orders` ADD TABLE `main`.`sales`.`orders` COMMENT 'Orders in the lakehouse.'",
        "GRANT SELECT ON SHARE `orders` TO RECIPIENT `acme`"]
    with pytest.raises(PublishError, match="target must be one of"):
        s.publisher.plan(PRODUCT, "bigquery", PublishOptions(share="x"))
    with pytest.raises(ValueError):
        PublishOptions(share="bad name; drop table x")


def test_only_owners_and_admins_publish(stack):
    s, graph, _ = stack
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="admin", effect="allow", actions=["*"],
                                                   condition={"attr": "subject.urn", "op": "eq", "value": ADMIN})]))
    c = TestClient(create_api_app(s))
    body = {"share": "orders_share"}
    url = f"/marketplace/products/{PRODUCT}/publish/snowflake"
    assert c.post(f"{url}/plan", json=body, headers={"X-Cairn-Actor": ANA}).status_code == 403
    assert c.post(f"{url}/plan", json=body, headers={"X-Cairn-Actor": OWNER}).json()["tables"] == ["shop.public.orders"]
    r = c.post(url, json={"options": body}, headers={"X-Cairn-Actor": OWNER})
    assert r.status_code == 200 and r.json()["applied"], r.text
    assert c.get("/marketplace/publications", headers={"X-Cairn-Actor": OWNER}).status_code == 403
    assert len(c.get("/marketplace/publications", headers={"X-Cairn-Actor": ADMIN}).json()) == 1
    assert len(c.get("/marketplace/publications", params={"product": PRODUCT},
                     headers={"X-Cairn-Actor": OWNER}).json()) == 1
