"""Data products made and changed through Cairn (``/catalog/products``), as Cairn Core entities: the aspects written
to the metadata graph, who may change a product (MANAGE_DATA_PRODUCTS on its domain), assets the caller may not see,
output ports, removal and making it again; products made in Core's own UI keep what Cairn does not manage."""

import json

import pytest
from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.marketplace.products import ProductIn, record
from cairn.services import build_services

ALICE, BOB = "urn:li:corpuser:alice", "urn:li:corpuser:bob"
SALES, FINANCE = "urn:li:domain:sales", "urn:li:domain:finance"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
REFUNDS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.refunds,PROD)"
SECRET = "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)"
URN = "urn:li:dataProduct:orders-360"


@pytest.fixture
def api(settings, graph):
    for ds in (ORDERS, REFUNDS, SECRET):
        graph.aspects[(ds, "datasetProperties")] = {"name": ds.split(",")[1]}
    s = build_services(settings, graph=graph, with_ai=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"]),
        Policy(id="no-hr", effect="deny", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=[SECRET])),
        Policy(id="alice-sales", effect="allow", actions=["MANAGE_DATA_PRODUCTS"],
               target=Target(resource_urns=[SALES]), condition={"attr": "subject.urn", "op": "eq", "value": ALICE}),
    ]))
    return TestClient(create_api_app(s)), s, graph


def as_(who):
    return {"X-Cairn-Actor": who}


BODY = {"id": "orders-360", "name": "Orders 360", "description": "Every order", "domain": SALES,
        "owners": ["olga", "urn:li:corpGroup:orders-team"], "assets": [ORDERS, REFUNDS], "output_ports": [ORDERS]}


def test_a_product_is_written_to_core_and_read_back(api):
    c, _, graph = api
    assert c.post("/catalog/products", json=BODY, headers=as_(BOB)).status_code == 403     # not on the sales domain
    r = c.post("/catalog/products", json=BODY, headers=as_(ALICE))
    assert r.status_code == 201, r.text
    assert r.json() == {"id": "orders-360", "urn": URN, "name": "Orders 360", "description": "Every order",
                        "domain": SALES, "owners": ["urn:li:corpGroup:orders-team", "urn:li:corpuser:olga"],
                        "assets": sorted([ORDERS, REFUNDS]), "output_ports": [ORDERS], "managed_by_cairn": True}
    props = graph.aspects[(URN, "dataProductProperties")]
    assert [a["destinationUrn"] for a in props["assets"]] == sorted([ORDERS, REFUNDS])
    assert props["customProperties"] == {"cairn.managed": "true", "cairn.output_ports": json.dumps([ORDERS])}
    assert graph.aspects[(URN, "domains")] == {"domains": [SALES]}
    assert {o["owner"] for o in graph.aspects[(URN, "ownership")]["owners"]} == {"urn:li:corpuser:olga",
                                                                                "urn:li:corpGroup:orders-team"}
    assert graph.aspects[(URN, "status")] == {"removed": False}
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 409
    assert [p["id"] for p in c.get("/catalog/products", headers=as_(BOB)).json()["products"]] == ["orders-360"]
    assert c.get("/catalog/products/orders-360", headers=as_(BOB)).json()["output_ports"] == [ORDERS]
    assert c.get("/catalog/products/nope", headers=as_(BOB)).status_code == 404


def test_changes_are_checked(api):
    c, _, graph = api
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 201
    bad = c.put("/catalog/products/orders-360", json={**BODY, "output_ports": [SECRET]}, headers=as_(ALICE))
    assert bad.status_code == 422 and "output ports must be assets" in bad.text
    hidden = c.put("/catalog/products/orders-360", json={**BODY, "assets": [ORDERS, SECRET]}, headers=as_(ALICE))
    assert hidden.status_code == 403 and "cannot see 1 of the assets" in hidden.text
    # Moving the product to a domain she does not manage products in: refused, nothing written.
    moved = c.put("/catalog/products/orders-360", json={**BODY, "domain": FINANCE}, headers=as_(ALICE))
    assert moved.status_code == 403
    assert graph.aspects[(URN, "domains")] == {"domains": [SALES]}
    assert c.put("/catalog/products/orders-360", json=BODY, headers=as_(BOB)).status_code == 403
    r = c.put("/catalog/products/orders-360", json={**BODY, "assets": [ORDERS], "output_ports": []},
              headers=as_(ALICE))
    assert r.status_code == 200, r.text
    assert r.json()["assets"] == [ORDERS] and r.json()["output_ports"] == []
    assert graph.aspects[(URN, "dataProductProperties")]["customProperties"] == {"cairn.managed": "true"}
    assert c.post("/catalog/products", json={**BODY, "id": "bad id"}, headers=as_(ALICE)).status_code == 422
    assert c.put("/catalog/products/nope", json=BODY, headers=as_(ALICE)).status_code == 404


def test_removed_and_made_again(api):
    c, _, graph = api
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 201
    assert c.delete("/catalog/products/orders-360", headers=as_(BOB)).status_code == 403
    assert c.delete("/catalog/products/orders-360", headers=as_(ALICE)).status_code == 204
    assert graph.aspects[(URN, "status")] == {"removed": True}
    assert c.get("/catalog/products", headers=as_(ALICE)).json()["products"] == []
    assert c.get("/catalog/products/orders-360", headers=as_(ALICE)).status_code == 404
    assert c.delete("/catalog/products/orders-360", headers=as_(ALICE)).status_code == 404
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 201
    assert graph.aspects[(URN, "status")] == {"removed": False}


def test_a_product_made_in_core_keeps_what_cairn_does_not_manage(api):
    c, s, graph = api
    stamp = {"time": 1, "actor": "urn:li:corpuser:datahub"}
    graph.aspects[(URN, "dataProductProperties")] = {
        "name": "Orders", "externalUrl": "https://wiki/orders", "customProperties": {"readme": "# Orders"},
        "assets": [{"destinationUrn": ORDERS, "created": stamp, "outputPort": True}]}
    graph.aspects[(URN, "domains")] = {"domains": [SALES]}
    got = c.get("/catalog/products/orders-360", headers=as_(ALICE)).json()
    assert got["output_ports"] == [ORDERS] and not got["managed_by_cairn"]          # Core's own output port flag
    r = c.put("/catalog/products/orders-360", json={**BODY, "assets": [ORDERS, REFUNDS], "output_ports": [REFUNDS]},
              headers=as_(ALICE))
    assert r.status_code == 200, r.text
    props = graph.aspects[(URN, "dataProductProperties")]
    assert props["externalUrl"] == "https://wiki/orders"
    assert props["customProperties"] == {"readme": "# Orders", "cairn.managed": "true", "cairn.output_ports": json.dumps([REFUNDS])}
    kept = next(a for a in props["assets"] if a["destinationUrn"] == ORDERS)
    assert kept["created"] == stamp and "outputPort" not in kept
    assert record(URN, {"dataProductProperties": props})["output_ports"] == [REFUNDS]


def test_spec_rules():
    with pytest.raises(ValueError):
        ProductIn(id="x", name="X", owners=["urn:li:dataset:nope"])
    with pytest.raises(ValueError):
        ProductIn(id="x", name="X", assets=["urn:li:dataProduct:other"])
    assert ProductIn(id="x", name="X", domain="sales").domain == SALES
