"""The asset page's lineage graph (GET /embed/v1/graph): the lineage around an asset for the signed-in
user, with the assets they may not see left out and counted."""

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

    def lineage(self, urn, direction, depth, count):
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
