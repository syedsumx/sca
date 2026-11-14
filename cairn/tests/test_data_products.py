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

ALICE, BOB, CAROL = "urn:li:corpuser:alice", "urn:li:corpuser:bob", "urn:li:corpuser:carol"
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
        Policy(id="alice-orders-contract", effect="allow", actions=["EDIT_ENTITY_ASSERTIONS"],
               target=Target(resource_urns=[ORDERS]), condition={"attr": "subject.urn", "op": "eq", "value": ALICE}),
        # Putting an asset in a product (or taking it out) is Core's EDIT_ENTITY_DATA_PRODUCTS on the asset.
        Policy(id="alice-assets", effect="allow", actions=["EDIT_ENTITY_DATA_PRODUCTS"],
               target=Target(resource_urns=[ORDERS, REFUNDS, SECRET]),
               condition={"attr": "subject.urn", "op": "eq", "value": ALICE}),
        Policy(id="carol-sales", effect="allow", actions=["MANAGE_DATA_PRODUCTS"],      # a steward, not of the assets
               target=Target(resource_urns=[SALES]), condition={"attr": "subject.urn", "op": "eq", "value": CAROL}),
        Policy(id="bob-finance", effect="allow", actions=["MANAGE_DATA_PRODUCTS"],
               target=Target(resource_urns=[FINANCE]), condition={"attr": "subject.urn", "op": "eq", "value": BOB}),
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


OLGA = "urn:li:corpuser:olga"


def test_rights_are_the_writes_own_answer(api):
    """What the pages show (Edit, Delete, New product) is asked of the check the writes make: the privilege on the
    product's domain, not being a listed owner."""
    c, _, graph = api
    for d in (SALES, FINANCE):
        graph.aspects[(d, "domainProperties")] = {"name": d.rsplit(":", 1)[1]}
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 201
    rights = lambda who, **q: c.get("/catalog/products/rights", params=q, headers=as_(who))  # noqa: E731
    # A steward with the privilege on the domain, who is not an owner: she may, and the PUT agrees.
    r = rights(ALICE, urn=URN)
    assert r.status_code == 200, r.text
    assert r.json()["may_manage"] is True and r.json()["reason"] == ""
    assert c.put("/catalog/products/orders-360", json=BODY, headers=as_(ALICE)).status_code == 200
    # A listed owner without the privilege: she may not, and the PUT and DELETE agree.
    assert OLGA in c.get("/catalog/products/orders-360", headers=as_(OLGA)).json()["owners"]
    r = rights(OLGA, urn=URN).json()
    assert r["may_manage"] is False and "MANAGE_DATA_PRODUCTS" in r["reason"] and r["privilege"] == "MANAGE_DATA_PRODUCTS"
    assert c.put("/catalog/products/orders-360", json=BODY, headers=as_(OLGA)).status_code == 403
    assert c.delete("/catalog/products/orders-360", headers=as_(OLGA)).status_code == 403
    # Making one: where the privilege is held; "rights" is not taken for a product's id.
    made = rights(ALICE).json()
    assert made["may_create"] is True and made["domains"] == [SALES] and made["without_domain"] is False
    assert rights(ALICE, domain=FINANCE).json()["may_create"] is False
    assert rights(ALICE, domain="sales").json()["may_create"] is True
    none = rights(OLGA).json()
    assert none["may_create"] is False and none["domains"] == [] and "MANAGE_DATA_PRODUCTS" in none["reason"]
    assert c.post("/catalog/products", json={**BODY, "id": "other"}, headers=as_(OLGA)).status_code == 403
    assert c.post("/catalog/products", json={**BODY, "id": "loose", "domain": None}, headers=as_(ALICE)).status_code == 403
    # One that is not there, and one that is not a product.
    assert rights(ALICE, urn="urn:li:dataProduct:nope").status_code == 404
    assert rights(ALICE, urn=ORDERS).status_code == 422


def test_an_error_is_never_a_yes(api):
    c, s, graph = api
    graph.aspects[(SALES, "domainProperties")] = {"name": "sales"}
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 201

    def broken(*a, **k):
        raise RuntimeError("the policy engine is away")
    s.pdp.decide = broken
    quiet = TestClient(c.app, raise_server_exceptions=False)
    assert quiet.get("/catalog/products/rights", params={"urn": URN}, headers=as_(ALICE)).status_code == 500
    assert quiet.get("/catalog/products/rights", headers=as_(ALICE)).status_code == 500
    # The writes too: no decision is no write, whatever the change.
    before = dict(graph.aspects)
    assert quiet.post("/catalog/products", json={**BODY, "id": "other"}, headers=as_(ALICE)).status_code == 500
    assert quiet.put("/catalog/products/orders-360", json={"description": "x"}, headers=as_(ALICE)).status_code == 500
    assert quiet.delete("/catalog/products/orders-360", headers=as_(ALICE)).status_code == 500
    assert graph.aspects == before


def test_an_asset_is_put_in_or_taken_out_only_with_the_privilege_on_it(api):
    """Managing the domain's products is not enough to attach an asset (and grant oneself access through the
    product's request policy): EDIT_ENTITY_DATA_PRODUCTS is needed on each asset that comes or goes."""
    c, _, graph = api
    r = c.post("/catalog/products", json=BODY, headers=as_(CAROL))
    assert r.status_code == 403 and "EDIT_ENTITY_DATA_PRODUCTS" in r.text and (URN, "status") not in graph.aspects
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 201
    # Carol, steward of the sales domain: she may change the rest, not the assets.
    assert c.put("/catalog/products/orders-360", json={"description": "Carol's words"}, headers=as_(CAROL)).status_code == 200
    took = c.put("/catalog/products/orders-360", json={"assets": [ORDERS]}, headers=as_(CAROL))
    assert took.status_code == 403 and REFUNDS in took.text
    assert c.put("/catalog/products/orders-360", json={"assets": [ORDERS, REFUNDS, SECRET]},
                 headers=as_(CAROL)).status_code == 403
    assert c.get("/catalog/products/orders-360", headers=as_(BOB)).json()["assets"] == sorted([ORDERS, REFUNDS])


def test_an_asset_of_a_product_made_here_is_in_it_for_access_decisions(api):
    """A product's access persona (scope: data_products) covers its assets: membership is read from the product
    (Core keeps it there), and a removed product holds nothing."""
    c, s, graph = api
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 201
    assert s.attributes.resource(ORDERS)["data_products"] == [URN]
    assert s.attributes.resource(SECRET)["data_products"] == []
    assert c.put("/catalog/products/orders-360", json={"assets": [REFUNDS], "output_ports": []},
                 headers=as_(ALICE)).status_code == 200
    assert s.attributes.resource(ORDERS)["data_products"] == [] and s.attributes.resource(REFUNDS)["data_products"] == [URN]
    assert c.delete("/catalog/products/orders-360", headers=as_(ALICE)).status_code == 204
    assert s.attributes.resource(REFUNDS)["data_products"] == []


def test_a_change_names_what_changes_and_keeps_owners_cairn_did_not_set(api):
    c, s, graph = api
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 201
    tech = {"owner": "urn:li:corpuser:tess", "type": "TECHNICAL_OWNER",
            "typeUrn": "urn:li:ownershipType:__system__technical_owner", "source": {"type": "MANUAL"}}
    ingested = {"owner": "urn:li:corpuser:ivan", "type": "DATAOWNER", "source": {"type": "SERVICE", "url": "x"}}
    graph.aspects[(URN, "ownership")]["owners"] += [tech, ingested]
    # Left out: unchanged (domain, owners, assets, ports).
    r = c.put("/catalog/products/orders-360", json={"description": "New words"}, headers=as_(ALICE))
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["description"] == "New words" and got["domain"] == SALES and got["output_ports"] == [ORDERS]
    assert got["assets"] == sorted([ORDERS, REFUNDS]) and got["name"] == "Orders 360"
    assert graph.aspects[(URN, "domains")] == {"domains": [SALES]}
    # Owners named: Cairn's DATAOWNERs follow the list; the technical and the ingested owner stay as they were.
    r = c.put("/catalog/products/orders-360", json={"owners": ["ana"]}, headers=as_(ALICE))
    owners = graph.aspects[(URN, "ownership")]["owners"]
    assert tech in owners and ingested in owners and {"owner": "urn:li:corpuser:ana", "type": "DATAOWNER"} in owners
    assert not any(o["owner"] in ("urn:li:corpuser:olga", "urn:li:corpGroup:orders-team") for o in owners)
    # Echoing what GET shows does not turn the others into DATAOWNERs; [] clears only Cairn's.
    shown = c.get("/catalog/products/orders-360", headers=as_(ALICE)).json()["owners"]
    assert c.put("/catalog/products/orders-360", json={"owners": shown}, headers=as_(ALICE)).status_code == 200
    assert sorted(o["owner"] for o in graph.aspects[(URN, "ownership")]["owners"]) == sorted(shown)
    assert c.put("/catalog/products/orders-360", json={"owners": []}, headers=as_(ALICE)).status_code == 200
    assert graph.aspects[(URN, "ownership")]["owners"] == [tech, ingested]
    # The assets named without ports: the ports still among them stay.
    r = c.put("/catalog/products/orders-360", json={"assets": [ORDERS]}, headers=as_(ALICE)).json()
    assert r["assets"] == [ORDERS] and r["output_ports"] == [ORDERS]
    # The audit says what moved.
    entry = next(e for e in s.audit.entries("marketplace") if e["action"] == "data_product.updated")
    assert entry["details"]["assets"] == {"added": [], "removed": [REFUNDS], "count": 1}
    assert entry["details"]["domain"] == {"from": SALES, "to": SALES}
    owners_entry = [e for e in s.audit.entries("marketplace") if e["action"] == "data_product.updated"][1]
    assert owners_entry["details"]["owners"]["removed"] == ["urn:li:corpuser:ana"]


def test_a_product_made_in_core_with_an_id_cairn_would_not_make_is_changed_by_that_id(api):
    c, _, graph = api
    odd = "urn:li:dataProduct:orders:360"
    graph.aspects[(odd, "dataProductProperties")] = {"name": "Orders", "assets": []}
    graph.aspects[(odd, "domains")] = {"domains": [SALES]}
    r = c.put("/catalog/products/orders:360", json={"description": "kept"}, headers=as_(ALICE))
    assert r.status_code == 200, r.text
    assert r.json()["id"] == "orders:360" and graph.aspects[(odd, "dataProductProperties")]["description"] == "kept"
    made = c.post("/catalog/products", json={**BODY, "id": "orders:361"}, headers=as_(ALICE))
    assert made.status_code == 422 and "letters, digits" in made.text


def test_core_refusing_a_write_is_a_502(api):
    from cairn.common.core import CoreError

    c, _, graph = api

    def refuse(mcps):
        raise CoreError("Cairn Core POST /aspects failed: 503 unavailable", 503)
    graph.emit = refuse
    r = c.post("/catalog/products", json=BODY, headers=as_(ALICE))
    assert r.status_code == 502 and "Cairn Core" in r.json()["detail"]


def test_a_deleted_product_takes_its_marketplace_state_with_it(api):
    """Refused while access granted through it is in force; then its policy, pending requests, subscriptions and
    persona go, and making it again needs the old domain's privilege and starts with no labels of the old one."""
    import time

    from cairn.marketplace.service import access_requests, product_policies, subscriptions

    c, s, graph = api
    assert c.post("/catalog/products", json=BODY, headers=as_(ALICE)).status_code == 201
    now = int(time.time() * 1000)
    with s.engine.begin() as conn:
        conn.execute(product_policies.insert().values(product_urn=URN, doc={"auto_approve": {"enabled": True}},
                                                      updated_by=ALICE))
        conn.execute(subscriptions.insert().values(actor=BOB, product_urn=URN, created_at_ms=now))
        for rid, state, exp in (("ar_live", "APPROVED", now + 86_400_000), ("ar_wait", "PENDING", None)):
            conn.execute(access_requests.insert().values(
                id=rid, product_urn=URN, requester=BOB, state=state, doc={"history": [], "product_name": "Orders 360"},
                created_at_ms=now, updated_at_ms=now, expires_at_ms=exp, version=1))
    r = c.delete("/catalog/products/orders-360", headers=as_(ALICE))
    assert r.status_code == 409 and "revoke" in r.text and graph.aspects[(URN, "status")] == {"removed": False}
    with s.engine.begin() as conn:
        conn.execute(access_requests.update().where(access_requests.c.id == "ar_live").values(expires_at_ms=now - 1))
    assert c.delete("/catalog/products/orders-360", headers=as_(ALICE)).status_code == 204
    assert s.marketplace.get_policy(URN).auto_approve.enabled is False and s.marketplace.subscribers(URN) == []
    assert s.marketplace.get_request("ar_wait")["state"] == "CANCELLED"
    # Made again: Bob manages finance, not sales where the removed one was.
    graph.aspects[(URN, "globalTags")] = {"tags": [{"tag": "urn:li:tag:gold"}]}
    again = {**BODY, "domain": FINANCE, "assets": [], "output_ports": []}
    assert c.post("/catalog/products", json=again, headers=as_(BOB)).status_code == 403
    assert graph.aspects[(URN, "status")] == {"removed": True}
    assert c.post("/catalog/products", json={**BODY, "owners": []}, headers=as_(ALICE)).status_code == 201
    assert graph.aspects[(URN, "globalTags")] == {"tags": []}
    assert graph.aspects[(URN, "ownership")]["owners"] == []


def test_contract_rights_follow_the_publish_check(api):
    c, _, _ = api
    spec = {"dataset": ORDERS, "version": "1.0.0", "schema": {"columns": [{"name": "id", "type": "bigint"}]}}
    yes = c.get("/quality/contracts/rights", params={"dataset": ORDERS}, headers=as_(ALICE)).json()
    no = c.get("/quality/contracts/rights", params={"dataset": ORDERS}, headers=as_(BOB)).json()
    assert yes["may_manage"] is True and no["may_manage"] is False and "EDIT_ENTITY_ASSERTIONS" in no["reason"]
    assert c.put("/quality/contracts", json=spec, headers=as_(BOB)).status_code == 403
    assert c.put("/quality/contracts", json=spec, headers=as_(ALICE)).status_code == 200
    assert c.get("/quality/contracts/rights", params={"dataset": SECRET}, headers=as_(ALICE)).status_code == 404


# ---- the Marketplace's lists are whole (cairn.marketplace.service) -----------------------------------------------------
def _core(graph, products: int, assets: int = 0, with_total: bool = True, calls=None):
    """Cairn Core's search, a page at a time: ``products`` data products, the first with ``assets`` assets."""
    urns = [f"urn:li:dataProduct:p{i:04d}" for i in range(products)]
    held = [f"urn:li:dataset:(urn:li:dataPlatform:iceberg,t.a{i:04d},PROD)" for i in range(assets)]

    def entity(u):
        return {"urn": u, "properties": {"name": f"Product {u[-4:]}", "numAssets": assets}}

    def handler(query, v):
        if calls is not None:
            calls.append((query.split("(")[0].split()[-1], dict(v.get("input") or v)))
        if "cairnProducts" in query:
            start, count = v["input"]["start"], v["input"]["count"]
            out = {"searchResults": [{"entity": entity(u)} for u in urns[start:start + count]]}
            return {"searchAcrossEntities": {**out, "total": len(urns)} if with_total else out}
        start, count = (v["start"], v["count"]) if "cairnProductAssets" in query else (0, 200)
        return {"dataProduct": {**entity(v["urn"]), "entities": {"total": len(held), "searchResults": [
            {"entity": {"urn": a, "type": "DATASET", "name": a.split(",")[1]}} for a in held[start:start + count]]}}}
    graph.graphql_handler = handler
    return urns


@pytest.mark.parametrize("with_total", [True, False])
def test_every_product_is_listed_not_the_first_sixty(api, with_total):
    c, s, graph = api
    calls: list = []
    urns = _core(graph, 450, with_total=with_total, calls=calls)
    r = c.get("/marketplace/products", params={"sort": "name"}, headers=as_(BOB))
    assert r.status_code == 200, r.text
    assert [p["urn"] for p in r.json()] == urns                       # all 450, in name order, each once
    assert r.headers["X-Truncated"] == "false"
    assert r.headers.get("X-Total-Count") == ("450" if with_total else None)
    assert [(v["start"], v["count"]) for _, v in calls] == [(0, 200), (200, 200), (400, 200)]
    # A filter is applied to the whole list, not to its first page.
    graph_wide = s.marketplace.list_products(BOB, can_view=lambda u: u.endswith("9"))
    assert len(graph_wide) == 45 and graph_wide.truncated is False
    # The assistant's "first few" stays one small page.
    calls.clear()
    few = s.marketplace.list_products(BOB, limit=20)
    assert len(few) == 20 and few.truncated is False and [v["count"] for _, v in calls] == [20]


def test_a_list_cut_at_the_bound_says_so(api, monkeypatch):
    from cairn.marketplace import service

    c, _, graph = api
    monkeypatch.setattr(service, "MAX_PRODUCTS", 300)
    _core(graph, 450)
    r = c.get("/marketplace/products", headers=as_(BOB))
    assert len(r.json()) == 300 and r.headers["X-Truncated"] == "true" and r.headers["X-Total-Count"] == "450"
    _core(graph, 300)                                                   # exactly the bound: whole
    r = c.get("/marketplace/products", headers=as_(BOB))
    assert len(r.json()) == 300 and r.headers["X-Truncated"] == "false"
    _core(graph, 450, with_total=False)                                 # no total from Core: cut, and said
    r = c.get("/marketplace/products", headers=as_(BOB))
    assert len(r.json()) == 300 and r.headers["X-Truncated"] == "true"


def test_a_products_assets_are_read_to_the_end(api, monkeypatch):
    from cairn.marketplace import service

    c, _, graph = api
    urn = _core(graph, 1, assets=450)[0]
    got = c.get(f"/marketplace/products/{urn}", headers=as_(BOB)).json()
    assert len(got["assets"]) == 450 and got["assets_total"] == 450 and got["assets_truncated"] is False
    assert len({a["urn"] for a in got["assets"]}) == 450 and got["assets"][0]["name"] == "t.a0000"
    monkeypatch.setattr(service, "MAX_ASSETS", 400)
    got = c.get(f"/marketplace/products/{urn}", headers=as_(BOB)).json()
    assert len(got["assets"]) == 400 and got["assets_total"] == 450 and got["assets_truncated"] is True
