"""Feature tables: a lakehouse table's columns declared as ML features by its owners, published to the
catalog with their lineage to the table; others see them only where they may see the table."""

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.services import build_services

DS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,shop.customers,PROD)"
ANA, BOB, EVE = "urn:li:corpuser:ana", "urn:li:corpuser:bob", "urn:li:corpuser:eve"


@pytest.fixture
def api(settings, graph):
    graph.aspects[(DS, "schemaMetadata")] = {"fields": [
        {"fieldPath": "customer_id", "nativeDataType": "bigint"}, {"fieldPath": "orders_90d", "nativeDataType": "int"},
        {"fieldPath": "spend_90d", "nativeDataType": "double", "description": "spend in the last 90 days"},
        {"fieldPath": "country", "nativeDataType": "varchar"}]}
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    sub = lambda u: {"attr": "subject.urn", "op": "eq", "value": u}  # noqa: E731
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="ana-owns", effect="allow", actions=["MANAGE_DATASET", "VIEW_ENTITY_PAGE"], target=Target(resource_urns=["*shop.*"]),
               condition=sub(ANA)),
        Policy(id="bob-sees", effect="allow", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=["*shop.*"]), condition=sub(BOB))]))
    app = create_api_app(s)
    return s, graph, {u: TestClient(app, headers={"X-Cairn-Actor": u}) for u in (ANA, BOB, EVE)}


def test_a_tables_owners_declare_its_features(api):
    s, graph, c = api
    r = c[ANA].post("/v1/ml/feature-tables", json={"name": "customer_activity", "table": "shop.customers",
                                                    "keys": ["customer_id"], "description": "90-day activity"})
    assert r.status_code == 201, r.text
    assert r.json()["features"] == ["orders_90d", "spend_90d", "country"] and r.json()["source"] == DS
    ft = "urn:li:mlFeatureTable:(urn:li:dataPlatform:iceberg,customer_activity)"
    props = graph.aspects[(ft, "mlFeatureTableProperties")]
    assert props["mlPrimaryKeys"] == ["urn:li:mlPrimaryKey:(customer_activity,customer_id)"] and len(props["mlFeatures"]) == 3
    spend = graph.aspects[("urn:li:mlFeature:(customer_activity,spend_90d)", "mlFeatureProperties")]
    assert spend == {"description": "spend in the last 90 days", "dataType": "CONTINUOUS", "sources": [DS]}   # lineage
    assert graph.aspects[(ft, "ownership")]["owners"][0]["owner"] == ANA
    # Bob may see the table, not declare its features; Eve sees neither.
    assert c[BOB].post("/v1/ml/feature-tables", json={"name": "x", "table": "shop.customers", "keys": ["customer_id"]}).status_code == 403
    assert [t["name"] for t in c[BOB].get("/v1/ml/feature-tables").json()] == ["customer_activity"]
    assert c[EVE].get("/v1/ml/feature-tables").json() == [] and c[EVE].get("/v1/ml/feature-tables/customer_activity").status_code == 404
    for body, msg in [({"name": "y", "table": "shop.customers", "keys": ["nope"]}, "no column nope"),
                      ({"name": "y", "table": "shop.customers", "keys": []}, "keys"),
                      ({"name": "y", "table": "shop.missing", "keys": ["id"]}, "not in the catalog"),
                      ({"name": "y", "table": "shop.customers", "keys": ["customer_id"], "features": ["customer_id"]}, "key column")]:
        r = c[ANA].post("/v1/ml/feature-tables", json=body)
        assert r.status_code >= 400 and msg in r.json()["detail"], (body, r.text)
    assert any(e["action"] == "ml.feature_table_registered" for e in s.audit.entries("ml", limit=10))


def test_a_feature_table_is_changed_only_by_who_manages_its_table(api):
    """Registering is also how a feature table changes (Terraform's cairn_feature_table updates with it): someone
    who manages another table may not take over an existing feature table by pointing its name at their table."""
    s, graph, c = api
    graph.aspects[("urn:li:dataset:(urn:li:dataPlatform:iceberg,eve.stuff,PROD)", "schemaMetadata")] = {"fields": [
        {"fieldPath": "customer_id", "nativeDataType": "bigint"}, {"fieldPath": "spend_90d", "nativeDataType": "double"}]}
    s.pdp.set_bundle(PolicyBundle(policies=[*s.pdp._bundle.policies, Policy(
        id="eve-owns", effect="allow", actions=["MANAGE_DATASET", "VIEW_ENTITY_PAGE"],
        target=Target(resource_urns=["*eve.*"]), condition={"attr": "subject.urn", "op": "eq", "value": EVE})]))
    assert c[ANA].post("/v1/ml/feature-tables", json={"name": "customer_activity", "table": "shop.customers",
                                                      "keys": ["customer_id"]}).status_code == 201
    r = c[EVE].post("/v1/ml/feature-tables", json={"name": "customer_activity", "table": "eve.stuff",
                                                    "keys": ["customer_id"]})
    assert r.status_code == 403, r.text
    assert c[ANA].get("/v1/ml/feature-tables/customer_activity").json()["source"] == DS
    ft = "urn:li:mlFeatureTable:(urn:li:dataPlatform:iceberg,customer_activity)"
    assert graph.aspects[(ft, "ownership")]["owners"][0]["owner"] == ANA
    assert c[EVE].post("/v1/ml/feature-tables", json={"name": "eve_features", "table": "eve.stuff",
                                                      "keys": ["customer_id"]}).status_code == 201   # her own: yes
    # Its table's managers still change it; once removed, the name is free again.
    assert c[ANA].post("/v1/ml/feature-tables", json={"name": "customer_activity", "table": "shop.customers",
                                                      "keys": ["customer_id"], "description": "x"}).status_code == 201
    assert c[ANA].delete("/v1/ml/feature-tables/customer_activity").status_code == 204
    assert c[EVE].post("/v1/ml/feature-tables", json={"name": "customer_activity", "table": "eve.stuff",
                                                      "keys": ["customer_id"]}).status_code == 201


def test_a_feature_table_without_a_known_source_is_seen_only_where_it_may_be(api):
    """Fail closed: a feature table in the catalog that names no source table (written by something other than
    Cairn's declarations) is decided on the feature table itself, never shown to everyone."""
    s, graph, c = api
    ft = "urn:li:mlFeatureTable:(urn:li:dataPlatform:iceberg,fraud_signals)"
    graph.aspects[(ft, "mlFeatureTableProperties")] = {"description": "internal", "mlFeatures": [
        "urn:li:mlFeature:(fraud_signals,score)"], "mlPrimaryKeys": ["urn:li:mlPrimaryKey:(fraud_signals,account_id)"]}
    assert c[EVE].get("/v1/ml/feature-tables").json() == []
    assert c[EVE].get("/v1/ml/feature-tables/fraud_signals").status_code == 404
    assert c[EVE].delete("/v1/ml/feature-tables/fraud_signals").status_code in (403, 404)
    s.pdp.set_bundle(PolicyBundle(policies=[*s.pdp._bundle.policies, Policy(
        id="eve-sees", effect="allow", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=[ft]),
        condition={"attr": "subject.urn", "op": "eq", "value": EVE})]))
    assert [t["name"] for t in c[EVE].get("/v1/ml/feature-tables").json()] == ["fraud_signals"]
