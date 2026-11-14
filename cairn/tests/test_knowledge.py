"""Knowledge sources: pages from Confluence, Notion, Google Docs and SharePoint are synced, linked to
the datasets they name, published as asset documents, and found by meaning for the assistant."""

import json

import httpx
from fastapi.testclient import TestClient

from cairn.ai.embeddings import HashingEmbedder
from cairn.app import create_api_app
from cairn.context.knowledge import KnowledgeService, KnowledgeSource, html_text, links_in
from cairn.governance.abac import Policy, PolicyBundle
from cairn.services import build_services

ADMIN, ANA = "urn:li:corpuser:admin", "urn:li:corpuser:ana"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.core.orders,PROD)"
REFUNDS = "urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.core.order_refunds,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:postgres,shop.public.orders,PROD)"


def test_text_and_links():
    assert html_text("<h1>Orders</h1><p>Net of <b>refunds</b>&nbsp;only.</p><script>x()</script>") == \
        "Orders\nNet of refunds only."
    text = ("Revenue comes from core.orders. See https://cairn.example.com/cairn/asset/"
            "urn%3Ali%3Adataset%3A(urn%3Ali%3AdataPlatform%3Apostgres%2Cshop.public.orders%2CPROD) and "
            "order_refunds (order_refunds is daily).")
    assert links_in(text, [ORDERS, REFUNDS, OTHER]) == sorted([ORDERS, REFUNDS, OTHER])
    assert links_in("we take orders by phone", [ORDERS]) == []                 # a plain word is not a link


def apis(req: httpx.Request) -> httpx.Response:
    u, host = req.url, req.url.host
    if host == "acme.atlassian.net":
        assert req.headers["authorization"].startswith("Basic ")
        return httpx.Response(200, json={"results": [{
            "id": "101", "title": "Orders: how revenue is counted", "_links": {"webui": "/spaces/DATA/pages/101"},
            "version": {"when": "2026-10-01T10:00:00Z"},
            "body": {"storage": {"value": "<p>Net revenue is computed from <code>core.orders</code> minus refunds.</p>"}}}],
            "_links": {}})
    if host == "api.notion.com":
        assert req.headers["notion-version"] == "2022-06-28"
        if u.path.endswith("/query"):
            return httpx.Response(200, json={"results": [{"id": "p-1", "url": "https://www.notion.so/p1",
                                                           "last_edited_time": "2026-10-02T00:00:00Z",
                                                           "properties": {"Name": {"type": "title", "title": [
                                                               {"plain_text": "Refund policy"}]}}}], "has_more": False})
        return httpx.Response(200, json={"results": [
            {"id": "b1", "type": "paragraph", "has_children": False,
             "paragraph": {"rich_text": [{"plain_text": "Refunds land in order_refunds within a day."}]}},
            {"id": "b2", "type": "heading_2", "has_children": False,
             "heading_2": {"rich_text": [{"plain_text": "Why order_refunds lags"}]}}], "has_more": False})
    if host == "www.googleapis.com":
        if u.path.endswith("/export"):
            return httpx.Response(200, text="Churn is measured weekly from the customers table.")
        return httpx.Response(200, json={"files": [{"id": "g1", "name": "Churn spec", "modifiedTime": "2026-10-03T00:00:00Z",
                                                    "webViewLink": "https://docs.google.com/document/d/g1"}]})
    if host == "login.microsoftonline.com":
        return httpx.Response(200, json={"access_token": "mtok"})
    if host == "graph.microsoft.com":
        assert req.headers["authorization"] == "Bearer mtok"
        return httpx.Response(200, json={"value": [{"id": "s1", "title": "Data access process",
                                                    "webUrl": "https://acme.sharepoint.com/sites/data/SitePages/access.aspx",
                                                    "lastModifiedDateTime": "2026-10-04T00:00:00Z",
                                                    "canvasLayout": {"horizontalSections": [{"columns": [{"webparts": [
                                                        {"innerHtml": "<p>Request access in the Cairn marketplace.</p>"}]}]}]}}]})
    return httpx.Response(404)


SOURCES = {
    "wiki": KnowledgeSource(id="wiki", type="confluence", url="https://acme.atlassian.net", space="DATA",
                            user="bot@acme.com", token="t"),
    "handbook": KnowledgeSource(id="handbook", type="notion", database="db1", token="n"),
    "specs": KnowledgeSource(id="specs", type="gdocs", folder="f1", token="g"),
    "portal": KnowledgeSource(id="portal", type="sharepoint", site="s", tenant_id="t", client_id="c", client_secret="x",
                              admins_only=True),
}


def service(engine, graph):
    emb = HashingEmbedder()
    return KnowledgeService(engine, SOURCES, lambda t: emb.embed([t])[0], lambda: [ORDERS, REFUNDS], graph,
                            http=httpx.Client(transport=httpx.MockTransport(apis)))


def test_sync_link_publish_and_search(engine, graph):
    graph.aspects[(ORDERS, "institutionalMemory")] = {"elements": [{"url": "https://runbook", "description": "Runbook",
                                                                    "createStamp": {"time": 0, "actor": ANA}}]}
    kn = service(engine, graph)
    out = kn.sync()
    assert {k: v["pages"] for k, v in out.items()} == {"wiki": 1, "handbook": 1, "specs": 1, "portal": 1}
    assert out["wiki"]["links"] == 1 and out["handbook"]["links"] == 1
    docs = graph.aspects[(ORDERS, "institutionalMemory")]["elements"]
    assert docs[0]["description"] == "Runbook"                                  # people's links are kept
    assert docs[1]["url"] == "https://acme.atlassian.net/wiki/spaces/DATA/pages/101"
    assert kn.for_dataset(REFUNDS) == [{"title": "Refund policy", "url": "https://www.notion.so/p1", "source": "handbook",
                                        "updated_ms": kn.for_dataset(REFUNDS)[0]["updated_ms"]}]
    hits = kn.search("how is net revenue counted")
    assert hits[0]["title"] == "Orders: how revenue is counted" and "Net revenue" in hits[0]["excerpt"]
    assert not any(h["source"] == "portal" for h in kn.search("request access process"))       # admins only
    assert kn.search("request access process", is_admin=True)[0]["source"] == "portal"
    before = len(graph.emitted)
    kn.sync()
    assert len(graph.emitted) == before                                        # nothing changed, nothing re-sent


def test_api_and_tool(settings, graph, engine):
    from cairn.ai.mcp_server import CairnTools, _principal
    from cairn.common.security import Principal

    s = build_services(settings, graph=graph, with_ai=False, with_gateway=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="admin", effect="allow", actions=["*"], condition={"attr": "subject.urn", "op": "eq", "value": ADMIN}),
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"], condition={"attr": "subject.urn", "op": "exists"})]))
    s.knowledge = service(s.engine, graph)
    c = TestClient(create_api_app(s))
    assert c.post("/context/knowledge/sync", headers={"X-Cairn-Actor": ANA}).status_code == 403
    assert c.post("/context/knowledge/sync", headers={"X-Cairn-Actor": ADMIN}).json()["wiki"]["pages"] == 1
    assert [x["id"] for x in c.get("/context/knowledge", headers={"X-Cairn-Actor": ADMIN}).json()] == list(SOURCES)
    hits = c.get("/context/knowledge/search", params={"q": "refunds daily"}, headers={"X-Cairn-Actor": ANA}).json()
    assert hits[0]["title"] == "Refund policy"
    assert c.get("/context/knowledge/for", params={"urn": ORDERS}, headers={"X-Cairn-Actor": ANA}).json()[0]["source"] == "wiki"
    token = _principal.set(Principal(ANA, "agent", agent="agt_x", scopes=("read",)))
    try:
        pages = CairnTools(s).search_knowledge("net revenue")["pages"]
    finally:
        _principal.reset(token)
    assert pages[0]["url"].startswith("https://acme.atlassian.net/") and json.dumps(pages)
