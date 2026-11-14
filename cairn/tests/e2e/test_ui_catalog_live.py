"""The Catalog section (L147: Cairn Core at the centre) against the real Cairn Core, in Chromium.

What tests/test_catalog_ui_e2e.py stands in for, done here with Core: the catalog's entity types and facets (Core's
search), a data product made through Cairn's /catalog/products and then read back from Core (search, the product's
GraphQL, the domain's), a data contract published through Cairn, Core's glossary (a term group, a term with its
definition and steward, the dataset that uses it), Core's domains, and the lineage section with column-level lineage
from Core. Every GraphQL query the pages send is Core's real schema here. L148: a term group, a term, a domain and a
subdomain made through Cairn's API, the term put on a dataset's column and the dataset put in the subdomain, all read
back from Core's own GraphQL; someone without the privileges refused; deletes refused while in use, then done.

Run with the rest of tests/e2e (``CAIRN_E2E=1``, the stack's admin token and UI password; tests/e2e/run.sh)."""

from __future__ import annotations

import json
import os
import subprocess
import uuid

import pytest

from test_stack_smoke import API, EDGE, TOKEN, emit, eventually, gql

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1", reason="set CAIRN_E2E=1 to run against a live stack")

RUN = uuid.uuid4().hex[:8]
NAME, RAW_NAME = f"bookings_{RUN}", f"raw_bookings_{RUN}"
DS = f"urn:li:dataset:(urn:li:dataPlatform:postgres,cx_{RUN}.public.{NAME},PROD)"
RAW = f"urn:li:dataset:(urn:li:dataPlatform:postgres,cx_{RUN}.public.{RAW_NAME},PROD)"
DOMAIN_ID, DOMAIN_NAME = f"cx-{RUN}", f"CX E2E {RUN}"
DOMAIN = f"urn:li:domain:{DOMAIN_ID}"
PRODUCT_ID, PRODUCT_NAME = f"cx-{RUN}", f"Bookings 360 {RUN}"
PRODUCT = f"urn:li:dataProduct:{PRODUCT_ID}"
NODE_ID, TERM_ID = f"CxGroup{RUN}", f"CxBooking{RUN}"
TERM = f"urn:li:glossaryTerm:{TERM_ID}"
STAMP = {"time": 0, "actor": "urn:li:corpuser:admin"}


def _schema(name: str, cols):
    return {"schemaName": name, "platform": "urn:li:dataPlatform:postgres", "version": 0, "hash": "",
            "platformSchema": {"com.linkedin.schema.MySqlDDL": {"tableSchema": ""}},
            "fields": [{"fieldPath": p, "nativeDataType": t, "type": {"type": {"com.linkedin.schema.StringType": {}}}}
                       for p, t in cols]}


@pytest.fixture(scope="module", autouse=True)
def seeded():
    import httpx

    for urn, name in ((DS, NAME), (RAW, RAW_NAME)):
        emit(urn, "schemaMetadata", _schema(name, (("id", "INT"), ("amount", "NUMERIC"))))
        emit(urn, "datasetProperties", {"name": name, "description": f"E2E {name}: one row per booking."})
    emit(DS, "ownership", {"owners": [{"owner": "urn:li:corpuser:admin", "type": "DATAOWNER"}], "lastModified": STAMP})
    emit(DS, "upstreamLineage", {"upstreams": [{"dataset": RAW, "type": "TRANSFORMED", "auditStamp": STAMP}],
                                 "fineGrainedLineages": [{"upstreamType": "FIELD_SET", "downstreamType": "FIELD",
                                                          "upstreams": [f"urn:li:schemaField:({RAW},amount)"],
                                                          "downstreams": [f"urn:li:schemaField:({DS},amount)"],
                                                          "transformOperation": "SUM", "confidenceScore": 1.0}]})
    gql("mutation($input: CreateDomainInput!) { createDomain(input: $input) }", {"input": {"id": DOMAIN_ID, "name": DOMAIN_NAME}})
    emit(DS, "domains", {"domains": [DOMAIN]})
    gql("mutation($input: CreateGlossaryEntityInput!) { createGlossaryNode(input: $input) }",
        {"input": {"id": NODE_ID, "name": f"CX group {RUN}", "description": "E2E term group"}})
    gql("mutation($input: CreateGlossaryEntityInput!) { createGlossaryTerm(input: $input) }",
        {"input": {"id": TERM_ID, "name": f"Booking {RUN}", "description": "A booking made by a customer.",
                   "parentNode": f"urn:li:glossaryNode:{NODE_ID}"}})
    emit(TERM, "ownership", {"owners": [{"owner": "urn:li:corpuser:admin", "type": "DATAOWNER"}], "lastModified": STAMP})
    emit(DS, "glossaryTerms", {"terms": [{"urn": TERM}], "auditStamp": STAMP})
    with httpx.Client(base_url=API, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60) as c:
        # Made through Cairn, kept in Core.
        r = c.post("/catalog/products", json={"id": PRODUCT_ID, "name": PRODUCT_NAME, "description": "E2E product",
                                              "domain": DOMAIN, "owners": ["urn:li:corpuser:admin"], "assets": [DS],
                                              "output_ports": [DS]})
        assert r.status_code == 201, r.text
        r = c.put("/quality/contracts", json={"dataset": DS, "version": "1.0.0", "owner": "urn:li:corpuser:admin",
                                              "schema": {"columns": [{"name": "id"}, {"name": "amount"}]}})
        assert r.status_code == 200, r.text

    def indexed():   # search is fed asynchronously (MCL → search index)
        r = gql("""query($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) {
                    searchResults { entity { urn } } } }""",
                {"input": {"types": ["DATASET", "DATA_PRODUCT"], "query": RUN, "start": 0, "count": 20}})
        found = {x["entity"]["urn"] for x in r["searchAcrossEntities"]["searchResults"]}
        return DS in found and PRODUCT in found
    eventually(indexed, timeout=120, interval=3)


def test_the_product_made_through_cairn_is_in_core():
    p = gql("query($u: String!) { dataProduct(urn: $u) { properties { name customProperties { key value } } "
            "domain { domain { urn } } } }", {"u": PRODUCT})["dataProduct"]
    assert p["properties"]["name"] == PRODUCT_NAME and p["domain"]["domain"]["urn"] == DOMAIN
    custom = {e["key"]: e["value"] for e in p["properties"]["customProperties"]}
    assert json.loads(custom["cairn.output_ports"]) == [DS] and custom["cairn.managed"] == "true"


def test_catalog_products_contracts_glossary_domains_and_lineage_from_core():
    out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "ui_catalog.js")], capture_output=True,
                         text=True, timeout=900, env={
                             **os.environ, "EDGE": EDGE, "SHOTS": os.environ.get("CAIRN_E2E_SCREENSHOT_DIR", ""),
                             "UI_USER": os.environ.get("CAIRN_E2E_UI_USER", "admin"),
                             "UI_PASSWORD": os.environ["CAIRN_E2E_UI_PASSWORD"], "RUN": RUN, "DS_NAME": NAME, "DS_URN": DS,
                             "RAW_NAME": RAW_NAME, "PRODUCT_URN": PRODUCT, "PRODUCT_NAME": PRODUCT_NAME,
                             "DOMAIN_NAME": DOMAIN_NAME, "TERM_URN": TERM, "TERM_NAME": f"Booking {RUN}",
                             "GROUP_NAME": f"CX group {RUN}"})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert not result["errors"], result
    # The catalog: Core's search, by type, with counts; the dataset found and opened.
    assert result["catalog"]["datasets"] >= 2 and result["catalog"]["opened"] == NAME, result["catalog"]
    # The product made through Cairn, listed from Core with its domain, and its page with the output port.
    assert result["products"] == {"row": True, "domain": DOMAIN_NAME, "port": True}, result["products"]
    # The contract published through Cairn.
    assert result["contracts"]["row"] and result["contracts"]["status"] in ("Passing", "Failing", "Not checked yet"), result
    # Core's glossary: the group, the term with its definition and steward, the dataset using it.
    assert result["glossary"] == {"group": True, "definition": True, "steward": True, "asset": True}, result["glossary"]
    # Core's domains: the domain with the product and the dataset.
    assert result["domain"] == {"product": True, "asset": True}, result["domain"]
    # Lineage: the graph from Cairn's lineage API, and column lineage from Core.
    assert result["lineage"] == {"graph": True, "column": True}, result["lineage"]


# ---- L148: the glossary and domains edited through Cairn, read back from Cairn Core's own GraphQL ----------------------
EDIT_DS = f"urn:li:dataset:(urn:li:dataPlatform:postgres,cx_{RUN}.public.invoices_{RUN},PROD)"
VIEWER = f"urn:li:corpuser:cx-viewer-{RUN}"


def _client(token: str):
    import httpx

    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=60)


def test_glossary_and_domains_made_through_cairn_are_in_core():
    node, term, dom, sub = f"cx-group-{RUN}", f"cx-invoice-{RUN}", f"cx-billing-{RUN}", f"cx-billing-eu-{RUN}"
    emit(EDIT_DS, "schemaMetadata", _schema(f"invoices_{RUN}", (("id", "INT"), ("total", "NUMERIC"))))
    emit(EDIT_DS, "datasetProperties", {"name": f"invoices_{RUN}", "description": "E2E invoices"})
    emit(VIEWER, "corpUserInfo", {"active": True, "displayName": "CX viewer", "email": f"cx-viewer-{RUN}@example.com"})
    viewer_token = gql("""mutation($input: CreateAccessTokenInput!) { createAccessToken(input: $input) { accessToken } }""",
                       {"input": {"type": "PERSONAL", "actorUrn": VIEWER, "duration": "ONE_DAY",
                                  "name": f"e2e glossary {RUN}"}})["createAccessToken"]["accessToken"]
    with _client(TOKEN) as root, _client(viewer_token) as viewer:
        # Someone without MANAGE_GLOSSARIES / MANAGE_DOMAINS / EDIT_ENTITY_*: refused, nothing written.
        assert viewer.post("/catalog/glossary/nodes", json={"id": node, "name": "nope"}).status_code == 403
        assert viewer.post("/catalog/domains", json={"id": dom, "name": "nope"}).status_code == 403
        assert viewer.get("/catalog/glossary/rights").json()["may_manage"] is False
        # Made through Cairn.
        r = root.post("/catalog/glossary/nodes", json={"id": node, "name": f"CX billing words {RUN}", "description": "E2E"})
        assert r.status_code == 201, r.text
        r = root.post("/catalog/glossary/terms", json={
            "id": term, "name": f"Invoice {RUN}", "definition": "A bill sent to a customer.", "parent": node,
            "stewards": ["urn:li:corpuser:admin"], "related": [TERM_ID], "custom_properties": {"standard": "e-invoicing"}})
        assert r.status_code == 201, r.text
        r = root.post("/catalog/domains", json={"id": dom, "name": f"CX billing {RUN}", "owners": ["urn:li:corpuser:admin"]})
        assert r.status_code == 201, r.text
        r = root.post("/catalog/domains", json={"id": sub, "name": f"CX billing EU {RUN}", "parent": dom})
        assert r.status_code == 201, r.text
        # Applied: the term on a column, the dataset in the subdomain.
        r = root.post("/catalog/assets/terms", json={"asset": EDIT_DS, "term": term, "field": "total"})
        assert r.status_code == 200 and r.json()["changed"], r.text
        r = root.put("/catalog/assets/domain", json={"asset": EDIT_DS, "domain": sub})
        assert r.status_code == 200 and r.json()["changed"], r.text
        # (404 when the stack's policies do not even let them see the term)
        assert viewer.post("/catalog/assets/terms", json={"asset": EDIT_DS, "term": term}).status_code in (403, 404)
        assert viewer.put("/catalog/assets/domain", json={"asset": EDIT_DS, "domain": None}).status_code == 403
        assert viewer.put(f"/catalog/glossary/terms/{term}", json={"id": term, "name": "Hijacked"}).status_code == 403

        # Read back from Cairn Core's own GraphQL.
        t = gql("""query($u: String!) { glossaryTerm(urn: $u) { properties { name definition customProperties { key value } }
                    parentNodes { nodes { urn } }
                    ownership { owners { owner { ... on CorpUser { urn } } type } } } }""",
                {"u": f"urn:li:glossaryTerm:{term}"})["glossaryTerm"]
        assert t["properties"]["name"] == f"Invoice {RUN}" and t["properties"]["definition"] == "A bill sent to a customer."
        assert {"key": "standard", "value": "e-invoicing"} in t["properties"]["customProperties"]
        assert [n["urn"] for n in t["parentNodes"]["nodes"]] == [f"urn:li:glossaryNode:{node}"]
        related = """query($u: String!) { glossaryTerm(urn: $u) { related: relationships(input: { types: ["IsRelatedTo"],
                     direction: OUTGOING, start: 0, count: 10 }) { relationships { entity { urn } } } } }"""
        eventually(lambda: [x["entity"]["urn"] for x in gql(related, {"u": f"urn:li:glossaryTerm:{term}"})[
            "glossaryTerm"]["related"]["relationships"]] == [TERM], timeout=120, interval=3)   # the graph index is async
        assert [(o["owner"]["urn"], o["type"]) for o in t["ownership"]["owners"]] == [("urn:li:corpuser:admin", "DATA_STEWARD")]
        n = gql("query($u: String!) { glossaryNode(urn: $u) { properties { name } } }",
                {"u": f"urn:li:glossaryNode:{node}"})["glossaryNode"]
        assert n["properties"]["name"] == f"CX billing words {RUN}"
        d = gql("query($u: String!) { domain(urn: $u) { properties { name } parentDomains { domains { urn } } } }",
                {"u": f"urn:li:domain:{sub}"})["domain"]
        assert d["properties"]["name"] == f"CX billing EU {RUN}"
        assert [x["urn"] for x in d["parentDomains"]["domains"]] == [f"urn:li:domain:{dom}"]
        ds = gql("""query($u: String!) { dataset(urn: $u) { domain { domain { urn } }
                     editableSchemaMetadata { editableSchemaFieldInfo { fieldPath glossaryTerms { terms { term { urn } } } } } } }""",
                 {"u": EDIT_DS})["dataset"]
        assert ds["domain"]["domain"]["urn"] == f"urn:li:domain:{sub}"
        fields = {f["fieldPath"]: [x["term"]["urn"] for x in (f["glossaryTerms"] or {}).get("terms") or []]
                  for f in ds["editableSchemaMetadata"]["editableSchemaFieldInfo"]}
        assert fields == {"total": [f"urn:li:glossaryTerm:{term}"]}

        # Read through Cairn too; the term's assets come from Core's search (fed asynchronously).
        assert root.get(f"/catalog/glossary/terms/{term}").json()["stewards"] == ["urn:li:corpuser:admin"]
        eventually(lambda: [a["urn"] for a in root.get(f"/catalog/glossary/terms/{term}/assets").json()["assets"]] == [EDIT_DS],
                   timeout=120, interval=3)
        eventually(lambda: [a["urn"] for a in root.get(f"/catalog/domains/{sub}/assets").json()["assets"]] == [EDIT_DS],
                   timeout=120, interval=3)
        # A domain with an asset is not deleted unless forced; forced, the asset is taken out and Core deletes it.
        assert root.delete(f"/catalog/domains/{dom}").status_code == 409                  # a domain inside it
        assert root.delete(f"/catalog/domains/{sub}").status_code == 409                  # an asset in it
        r = root.delete(f"/catalog/domains/{sub}", params={"force": "true"})
        assert r.status_code == 204, r.text
        assert gql("query($u: String!) { dataset(urn: $u) { domain { domain { urn } } } }", {"u": EDIT_DS})["dataset"]["domain"] is None
        assert root.get(f"/catalog/domains/{sub}").status_code == 404
        # Core's deleteDomain asks its search whether domains are inside; it may still list the one just deleted.
        eventually(lambda: root.delete(f"/catalog/domains/{dom}").status_code == 204, timeout=120, interval=3)
        # The term taken off its column, then deleted (soft: Core's status).
        assert root.post("/catalog/assets/terms/remove", json={"asset": EDIT_DS, "term": term, "field": "total"}).json()["changed"]
        # Core's search still lists the dataset until it re-indexes it: refused (409) until then.
        eventually(lambda: root.delete(f"/catalog/glossary/terms/{term}").status_code == 204, timeout=120, interval=3)
        assert root.get(f"/catalog/glossary/terms/{term}").status_code == 404
        assert root.delete(f"/catalog/glossary/nodes/{node}").status_code == 204
