"""The asset page's lineage graph (GET /embed/v1/graph): the lineage around an asset for the signed-in
user, with the assets they may not see left out and counted."""

import json

from fastapi.testclient import TestClient

from cairn.app import create_api_app
from cairn.governance.abac import Policy, PolicyBundle, Target
from cairn.services import build_services

ANA = {"X-Cairn-Actor": "urn:li:corpuser:ana"}
ROOT = "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.sales.by_region,PROD)"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.sales.orders,PROD)"
RAW = "urn:li:dataset:(urn:li:dataPlatform:s3,raw.orders,PROD)"
SECRET = "urn:li:dataset:(urn:li:dataPlatform:trino,lakehouse.hr.payroll,PROD)"
DASH = "urn:li:dashboard:(looker,regional_sales)"
PAY = "urn:li:dataset:(urn:li:dataPlatform:s3,raw.payslips,PROD)"
OLD = "urn:li:dashboard:(tableau,legacy)"


class Catalog:
    """Search across lineage, as Cairn Core answers it: (urn, degree) per direction."""

    def summaries(self, urns):
        return {u: {"name": u.split(",")[1] if "," in u else u} for u in urns}

    def lineage(self, urn, direction, depth, count, with_paths=False):
        """(urn, degree, name, path from the asset): Cairn Core's search across lineage, with paths."""
        if urn != ROOT:
            return []
        if direction == "UPSTREAM":
            items = [(ORDERS, 1, "lakehouse.sales.orders", [ROOT, ORDERS]),
                     (SECRET, 1, "lakehouse.hr.payroll", [ROOT, SECRET]),
                     (RAW, 2, "raw.orders", [ROOT, ORDERS, RAW]),
                     (PAY, 2, "raw.payslips", [ROOT, SECRET, PAY])]      # reached through the hidden table
        else:
            items = [(DASH, 1, "Regional sales", [ROOT, DASH]), (OLD, 2, "Legacy", None)]  # no path given
        return [{"urn": u, "degree": d, "name": n, "type": "DATASET", "platform": "trino", "paths": [p] if p else []}
                for u, d, n, p in items if d <= depth]


def test_the_graph_leaves_out_what_the_user_may_not_see(settings, graph):
    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="see-all", effect="allow", actions=["VIEW_ENTITY_PAGE"]),
        Policy(id="no-hr", effect="deny", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=["*hr.*"])),
    ]))
    s.embed.catalog = Catalog()
    c = TestClient(create_api_app(s))
    g = c.get("/embed/v1/graph", params={"urn": ROOT}, headers=ANA).json()
    assert {n["urn"] for n in g["nodes"]} == {ROOT, ORDERS, RAW, PAY, DASH, OLD}
    assert g["hidden_count"] == 1  # the payroll table: counted, never named
    edges = {(e["source"], e["target"], e["indirect"]) for e in g["edges"]}
    assert edges == {
        (ORDERS, ROOT, False), (RAW, ORDERS, False),  # along the path: raw.orders feeds orders, which feeds the asset
        (PAY, ROOT, True),                            # through the hidden table: joined around it, dashed
        (ROOT, DASH, False),
        (ROOT, OLD, True),                            # no path: to the asset, dashed (two hops away)
    }
    assert all(SECRET not in (e["source"], e["target"]) for e in g["edges"])
    # Depth 1: only direct neighbours.
    g1 = c.get("/embed/v1/graph", params={"urn": ROOT, "depth": 1}, headers=ANA).json()
    assert RAW not in {n["urn"] for n in g1["nodes"]}
    # An asset the user may not see is refused; signing in is required.
    assert c.get("/embed/v1/graph", params={"urn": SECRET}, headers=ANA).status_code == 403
    assert c.get("/embed/v1/graph", params={"urn": ROOT}).status_code == 401
    assert c.get("/embed/v1/graph", params={"urn": ROOT, "depth": 9}, headers=ANA).status_code == 422


def test_the_graph_filters_neighbours_as_the_caller(settings, graph):
    """Neighbours are checked as the caller is, not as the person behind it: an extension scoped to one asset
    (or an agent held to its intent) sees that asset's graph without the assets outside its scope."""
    from cairn.common.security import Principal

    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[Policy(id="see-all", effect="allow", actions=["VIEW_ENTITY_PAGE"])]))
    s.embed.catalog = Catalog()
    s.extensions.allows = lambda actor, privilege, urn: privilege == "VIEW_ENTITY_PAGE" and urn in (ROOT, DASH)
    app = create_api_app(s)
    ext = Principal(urn="urn:li:corpuser:cairn-ext-lineage", via="extension")

    async def as_extension(scope, receive, send):
        scope["cairn.principal"] = ext
        await app(scope, receive, send)

    g = TestClient(as_extension).get("/embed/v1/graph", params={"urn": ROOT}).json()
    assert {n["urn"] for n in g["nodes"]} == {ROOT, DASH}
    assert g["hidden_count"] == 5


def test_lineage_paths_do_not_reach_other_callers(settings, graph):
    """Paths name what lies between. Only the graph, which steps over hidden assets, may ask for them: the
    assistant's lineage tool filters results by visibility, and a path through a hidden table must not name it."""
    from cairn.ai.mcp_server import CairnTools, _principal
    from cairn.common.security import Principal

    def entity(u):
        return {"urn": u, "type": "DATASET", "name": u.split(",")[1], "platform": {"name": "trino"}}

    def handler(query, variables):
        if "searchAcrossLineage" in query and variables["input"]["direction"] == "UPSTREAM":
            return {"searchAcrossLineage": {"searchResults": [
                {"degree": 1, "entity": entity(SECRET), "paths": [{"path": [{"urn": ROOT}, {"urn": SECRET}]}]},
                {"degree": 2, "entity": entity(PAY),
                 "paths": [{"path": [{"urn": ROOT}, {"urn": SECRET}, {"urn": PAY}]}]}]}}
        return {}

    graph.graphql_handler = handler
    s = build_services(settings, graph=graph, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="see-all", effect="allow", actions=["VIEW_ENTITY_PAGE"]),
        Policy(id="no-hr", effect="deny", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=["*hr.*"])),
    ]))
    token = _principal.set(Principal("urn:li:corpuser:ana", "dev"))
    try:
        out = CairnTools(s).get_lineage(ROOT, "UPSTREAM", 2)
    finally:
        _principal.reset(token)
    assert [r["urn"] for r in out["results"]] == [PAY] and out["hidden_count"] == 1
    assert SECRET not in json.dumps(out)
    # The graph still follows the path, around the hidden table.
    g = s.embed.lineage({"sub": "urn:li:corpuser:ana"}, ROOT, 2)
    assert {(e["source"], e["target"], e["indirect"]) for e in g["edges"]} == {(PAY, ROOT, True)}
    assert SECRET not in json.dumps(g)


def test_a_hidden_asset_on_both_sides_counts_once(settings, graph):
    """In a cycle the same hidden asset is both upstream and downstream: it is one asset left out, not two."""

    class Cycle(Catalog):
        def lineage(self, urn, direction, depth, count, with_paths=False):
            return [{"urn": SECRET, "degree": 1, "name": "payroll", "paths": [[ROOT, SECRET]]},
                    {"urn": ORDERS, "degree": 1, "name": "orders", "paths": [[ROOT, ORDERS]]}]

    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="see-all", effect="allow", actions=["VIEW_ENTITY_PAGE"]),
        Policy(id="no-hr", effect="deny", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=["*hr.*"])),
    ]))
    s.embed.catalog = Cycle()
    g = s.embed.lineage({"sub": "urn:li:corpuser:ana"}, ROOT, 1)
    assert g["hidden_count"] == 1
    assert {n["urn"] for n in g["nodes"]} == {ROOT, ORDERS}
