"""SaaS connectors (cairn.pipelines.saas) without a catalog: what a sync may be defined as, and how records are read
from the APIs (answered through httpx's mock transport). Landing in Iceberg on real components is in
tests/test_saas_e2e.py."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.pipelines.saas import SaasError, SaasSyncs, hubspot_records, salesforce_records

OWNER = "urn:li:corpuser:ana"


@pytest.fixture()
def syncs(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/s.db")
    init_db(engine)
    return SaasSyncs(engine, {}, lambda *a: "", lambda owner, urn: True)


def body(kind, fields, obj=None):
    return {"name": "s", "kind": kind, "object": obj or {"hubspot": "contacts", "salesforce": "Account",
                                                         "google_sheets": "1AbCdEfGhIjKlMnOp/Sheet1"}[kind],
            "fields": fields, "target": "crm.t", "secret_scope": "keys"}


@pytest.mark.parametrize("kind,fields", [("hubspot", ["id", "email"]), ("google_sheets", ["ID", "region"]),
                                         ("hubspot", ["Email", "email"]), ("salesforce", ["Name", "NAME"])])
def test_two_fields_never_land_in_one_column(syncs, kind, fields):
    # Each field is a lower-case column beside the record's own id. A field named id overwrote the record ids
    # (with HubSpot's absent "id" property: nulls in the required id column), and two fields
    # that differ only in case landed in one column, one hiding the other.
    with pytest.raises(SaasError, match="column"):
        syncs.save(OWNER, body(kind, fields))


def test_salesforce_id_is_the_record_id(syncs):
    # Salesforce's Id field is the record id itself: asking for it is harmless and stays allowed.
    assert syncs.save(OWNER, body("salesforce", ["Id", "Name"]))["fields"] == ["Id", "Name"]


def client(answer):
    calls = []

    def handle(request):
        calls.append(request)
        if len(calls) > 50:                              # what the test stops; the reader must stop far sooner
            return httpx.Response(500, json={})
        return answer(request, len(calls))
    return httpx.Client(transport=httpx.MockTransport(handle)), calls


def test_a_salesforce_page_that_points_back_stops_the_read():
    # nextRecordsUrl was followed as long as the API gave one: an answer naming a page already read looped without
    # end, loading the same records over and over.
    http, calls = client(lambda r, n: httpx.Response(200, json={
        "done": False, "nextRecordsUrl": "/services/data/v61.0/query/01g-2000",
        "records": [{"Id": f"001{n}", "SystemModstamp": "2026-11-05T10:00:00.000+0000"}]}))
    keys = {"instance_url": "https://sf.example", "access_token": "t"}
    with pytest.raises(SaasError, match="already read"):
        list(salesforce_records(http, keys, "Account", ["Name"], None))
    assert len(calls) == 2


def test_a_hubspot_page_that_does_not_move_on_stops_the_read():
    # The search's paging.next.after was followed until it reached 10,000: one that did not move on looped.
    http, calls = client(lambda r, n: httpx.Response(200, json={
        "results": [{"id": "1", "properties": {"hs_lastmodifieddate": "2026-11-05T10:00:00.000Z"}}],
        "paging": {"next": {"after": "100"}}}))
    with pytest.raises(SaasError, match="already read"):
        list(hubspot_records(http, {"access_token": "t"}, "contacts", ["email"], None))
    assert len(calls) == 2
