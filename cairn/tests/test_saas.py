"""SaaS connectors (cairn.pipelines.saas) without a catalog: what a sync may be defined as, and how records are read
from the APIs (answered through httpx's mock transport). Landing in Iceberg on real components is in
tests/test_saas_e2e.py."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from cairn.common.db import init_db
from cairn.pipelines.saas import SaasError, SaasSyncs

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
