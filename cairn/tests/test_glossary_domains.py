"""The glossary and domains edited through Cairn (``/catalog/glossary``, ``/catalog/domains``, ``/catalog/assets``),
written to Cairn Core: the aspects written, who may change what (MANAGE_GLOSSARIES scoped to a term group,
MANAGE_DOMAINS on a domain, EDIT_ENTITY_GLOSSARY_TERMS / EDIT_DATASET_COL_GLOSSARY_TERMS / EDIT_ENTITY_DOMAINS on
an asset, and seeing it), refusals that keep the graph as it was, deletes (soft for the glossary, Core's own for a
domain, refused while in use unless forced), entries made in Core's UI keeping what Cairn does not manage, the audit
log, a term change proposed as a change request, the CLI and the MCP tools. Core's search and mutations are
answered by tests/core_standin.py; tests/e2e/test_ui_catalog_live.py does the same against the real Cairn Core."""

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core_standin import CoreStandIn  # noqa: E402

from cairn.app import create_api_app  # noqa: E402
from cairn.governance.abac import Policy, PolicyBundle, Target  # noqa: E402
from cairn.services import build_services  # noqa: E402

ALICE, BOB, CAROL = "urn:li:corpuser:alice", "urn:li:corpuser:bob", "urn:li:corpuser:carol"
ORDERS = "urn:li:dataset:(urn:li:dataPlatform:iceberg,sales.orders,PROD)"
SECRET = "urn:li:dataset:(urn:li:dataPlatform:iceberg,hr.salaries,PROD)"
FINANCE_NODE, SALES_NODE = "urn:li:glossaryNode:finance", "urn:li:glossaryNode:sales"
REVENUE = "urn:li:glossaryTerm:revenue"
SALES = "urn:li:domain:sales"


def who(urn):
    return {"attr": "subject.urn", "op": "eq", "value": urn}


@pytest.fixture
def api(settings, graph):
    for ds, cols in ((ORDERS, ("id", "amount")), (SECRET, ("id", "salary"))):
        graph.aspects[(ds, "datasetProperties")] = {"name": ds.split(",")[1]}
        graph.aspects[(ds, "schemaMetadata")] = {"fields": [{"fieldPath": c} for c in cols]}
    graph.graphql_handler = CoreStandIn(graph)
    s = build_services(settings, graph=graph, with_ai=False)
    s.pdp.set_bundle(PolicyBundle(policies=[
        Policy(id="view", effect="allow", actions=["VIEW_ENTITY_PAGE"]),
        Policy(id="no-hr", effect="deny", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=[SECRET]),
               condition=who(BOB)),
        # Alice runs the glossary and the domains; Bob stewards the finance group only; Carol labels assets.
        Policy(id="alice", effect="allow", actions=["MANAGE_GLOSSARIES", "MANAGE_DOMAINS", "MANAGE_DATA_PRODUCTS",
                                                     "EDIT_ENTITY_GLOSSARY_TERMS", "EDIT_ENTITY_DOMAINS",
                                                     "EDIT_DATASET_COL_GLOSSARY_TERMS"], condition=who(ALICE)),
        Policy(id="bob-finance", effect="allow", actions=["MANAGE_GLOSSARIES"],
               target=Target(resource_urns=[FINANCE_NODE]), condition=who(BOB)),
        Policy(id="carol", effect="allow", actions=["EDIT_ENTITY_GLOSSARY_TERMS", "EDIT_ENTITY_DOMAINS"],
               target=Target(resource_urns=[ORDERS]), condition=who(CAROL)),
    ]))
    return TestClient(create_api_app(s)), s, graph


def as_(urn):
    return {"X-Cairn-Actor": urn}


def seed_glossary(c):
    for node in ({"id": "finance", "name": "Finance", "description": "Money"},
                 {"id": "ledger", "name": "Ledger", "parent": "finance"},
                 {"id": "sales", "name": "Sales"}):
        r = c.post("/catalog/glossary/nodes", json=node, headers=as_(ALICE))
        assert r.status_code == 201, r.text


def test_term_groups_and_terms_are_written_to_core(api):
    c, s, graph = api
    assert c.post("/catalog/glossary/nodes", json={"id": "sales", "name": "Sales"}, headers=as_(BOB)).status_code == 403
    seed_glossary(c)
    assert graph.aspects[("urn:li:glossaryNode:ledger", "glossaryNodeInfo")] == {
        "name": "Ledger", "definition": "", "customProperties": {}, "parentNode": FINANCE_NODE}
    body = {"id": "revenue", "name": "Revenue", "definition": "Money **earned**", "parent": "urn:li:glossaryNode:ledger",
            "owners": ["olga"], "stewards": ["bob"], "custom_properties": {"source": "IFRS 15"},
            "source_url": "https://wiki.example.com/revenue"}
    # Bob manages the finance group: a term in a group inside it is his to make.
    r = c.post("/catalog/glossary/terms", json=body, headers=as_(BOB))
    assert r.status_code == 201, r.text
    assert r.json() == {"id": "revenue", "urn": REVENUE, "name": "Revenue", "definition": "Money **earned**",
                        "parent": "ledger", "is_a": [], "has_a": [], "related": [], "owners": ["urn:li:corpuser:olga"],
                        "stewards": [BOB], "custom_properties": {"source": "IFRS 15"},
                        "source_url": "https://wiki.example.com/revenue", "deprecated": False, "deprecation_note": ""}
    info = graph.aspects[(REVENUE, "glossaryTermInfo")]
    assert info["termSource"] == "INTERNAL" and info["parentNode"] == "urn:li:glossaryNode:ledger"
    owners = graph.aspects[(REVENUE, "ownership")]["owners"]
    assert {(o["owner"], o["type"]) for o in owners} == {("urn:li:corpuser:olga", "TECHNICAL_OWNER"), (BOB, "DATA_STEWARD")}
    # ... but not in the sales group, nor moving it there.
    assert c.post("/catalog/glossary/terms", json={**body, "id": "deal", "parent": "sales"}, headers=as_(BOB)).status_code == 403
    assert c.put("/catalog/glossary/terms/revenue", json={**body, "parent": "sales"}, headers=as_(BOB)).status_code == 403
    assert graph.aspects[(REVENUE, "glossaryTermInfo")]["parentNode"] == "urn:li:glossaryNode:ledger"
    # Related terms, read back; a related term that does not exist is refused.
    assert c.post("/catalog/glossary/terms", json={"id": "income", "name": "Income", "parent": "finance"},
                  headers=as_(BOB)).status_code == 201
    bad = c.put("/catalog/glossary/terms/revenue", json={**body, "is_a": ["nope"]}, headers=as_(BOB))
    assert bad.status_code == 422 and "no glossary term nope" in bad.text
    r = c.put("/catalog/glossary/terms/revenue", json={**body, "is_a": ["income"], "related": ["urn:li:glossaryTerm:income"]},
              headers=as_(BOB))
    assert r.status_code == 200, r.text
    assert graph.aspects[(REVENUE, "glossaryRelatedTerms")] == {
        "isRelatedTerms": ["urn:li:glossaryTerm:income"], "hasRelatedTerms": [], "relatedTerms": ["urn:li:glossaryTerm:income"]}
    got = c.get("/catalog/glossary/terms/revenue", headers=as_(CAROL)).json()
    assert got["is_a"] == ["income"] and got["related"] == ["income"]
    assert c.post("/catalog/glossary/terms", json={**body, "is_a": ["revenue"]}, headers=as_(ALICE)).status_code == 422
    assert c.post("/catalog/glossary/terms", json=body, headers=as_(ALICE)).status_code == 409
    assert [t["id"] for t in c.get("/catalog/glossary/terms", headers=as_(CAROL)).json()["terms"]] == ["income", "revenue"]
    assert [n["id"] for n in c.get("/catalog/glossary/nodes", headers=as_(CAROL)).json()["nodes"]] == ["finance", "ledger", "sales"]
    # Deprecated, with a note; the audit log has every change.
    r = c.put("/catalog/glossary/terms/revenue", json={**body, "deprecated": True, "deprecation_note": "use income"},
              headers=as_(BOB))
    assert r.json()["deprecated"] and graph.aspects[(REVENUE, "deprecation")] == {
        "deprecated": True, "note": "use income", "actor": BOB}
    actions = [e["action"] for e in s.audit.entries("glossary", 50)]
    assert actions.count("glossary.term_updated") == 2 and actions.count("glossary.node_created") == 3


def test_rights_say_why(api):
    c, _, _ = api
    seed_glossary(c)
    top = c.get("/catalog/glossary/rights", headers=as_(BOB)).json()
    assert top["may_manage"] is False and "MANAGE_GLOSSARIES" in top["reason"]
    assert c.get("/catalog/glossary/rights", params={"urn": "urn:li:glossaryNode:ledger"}, headers=as_(BOB)).json()["may_manage"]
    assert not c.get("/catalog/glossary/rights", params={"urn": SALES_NODE}, headers=as_(BOB)).json()["may_manage"]
    assert c.get("/catalog/domains/rights", headers=as_(ALICE)).json() == {"may_manage": True, "reason": ""}
    assert not c.get("/catalog/domains/rights", headers=as_(CAROL)).json()["may_manage"]


def test_groups_cannot_loop_and_full_groups_stay(api):
    c, _, graph = api
    seed_glossary(c)
    loop = c.put("/catalog/glossary/nodes/finance", json={"id": "finance", "name": "Finance", "parent": "ledger"},
                 headers=as_(ALICE))
    assert loop.status_code == 422
    assert c.delete("/catalog/glossary/nodes/finance", headers=as_(ALICE)).status_code == 409    # ledger is in it
    assert c.delete("/catalog/glossary/nodes/ledger", headers=as_(CAROL)).status_code == 403
    assert c.delete("/catalog/glossary/nodes/ledger", headers=as_(ALICE)).status_code == 204
    assert graph.aspects[("urn:li:glossaryNode:ledger", "status")] == {"removed": True}
    assert c.get("/catalog/glossary/nodes/ledger", headers=as_(ALICE)).status_code == 404
    assert c.post("/catalog/glossary/nodes", json={"id": "ledger", "name": "Ledger 2"}, headers=as_(ALICE)).status_code == 201
    assert graph.aspects[("urn:li:glossaryNode:ledger", "status")] == {"removed": False}


def test_terms_on_assets_and_columns(api):
    c, s, graph = api
    seed_glossary(c)
    assert c.post("/catalog/glossary/terms", json={"id": "revenue", "name": "Revenue", "parent": "finance"},
                  headers=as_(ALICE)).status_code == 201
    on = {"asset": ORDERS, "term": "revenue"}
    assert c.post("/catalog/assets/terms", json=on, headers=as_(BOB)).status_code == 403          # no right on the asset
    r = c.post("/catalog/assets/terms", json=on, headers=as_(CAROL))
    assert r.status_code == 200 and r.json()["changed"], r.text
    assert graph.aspects[(ORDERS, "glossaryTerms")]["terms"] == [{"urn": REVENUE}]
    assert graph.emitted[-1]["changeType"] == "PATCH"                                             # Core applies it atomically
    assert not c.post("/catalog/assets/terms", json=on, headers=as_(CAROL)).json()["changed"]     # already there
    # A column: Carol has no column right; Alice has. A column that does not exist is refused.
    col = {**on, "field": "amount"}
    assert c.post("/catalog/assets/terms", json=col, headers=as_(CAROL)).status_code == 403
    assert c.post("/catalog/assets/terms", json={**on, "field": "nope"}, headers=as_(ALICE)).status_code == 404
    assert c.post("/catalog/assets/terms", json=col, headers=as_(ALICE)).status_code == 200
    esm = graph.aspects[(ORDERS, "editableSchemaMetadata")]["editableSchemaFieldInfo"]
    assert esm[0]["fieldPath"] == "amount" and esm[0]["glossaryTerms"]["terms"] == [{"urn": REVENUE}]
    labels = c.get("/catalog/assets/labels", params={"urn": ORDERS}, headers=as_(CAROL)).json()
    assert labels["terms"] == [REVENUE] and labels["columns"]["amount"] == [REVENUE]
    assert labels["may"] == {"terms": True, "column_terms": False, "domain": True}
    assert "EDIT_DATASET_COL_GLOSSARY_TERMS" in labels["why"]["column_terms"]
    # The assets using it, from Core's search; a term no one may see is not applied.
    assets = c.get("/catalog/glossary/terms/revenue/assets", headers=as_(CAROL)).json()["assets"]
    assert [a["urn"] for a in assets] == [ORDERS]
    # An asset the caller cannot see: refused (Bob may not see the salaries).
    assert c.post("/catalog/assets/terms", json={"asset": SECRET, "term": "revenue"}, headers=as_(BOB)).status_code == 403
    # A deprecated term is not applied again; it still comes off.
    assert c.put("/catalog/glossary/terms/revenue", json={"id": "revenue", "name": "Revenue", "parent": "finance",
                                                          "deprecated": True, "deprecation_note": "use income"},
                 headers=as_(ALICE)).status_code == 200
    dep = c.post("/catalog/assets/terms", json={"asset": SECRET, "term": "revenue"}, headers=as_(ALICE))
    assert dep.status_code == 409 and "use income" in dep.text
    assert c.post("/catalog/assets/terms/remove", json=on, headers=as_(CAROL)).json()["changed"]
    assert graph.aspects[(ORDERS, "glossaryTerms")]["terms"] == []
    # Deleting a term still on a column: refused, then forced (Alice may take it off), soft (status).
    assert c.delete("/catalog/glossary/terms/revenue", headers=as_(ALICE)).status_code == 409
    assert c.delete("/catalog/glossary/terms/revenue", params={"force": "true"}, headers=as_(CAROL)).status_code == 403
    assert c.delete("/catalog/glossary/terms/revenue", params={"force": "true"}, headers=as_(ALICE)).status_code == 204
    assert graph.aspects[(ORDERS, "editableSchemaMetadata")]["editableSchemaFieldInfo"][0]["glossaryTerms"]["terms"] == []
    assert graph.aspects[(REVENUE, "status")] == {"removed": True}
    assert c.get("/catalog/glossary/terms/revenue", headers=as_(ALICE)).status_code == 404
    actions = [e["action"] for e in s.audit.entries("glossary", 50)]
    assert {"glossary.term_applied", "glossary.term_unapplied", "glossary.term_deleted"} <= set(actions)


def test_domains(api):
    c, s, graph = api
    assert c.post("/catalog/domains", json={"id": "sales", "name": "Sales"}, headers=as_(CAROL)).status_code == 403
    r = c.post("/catalog/domains", json={"id": "sales", "name": "Sales", "description": "Selling", "owners": ["olga"]},
               headers=as_(ALICE))
    assert r.status_code == 201, r.text
    assert r.json() == {"id": "sales", "urn": SALES, "name": "Sales", "description": "Selling", "parent": None,
                        "owners": ["urn:li:corpuser:olga"]}
    props = graph.aspects[(SALES, "domainProperties")]
    assert props["name"] == "Sales" and props["created"]["actor"] == ALICE
    r = c.post("/catalog/domains", json={"id": "emea", "name": "EMEA sales", "parent": "urn:li:domain:sales"},
               headers=as_(ALICE))
    assert r.status_code == 201 and graph.aspects[("urn:li:domain:emea", "domainProperties")]["parentDomain"] == SALES
    assert c.put("/catalog/domains/sales", json={"id": "sales", "name": "Sales", "parent": "emea"},
                 headers=as_(ALICE)).status_code == 422                                              # a loop
    # An asset's domain: Carol may on the orders; nobody puts a hidden asset in a domain.
    assert c.put("/catalog/assets/domain", json={"asset": ORDERS, "domain": "emea"}, headers=as_(BOB)).status_code == 403
    assert c.put("/catalog/assets/domain", json={"asset": ORDERS, "domain": "nope"}, headers=as_(CAROL)).status_code == 404
    r = c.put("/catalog/assets/domain", json={"asset": ORDERS, "domain": "emea"}, headers=as_(CAROL))
    assert r.status_code == 200 and r.json()["changed"]
    assert graph.aspects[(ORDERS, "domains")] == {"domains": ["urn:li:domain:emea"]}
    contents = c.get("/catalog/domains/emea/assets", headers=as_(CAROL)).json()
    assert [a["urn"] for a in contents["assets"]] == [ORDERS] and contents["products"] == []
    # A product in it too (made through /catalog/products).
    assert c.post("/catalog/products", json={"id": "o360", "name": "Orders 360", "domain": "urn:li:domain:emea",
                                             "assets": [ORDERS]}, headers=as_(ALICE)).status_code == 201
    # Deleting: refused with a domain inside, and while assets are in it unless forced by someone who may.
    assert c.delete("/catalog/domains/sales", headers=as_(ALICE)).status_code == 409
    full = c.delete("/catalog/domains/emea", headers=as_(ALICE))
    assert full.status_code == 409 and "1 assets and 1 data products" in full.text
    assert c.delete("/catalog/domains/emea", params={"force": "true"}, headers=as_(CAROL)).status_code == 403
    assert c.delete("/catalog/domains/emea", params={"force": "true"}, headers=as_(ALICE)).status_code == 204
    assert graph.aspects[(ORDERS, "domains")] == {"domains": []}
    assert graph.aspects[("urn:li:dataProduct:o360", "domains")] == {"domains": []}
    assert ("urn:li:domain:emea", "domainProperties") not in graph.aspects                     # Core's deleteDomain
    assert c.get("/catalog/domains/emea", headers=as_(ALICE)).status_code == 404
    assert [d["id"] for d in c.get("/catalog/domains", headers=as_(CAROL)).json()["domains"]] == ["sales"]
    assert c.put("/catalog/assets/domain", json={"asset": "urn:li:dataProduct:o360", "domain": "sales"},
                 headers=as_(ALICE)).status_code == 422
    actions = [e["action"] for e in s.audit.entries("domains", 50)]
    assert {"domains.created", "domains.asset_set", "domains.emptied", "domains.deleted"} <= set(actions)


def test_core_refusing_is_said_and_audited(api):
    c, s, graph = api

    def refuse(mcps):
        raise RuntimeError("Cairn Core POST /aspects failed: 500")
    graph.emit = refuse
    r = c.post("/catalog/domains", json={"id": "sales", "name": "Sales"}, headers=as_(ALICE))
    assert r.status_code == 502 and "did not take the change" in r.text
    assert [e["action"] for e in s.audit.entries("domains", 5)] == ["domains.created_failed"]
    graph.graphql_handler = None                       # Core's search does not answer: no delete on a guess
    del graph.emit
    assert c.post("/catalog/domains", json={"id": "sales", "name": "Sales"}, headers=as_(ALICE)).status_code == 201
    assert c.delete("/catalog/domains/sales", headers=as_(ALICE)).status_code == 503


def test_entries_made_in_core_keep_what_cairn_does_not_manage(api):
    c, _, graph = api
    stamp = {"time": 1, "actor": "urn:li:corpuser:datahub"}
    graph.aspects[(REVENUE, "glossaryTermInfo")] = {"name": "Revenue", "definition": "x", "termSource": "EXTERNAL",
                                                    "sourceRef": "FIBO", "rawSchema": "{}"}
    graph.aspects[(REVENUE, "glossaryRelatedTerms")] = {"values": ["urn:li:glossaryTerm:v"]}
    graph.aspects[(REVENUE, "ownership")] = {"owners": [{"owner": BOB, "type": "BUSINESS_OWNER", "source": {"type": "MANUAL"}}],
                                             "lastModified": stamp}
    r = c.put("/catalog/glossary/terms/revenue", json={"id": "revenue", "name": "Revenue", "definition": "y",
                                                       "owners": [BOB, CAROL]}, headers=as_(ALICE))
    assert r.status_code == 200, r.text
    info = graph.aspects[(REVENUE, "glossaryTermInfo")]
    assert info["termSource"] == "EXTERNAL" and info["sourceRef"] == "FIBO" and info["rawSchema"] == "{}"
    assert graph.aspects[(REVENUE, "glossaryRelatedTerms")]["values"] == ["urn:li:glossaryTerm:v"]
    owners = {o["owner"]: o for o in graph.aspects[(REVENUE, "ownership")]["owners"]}
    assert owners[BOB]["type"] == "BUSINESS_OWNER" and owners[BOB]["source"] == {"type": "MANUAL"}
    assert owners[CAROL]["type"] == "TECHNICAL_OWNER"


def test_a_term_change_is_proposed_and_approved(api, monkeypatch):
    c, s, graph = api
    seed_glossary(c)
    assert c.post("/catalog/glossary/terms", json={"id": "revenue", "name": "Revenue", "parent": "finance"},
                  headers=as_(ALICE)).status_code == 201
    # Carol may not change the term: she proposes it; Alice (who may approve glossary changes) approves and applies.
    assert c.put("/catalog/glossary/terms/revenue", json={"id": "revenue", "name": "Revenue", "parent": "finance",
                                                          "definition": "Money earned"}, headers=as_(CAROL)).status_code == 403
    rights = c.get("/catalog/glossary/rights", params={"urn": REVENUE}, headers=as_(CAROL)).json()
    assert rights["may_manage"] is False and rights["may_propose"] is True
    r = c.post("/catalog/glossary/proposals", json={"id": "revenue", "name": "Revenue", "parent": "finance",
                                                    "definition": "Money earned", "intent": "The definition was empty"},
               headers=as_(CAROL))
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert r.json()["status"] == "proposed" and r.json()["plan"]["counts"]["update"] == 1
    assert graph.aspects[(REVENUE, "glossaryTermInfo")]["definition"] == ""                    # not changed yet
    s.pdp.set_bundle(PolicyBundle(policies=[*s.pdp.bundle.policies,
                                            Policy(id="approve", effect="allow", actions=["APPROVE_IAC_CHANGE"],
                                                   condition=who(ALICE))]))
    assert c.post(f"/v1/iac/changes/{cid}/approve", json={}, headers=as_(ALICE)).status_code == 200
    applied = c.post(f"/v1/iac/changes/{cid}/apply", json={}, headers=as_(ALICE))
    assert applied.status_code == 200 and applied.json()["status"] == "applied", applied.text
    assert graph.aspects[(REVENUE, "glossaryTermInfo")]["definition"] == "Money earned"
    assert c.post("/catalog/glossary/proposals", json={"id": "revenue", "name": "Revenue"},
                  headers=as_(CAROL)).status_code == 400                                       # no intent


def test_the_mcp_tools_run_as_the_caller(api):
    from cairn.ai.mcp_server import CairnTools, _principal
    from cairn.common.security import Principal
    from cairn.identity.agents import tool_scope
    from cairn.services import Forbidden

    c, s, graph = api
    seed_glossary(c)
    assert c.post("/catalog/glossary/terms", json={"id": "revenue", "name": "Revenue", "definition": "Money earned",
                                                   "parent": "finance"}, headers=as_(ALICE)).status_code == 201
    assert c.post("/catalog/domains", json={"id": "sales", "name": "Sales"}, headers=as_(ALICE)).status_code == 201
    tools = CairnTools(s)
    token = _principal.set(Principal(urn=CAROL, via="token"))
    try:
        assert [t["id"] for t in tools.glossary_search("money")["terms"]] == ["revenue"]
        assert tools.apply_term(ORDERS, "revenue")["changed"]
        assert graph.aspects[(ORDERS, "glossaryTerms")]["terms"] == [{"urn": REVENUE}]
        assert tools.set_domain(ORDERS, "urn:li:domain:sales")["domain"] == SALES
        with pytest.raises(Forbidden):
            tools.apply_term(ORDERS, "revenue", column="amount")       # no column right
        with pytest.raises(ValueError):
            tools.apply_term(ORDERS, "bad id!")
    finally:
        _principal.reset(token)
    token = _principal.set(Principal(urn=BOB, via="agent", agent="a1", scopes=("read", "write")))
    try:
        with pytest.raises(Forbidden):
            tools.set_domain(ORDERS, None)
    finally:
        _principal.reset(token)
    assert graph.aspects[(ORDERS, "domains")] == {"domains": [SALES]}
    assert tool_scope("apply_term") == tool_scope("set_domain") == "write" and tool_scope("glossary_search") == "read"


def test_the_cli_calls_the_api_as_you(api, monkeypatch, capsys):
    from cairn.cli import build_parser
    from cairn.flows.cli import CliError
    from cairn.marketplace import catalog_cli

    c, _, graph = api
    seed_glossary(c)
    assert c.post("/catalog/glossary/terms", json={"id": "revenue", "name": "Revenue", "parent": "finance"},
                  headers=as_(ALICE)).status_code == 201
    assert c.post("/catalog/domains", json={"id": "sales", "name": "Sales"}, headers=as_(ALICE)).status_code == 201

    def as_who(urn):
        def call(method, path, body=None):
            r = c.request(method, path, json=body, headers=as_(urn))
            if r.status_code >= 300:
                raise CliError(f"{method} {path}: {r.status_code} {r.json().get('detail')}")
            return r.json() if r.content else None
        return lambda: call
    parser = build_parser()

    def run(*argv):
        a = parser.parse_args(list(argv))
        with pytest.raises(SystemExit) as done:
            a.fn(a)
        return done.value.code, capsys.readouterr().out
    monkeypatch.setattr(catalog_cli, "_call", as_who(CAROL))
    assert run("glossary", "terms") == (0, "revenue\tRevenue\tfinance\t\n")
    assert run("glossary", "apply", "urn:li:glossaryTerm:revenue", ORDERS)[0] == 0
    assert graph.aspects[(ORDERS, "glossaryTerms")]["terms"] == [{"urn": REVENUE}]
    code, out = run("glossary", "apply", "revenue", ORDERS, "--column", "amount")
    assert code == 1 and "403" in out
    assert run("glossary", "assets", "revenue")[1].startswith(ORDERS)
    assert run("domains", "set", ORDERS, "urn:li:domain:sales")[0] == 0
    assert graph.aspects[(ORDERS, "domains")] == {"domains": [SALES]}
    assert run("domains", "assets", "sales")[1].startswith(ORDERS)
    monkeypatch.setattr(catalog_cli, "_call", as_who(ALICE))
    assert run("glossary", "deprecate", "revenue", "--note", "use income") == (0, "revenue deprecated\n")
    assert graph.aspects[(REVENUE, "deprecation")]["note"] == "use income"
    assert run("domains", "delete", "sales")[0] == 1
    assert run("domains", "delete", "sales", "--force") == (0, "sales deleted\n")


def test_a_child_just_made_keeps_its_parent_from_being_deleted(api):
    """Core lists domains, groups and terms from its search index, a moment behind its writes (CI run 396 deleted a
    domain three seconds after a domain was made inside it). Cairn waits until Core lists what it has just made in a
    parent, so the parent's delete right after is refused."""
    c, s, graph = api
    real = graph.scroll_entities
    lag: dict[str, int] = {}

    def lagging(etype, aspects, page_size=500):
        rows = real(etype, aspects, page_size)
        for r in rows:
            if r["urn"] not in lag:
                lag[r["urn"]] = 3                       # listed only from its fourth read on
        out = [r for r in rows if lag[r["urn"]] <= 0]
        for r in rows:
            lag[r["urn"]] -= 1
        return out

    graph.scroll_entities = lagging
    naps = []
    for editor in (s.domains, s.glossary):
        editor.sleep = naps.append
    assert c.post("/catalog/domains", json={"id": "eu", "name": "EU"}, headers=as_(ALICE)).status_code == 201
    assert c.post("/catalog/domains", json={"id": "eu-billing", "name": "EU billing", "parent": "eu"},
                  headers=as_(ALICE)).status_code == 201
    r = c.delete("/catalog/domains/eu", headers=as_(ALICE))
    assert r.status_code == 409 and "eu-billing" in r.text, r.text
    assert naps                                                  # it waited for Core's index

    assert c.post("/catalog/glossary/nodes", json={"id": "tax", "name": "Tax"}, headers=as_(ALICE)).status_code == 201
    assert c.post("/catalog/glossary/terms", json={"id": "vat", "name": "VAT", "parent": "tax"},
                  headers=as_(ALICE)).status_code == 201
    assert c.delete("/catalog/glossary/nodes/tax", headers=as_(ALICE)).status_code == 409

    # An index that never catches up: the write stands (it is in Core), and the wait ends.
    graph.scroll_entities = lambda etype, aspects, page_size=500: []
    s.domains.index_wait_s = 0
    assert c.post("/catalog/domains", json={"id": "eu-tax", "name": "EU tax", "parent": "eu"},
                  headers=as_(ALICE)).status_code == 201


def test_moving_needs_the_privilege_where_it_goes(api):
    """Review of 5a0935b: holding MANAGE on the entry itself was enough to move it anywhere."""
    c, s, graph = api
    seed_glossary(c)
    body = {"id": "revenue", "name": "Revenue", "definition": "Money earned", "parent": "finance"}
    assert c.post("/catalog/glossary/terms", json=body, headers=as_(BOB)).status_code == 201
    # Bob stewards the finance group: he may not move it into Sales, nor a term of his to Sales or to the top.
    r = c.put("/catalog/glossary/nodes/finance", json={"name": "Finance", "parent": "sales"}, headers=as_(BOB))
    assert r.status_code == 403 and "to move it there" in r.text
    assert "parentNode" not in graph.aspects[(FINANCE_NODE, "glossaryNodeInfo")]
    assert c.put("/catalog/glossary/terms/revenue", json={**body, "parent": "sales"}, headers=as_(BOB)).status_code == 403
    assert c.put("/catalog/glossary/terms/revenue", json={**body, "parent": None}, headers=as_(BOB)).status_code == 403
    assert graph.aspects[(REVENUE, "glossaryTermInfo")]["parentNode"] == FINANCE_NODE
    # Inside what he stewards he moves freely; Alice, who runs the glossary, moves anything.
    assert c.put("/catalog/glossary/terms/revenue", json={**body, "parent": "ledger"}, headers=as_(BOB)).status_code == 200
    assert c.put("/catalog/glossary/nodes/finance", json={"name": "Finance", "parent": "sales"},
                 headers=as_(ALICE)).status_code == 200
    # Domains: the privilege on one domain does not move it under another.
    for d in ({"id": "sales", "name": "Sales"}, {"id": "emea", "name": "EMEA"}):
        assert c.post("/catalog/domains", json=d, headers=as_(ALICE)).status_code == 201
    bundle = s.pdp.bundle
    s.pdp.set_bundle(PolicyBundle(policies=[*bundle.policies, Policy(
        id="bob-emea", effect="allow", actions=["MANAGE_DOMAINS"], target=Target(resource_urns=["urn:li:domain:emea"]),
        condition=who(BOB))]))
    assert c.put("/catalog/domains/emea", json={"name": "EMEA", "parent": "sales"}, headers=as_(BOB)).status_code == 403
    assert c.put("/catalog/domains/emea", json={"name": "Europe"}, headers=as_(BOB)).status_code == 200
    assert "parentDomain" not in graph.aspects[("urn:li:domain:emea", "domainProperties")]


def test_an_entry_the_caller_cannot_see_is_not_there_for_them(api):
    """Review of 5a0935b: write routes and rights answered differently for a hidden entry than for a missing one."""
    c, s, _graph = api
    seed_glossary(c)
    assert c.post("/catalog/glossary/terms", json={"id": "merger", "name": "Merger", "definition": "Confidential",
                                                   "parent": "finance"}, headers=as_(ALICE)).status_code == 201
    merger = "urn:li:glossaryTerm:merger"
    s.pdp.set_bundle(PolicyBundle(policies=[*s.pdp.bundle.policies, Policy(
        id="hide-merger", effect="deny", actions=["VIEW_ENTITY_PAGE"], target=Target(resource_urns=[merger]),
        condition=who(BOB))]))
    body = {"name": "X", "definition": "x", "parent": "finance"}
    for tid in ("merger", "nope"):                       # the same answers for hidden and for absent
        assert c.get(f"/catalog/glossary/terms/{tid}", headers=as_(BOB)).status_code == 404
        assert c.get("/catalog/glossary/rights", params={"urn": f"urn:li:glossaryTerm:{tid}"},
                     headers=as_(BOB)).status_code == 404, tid
        assert c.put(f"/catalog/glossary/terms/{tid}", json=body, headers=as_(BOB)).status_code == 404, tid
        assert c.delete(f"/catalog/glossary/terms/{tid}", headers=as_(BOB)).status_code == 404, tid
        r = c.post("/catalog/glossary/terms", json={**body, "id": f"rel-{tid}", "related": [tid]}, headers=as_(BOB))
        assert r.status_code == 422, (tid, r.text)
    # Where Bob may not create, an existing name answers as any other (403), not 409.
    assert c.post("/catalog/glossary/terms", json={"id": "merger", "name": "M", "definition": "m", "parent": "sales"},
                  headers=as_(BOB)).status_code == 403
    assert c.get("/catalog/glossary/terms/merger", headers=as_(ALICE)).status_code == 200      # untouched


def test_a_forced_delete_names_only_what_the_caller_sees_and_changes_all_or_nothing(api):
    """Review of 5a0935b: the refusal named hidden assets; a failed second write said nothing had changed; a term
    that came with ingested columns was counted as removed."""
    c, s, graph = api
    seed_glossary(c)
    body = {"id": "revenue", "name": "Revenue", "definition": "Money earned", "parent": "finance"}
    assert c.post("/catalog/glossary/terms", json=body, headers=as_(BOB)).status_code == 201
    for asset in (ORDERS, SECRET):
        assert c.post("/catalog/assets/terms", json={"asset": asset, "term": "revenue"}, headers=as_(ALICE)).status_code == 200
    r = c.delete("/catalog/glossary/terms/revenue", params={"force": "true"}, headers=as_(BOB))
    assert r.status_code == 403 and "hr.salaries" not in r.text and "cannot see" in r.text
    assert c.get("/catalog/glossary/terms/revenue", headers=as_(ALICE)).status_code == 200

    # One write: when Core refuses it, the term is still on its assets and still there.
    def refuse(mcps):
        raise RuntimeError("Cairn Core POST /aspects failed: 500")
    graph.emit = refuse
    r = c.delete("/catalog/glossary/terms/revenue", params={"force": "true"}, headers=as_(ALICE))
    assert r.status_code == 502
    del graph.emit
    assert [t["urn"] for t in graph.aspects[(ORDERS, "glossaryTerms")]["terms"]] == [REVENUE]
    assert c.get("/catalog/glossary/terms/revenue", headers=as_(ALICE)).status_code == 200

    # A term that came with an asset's ingested columns cannot be taken off by Cairn: the delete is refused.
    editor = s.glossary
    graph.aspects[(ORDERS, "schemaMetadata")] = {"fields": [
        {"fieldPath": "amount", "glossaryTerms": {"terms": [{"urn": "urn:li:glossaryTerm:ingested"}]}}]}
    assert c.post("/catalog/glossary/terms", json={"id": "ingested", "name": "Ingested", "definition": "From the source",
                                                   "parent": "finance"}, headers=as_(ALICE)).status_code == 201
    editor._uses = lambda tid: [{"urn": ORDERS, "type": "DATASET", "name": "orders"}]
    r = c.delete("/catalog/glossary/terms/ingested", params={"force": "true"}, headers=as_(ALICE))
    assert r.status_code == 409 and "at the source" in r.text
    assert c.get("/catalog/glossary/terms/ingested", headers=as_(ALICE)).status_code == 200
    del editor._uses
    # And the ordinary forced delete still works, counting what it took the term off.
    out = c.delete("/catalog/glossary/terms/revenue", params={"force": "true"}, headers=as_(ALICE))
    assert out.status_code == 204, out.text
    assert graph.aspects[(ORDERS, "glossaryTerms")]["terms"] == [] and graph.aspects[(SECRET, "glossaryTerms")]["terms"] == []
    deleted = [e for e in s.audit.entries("glossary", 10) if e["action"] == "glossary.term_deleted"]
    assert deleted and deleted[0]["details"]["removed_from"] == 2
