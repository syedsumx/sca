"""Semantic search: similar assets, explanations, exclusions and index upkeep."""

import os

import pytest
from fastapi.testclient import TestClient

from cairn.ai.catalog import Catalog
from cairn.ai.embeddings import HashingEmbedder, InMemoryVectorStore, _stem, content_hash, matched_terms
from cairn.ai.evals import _graph
from cairn.ai.semantic import SemanticIndex
from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services


def ds(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.{name},PROD)"


CATALOG = [
    {"urn": ds("orders"), "name": "orders", "description": "One row per customer order with totals.",
     "fields": ["order_id", "customer_id", "order_total"]},
    {"urn": ds("order_items"), "name": "order_items", "description": "Line items of each customer order.",
     "fields": ["order_id", "sku", "quantity"]},
    {"urn": ds("salaries"), "name": "salaries", "description": "Employee salaries and bonuses.",
     "fields": ["employee_id", "salary"]},
    {"urn": ds("weather"), "name": "weather", "description": "Hourly temperature readings per city.",
     "fields": ["city", "temperature_c"]},
]


@pytest.fixture
def index():
    case = {"catalog": [dict(e) for e in CATALOG]}
    graph = _graph(case)
    idx = SemanticIndex(Catalog(graph), HashingEmbedder(), InMemoryVectorStore())
    idx.case = case          # tests change the catalog through it
    idx.graph = graph
    return idx


def test_plural_folding_and_matched_terms():
    assert [_stem(w) for w in ("salaries", "orders", "addresses", "status", "class", "bus")] == \
        ["salary", "order", "address", "status", "class", "bus"]
    assert matched_terms("show me customer orders by region", "orders: one row per customer order") == \
        ["customer", "orders"]
    assert matched_terms("the data", "the data table") == []           # stop words do not count


def test_similar_assets(index):
    index.index_many([e["urn"] for e in CATALOG])
    similar = index.similar(ds("orders"), 2)
    assert similar[0]["urn"] == ds("order_items")
    assert ds("orders") not in [s["urn"] for s in similar]
    hidden = index.similar(ds("orders"), 3, can_view=lambda u: u != ds("order_items"))
    assert ds("order_items") not in [s["urn"] for s in hidden]
    # An asset not indexed yet is indexed on demand.
    index.store.delete(ds("weather"))
    assert index.similar(ds("weather"), 3) and index.store.get_vector(ds("weather"))
    assert index.similar("urn:li:dataset:(urn:li:dataPlatform:x,missing,PROD)") == []


def test_search_explains_and_excludes(index):
    index.index_many([e["urn"] for e in CATALOG])
    hits = index.search("customer orders", 3)
    assert hits[0]["urn"] in (ds("orders"), ds("order_items")) and "customer" in hits[0]["matched"]
    # Results carry the asset's own description and platform, not the raw indexed document.
    assert hits[0]["platform"] == "postgres" and "customer order" in hits[0]["description"]
    assert not hits[0]["description"].startswith("dataset ")
    rest = index.search("customer orders", 3, exclude=[ds("orders"), ds("order_items")])
    assert {h["urn"] for h in rest}.isdisjoint({ds("orders"), ds("order_items")})
    assert all(h["score"] >= 0.2 for h in index.search("customer orders", 4, min_score=0.2))


def test_rebuild_prunes_deleted_entities_and_reports_status(index, monkeypatch):
    def all_urns(entity_types, page, max_entities):
        return [e["urn"] for e in index.case["catalog"]], True
    monkeypatch.setattr(index, "_all_urns", all_urns)
    first = index.ensure_built()
    assert first["indexed"] == 4 and first["pruned"] == 0
    assert index.status()["entries"] == 4 and index.status()["by_type"] == {"dataset": 4}
    again = index.rebuild()
    assert again["unchanged"] == 4 and again["indexed"] == 0          # nothing re-embedded
    index.case["catalog"].pop()                                       # weather is deleted from the catalog
    out = index.rebuild()
    assert out["pruned"] == 1 and ds("weather") not in index.store.urns()
    assert index.status()["last_rebuild"]["pruned"] == 1
    # A partial rebuild (some types) never prunes.
    index.store.upsert(ds("ghost"), "dataset", "ghost", "x", [0.0] * 384, "h")
    assert "pruned" not in index.rebuild(["dataset"])


def test_embedder_upgrade_re_embeds(index, monkeypatch):
    monkeypatch.setattr(index, "_all_urns", lambda *a: ([e["urn"] for e in CATALOG], True))
    index.rebuild()
    old = content_hash("doc", index.embedder)
    monkeypatch.setattr(HashingEmbedder, "version", 99)
    assert content_hash("doc", index.embedder) != old
    assert index.rebuild()["indexed"] == 4


def test_rebuild_lists_every_type_with_valid_search_types(index):
    """The listing uses the catalog's GraphQL EntityType names (GLOSSARY_TERM, not GLOSSARYTERM),
    and a type that fails to list is skipped without pruning anything."""
    from cairn.ai.semantic import DEFAULT_TYPES

    valid = {"DATASET", "DASHBOARD", "CHART", "DATA_JOB", "GLOSSARY_TERM", "DATA_PRODUCT"}
    asked = []
    inner = index.graph.graphql_handler

    def handler(query, variables):
        if "searchAcrossEntities" in query and "total" in query and "searchResults { entity { urn } }" in query:
            t = variables["i"]["types"][0]
            asked.append(t)
            if t not in valid:
                raise RuntimeError(f"Invalid input for enum 'EntityType': {t}")
            if t != "DATASET":
                return {"searchAcrossEntities": {"total": 0, "searchResults": []}}
        return inner(query, variables)
    index.graph.graphql_handler = handler
    out = index.rebuild()
    assert set(asked) == valid and len(asked) == len(DEFAULT_TYPES) and out["indexed"] == 4
    assert out["pruned"] == 0

    index.store.upsert(ds("ghost"), "dataset", "ghost", "x", [0.0] * 384, "h")

    def failing(query, variables):
        if "searchResults { entity { urn } }" in query and variables["i"]["types"] == ["DASHBOARD"]:
            raise RuntimeError("search unavailable")
        return handler(query, variables)
    index.graph.graphql_handler = failing
    out = index.rebuild()
    assert "pruned" not in out and ds("ghost") in index.store.urns()      # incomplete listing: keep entries


def test_concurrent_rebuilds_do_not_overlap(index):
    assert index._rebuild_lock.acquire()
    try:
        assert index.rebuild() == {"status": "already running"}
    finally:
        index._rebuild_lock.release()


@pytest.mark.skipif(not os.environ.get("CAIRN_TEST_PG_URL"), reason="needs CAIRN_TEST_PG_URL (pgvector)")
def test_pgvector_table_is_recreated_when_the_dimension_changes():
    from sqlalchemy import create_engine

    from cairn.ai.embeddings import PgVectorStore

    engine = create_engine(os.environ["CAIRN_TEST_PG_URL"])
    store = PgVectorStore(engine, 8, table="cairn_embeddings_test")
    store.upsert("u1", "dataset", "n", "body", [1.0] + [0.0] * 7, "h")
    assert store.get_vector("u1")[0] == 1.0 and store.counts() == {"dataset": 1}
    bigger = PgVectorStore(engine, 16, table="cairn_embeddings_test")
    assert bigger.counts() == {}
    bigger.upsert("u1", "dataset", "n", "body", [0.0] * 15 + [1.0], "h")
    assert bigger.search([0.0] * 15 + [1.0], 1)[0].urn == "u1"


# --------------------------------------------------------------------------- API
@pytest.fixture
def api(settings, graph):
    graph.graphql_handler = _graph({"catalog": CATALOG}).graphql_handler
    s = build_services(settings, graph=graph, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"}),
        Policy(id="no-hr", effect="deny", priority=10, actions=["*"],
               condition={"attr": "resource.urn", "op": "eq", "value": ds("salaries")}),
        Policy(id="ops", effect="allow", actions=["MANAGE_SYSTEM_OPERATIONS"],
               condition={"attr": "subject.urn", "op": "eq", "value": "urn:li:corpuser:ops"})]))
    s.semantic.index_many([e["urn"] for e in CATALOG])
    return TestClient(create_api_app(s))


def test_similar_and_status_endpoints(api):
    bob, ops = {"X-Cairn-Actor": "urn:li:corpuser:bob"}, {"X-Cairn-Actor": "urn:li:corpuser:ops"}
    r = api.get("/ai/similar", params={"urn": ds("orders")}, headers=bob)
    assert r.status_code == 200 and r.json()["results"][0]["urn"] == ds("order_items")
    assert ds("salaries") not in [x["urn"] for x in r.json()["results"]]
    assert api.get("/ai/similar", params={"urn": ds("salaries")}, headers=bob).status_code == 403
    r = api.post("/ai/search/semantic", json={"query": "employee salary", "exclude": [ds("orders")]}, headers=bob)
    assert ds("salaries") not in [x["urn"] for x in r.json()["results"]]
    assert api.get("/ai/index/status", headers=bob).status_code == 403
    assert api.get("/ai/index/status", headers=ops).json()["entries"] == 4
