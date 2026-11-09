"""The workspace's guided experience and forms against the real Cairn Core (L127, L128, L129), in Chromium.

What the browser tests outside the stack stand in for, done here with Core: the command palette's catalog results
(Core's search, as the person), an asset page's header and a data product's (Core's GraphQL, through the masking
proxy), a form's dataset picker searching Core's catalog (not its fallback to the connections' tables), and a
glossary term from Core's glossary added to a dataset with bulk edit (written to Core, read back from it).

Run with the rest of tests/e2e (``CAIRN_E2E=1``, the stack's admin token and UI password; tests/e2e/run.sh)."""

from __future__ import annotations

import json
import os
import subprocess
import uuid

import pytest

from test_stack_smoke import EDGE, emit, eventually, gql

pytestmark = pytest.mark.skipif(os.environ.get("CAIRN_E2E") != "1", reason="set CAIRN_E2E=1 to run against a live stack")

RUN = uuid.uuid4().hex[:8]
NAME = f"ledger_{RUN}"
DS = f"urn:li:dataset:(urn:li:dataPlatform:postgres,gx_{RUN}.public.{NAME},PROD)"
DOMAIN_ID, DOMAIN_NAME = f"gx-{RUN}", f"GX E2E {RUN}"
PRODUCT, PRODUCT_NAME = f"urn:li:dataProduct:gx-{RUN}", f"Ledger 360 {RUN}"
TERM_ID = f"GxLedgerEntry{RUN}"
TERM = f"urn:li:glossaryTerm:{TERM_ID}"
STAMP = {"time": 0, "actor": "urn:li:corpuser:admin"}


@pytest.fixture(scope="module", autouse=True)
def seeded():
    fields = [{"fieldPath": p, "nativeDataType": t, "type": {"type": {"com.linkedin.schema.StringType": {}}}}
              for p, t in (("id", "INT"), ("account", "VARCHAR"), ("amount", "NUMERIC"))]
    emit(DS, "schemaMetadata", {"schemaName": NAME, "platform": "urn:li:dataPlatform:postgres", "version": 0, "hash": "",
                                "platformSchema": {"com.linkedin.schema.MySqlDDL": {"tableSchema": ""}}, "fields": fields})
    emit(DS, "datasetProperties", {"name": NAME, "description": "E2E ledger: one row per booking."})
    emit(DS, "ownership", {"owners": [{"owner": "urn:li:corpuser:admin", "type": "DATAOWNER"}], "lastModified": STAMP})
    gql("""mutation($input: CreateDomainInput!) { createDomain(input: $input) }""",
        {"input": {"id": DOMAIN_ID, "name": DOMAIN_NAME}})
    emit(DS, "domains", {"domains": [f"urn:li:domain:{DOMAIN_ID}"]})
    gql("""mutation($input: CreateDataProductInput!) { createDataProduct(input: $input) { urn } }""",
        {"input": {"id": f"gx-{RUN}", "domainUrn": f"urn:li:domain:{DOMAIN_ID}",
                   "properties": {"name": PRODUCT_NAME, "description": "E2E data product of the ledger"}}})
    gql("""mutation($input: BatchSetDataProductInput!) { batchSetDataProduct(input: $input) }""",
        {"input": {"dataProductUrn": PRODUCT, "resourceUrns": [DS]}})
    gql("""mutation($input: CreateGlossaryEntityInput!) { createGlossaryTerm(input: $input) }""",
        {"input": {"id": TERM_ID, "name": f"Ledger Entry {RUN}", "description": "A booking in the ledger."}})

    def indexed():   # search is fed asynchronously (MCL → search index)
        r = gql("""query($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) {
                    searchResults { entity { urn } } } }""",
                {"input": {"types": ["DATASET"], "query": NAME, "start": 0, "count": 5}})
        return DS in [x["entity"]["urn"] for x in r["searchAcrossEntities"]["searchResults"]]
    eventually(indexed, timeout=120, interval=3)


def test_palette_asset_and_product_headers_picker_and_a_glossary_term_from_core():
    out = subprocess.run(["node", os.path.join(os.path.dirname(__file__), "ui_guided.js")], capture_output=True,
                         text=True, timeout=600, env={
                             **os.environ, "EDGE": EDGE, "SHOTS": os.environ.get("CAIRN_E2E_SCREENSHOT_DIR", ""),
                             "UI_USER": os.environ.get("CAIRN_E2E_UI_USER", "admin"),
                             "UI_PASSWORD": os.environ["CAIRN_E2E_UI_PASSWORD"],
                             "DS_NAME": NAME, "DS_URN": DS, "PRODUCT_URN": PRODUCT, "TERM_ID": TERM_ID})
    assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-3000:]
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert not result["errors"], result
    # The palette's catalog result: Core's search, a dataset on postgres.
    assert NAME in result["palette"] and "dataset" in result["palette"].lower(), result
    # The asset page's shared header, from Core: name, platform and domain; the help button and its docs anchor.
    asset = result["asset"]
    assert asset["title"] == NAME, asset
    assert asset["description"] == f"Dataset · postgres · {DOMAIN_NAME}", asset
    assert asset["help"] == "getting-started.html#understand-an-asset" and asset["helpButton"] == 1, asset
    # The data product's header, from Core.
    assert result["product"] == {"title": PRODUCT_NAME, "description": DOMAIN_NAME}, result["product"]
    # The picker found the dataset in Core's catalog (by its name) and the form sends its urn.
    assert NAME in result["picker"]["option"] and result["picker"]["body"].get("dataset_urn") == DS, result["picker"]
    # The glossary term is on the dataset in Core.
    assert result["bulk"], result

    def tagged():
        r = gql("query($u: String!) { dataset(urn: $u) { glossaryTerms { terms { term { urn } } } } }", {"u": DS})
        return TERM in [t["term"]["urn"] for t in ((r["dataset"] or {}).get("glossaryTerms") or {}).get("terms") or []]
    eventually(tagged, timeout=60)


def test_the_term_is_shown_on_the_asset_page_from_core():
    """After the bulk edit, Core answers the asset with the term: the asset page's GraphQL (through the masking
    proxy, as the UI asks) lists it."""
    from test_active_metadata_live import frontend_gql
    from test_stack_smoke import TOKEN

    def listed():
        body = frontend_gql(TOKEN, "query($u: String!) { dataset(urn: $u) { glossaryTerms { terms { term { urn "
                                   "properties { name } } } } } }", {"u": DS}).json()
        terms = (((body.get("data") or {}).get("dataset") or {}).get("glossaryTerms") or {}).get("terms") or []
        return any(t["term"]["urn"] == TERM and t["term"]["properties"]["name"] == f"Ledger Entry {RUN}" for t in terms)
    eventually(listed, timeout=60)
